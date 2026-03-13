# app/extractors/goalserve.py — Async GoalServe JSON client
#
# Uses correct GoalServe endpoint names (verified via curl):
#   - standings/{id}.xml?json=true     → league standings
#   - soccerleague/{id}?json=true      → teams + squads + player stats
#   - soccerfixtures/leagueid/{id}     → fixtures with embedded lineups
#   - soccernew/injuries               → global injuries feed
#   - soccerstats/player/{id}          → individual player profile

import asyncio
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

import httpx
from tenacity import (
    retry, stop_after_attempt,
    wait_exponential, retry_if_exception_type,
)

from app.core.config import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_MIN_INTERVAL = 60.0 / settings.REQUESTS_PER_MINUTE


class GoalServeClient:
    """Async HTTP client for GoalServe JSON soccer feeds."""

    def __init__(self, api_key: str = ""):
        self._key = api_key or settings.GOALSERVE_API_KEY
        if not self._key:
            raise ValueError(
                "GOALSERVE_API_KEY is not set. Add it to your .env file."
            )
        self._base = settings.GOALSERVE_BASE_URL
        self._last_call: float = 0.0
        self._throttle_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.REQUEST_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _throttle(self):
        """Global rate limiter — safe for concurrent callers via asyncio.Lock."""
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_call
            wait = _MIN_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _fetch_json(self, path: str, params: Dict | None = None) -> Dict:
        """
        Fetch a GoalServe feed as JSON.
        URL pattern: {base}/{api_key}/{path}?json=true&{extra_params}
        """
        await self._throttle()
        url = f"{self._base}/{self._key}/{path}"
        p = {"json": "true"}
        if params:
            p.update(params)

        log.debug("GET %s | params=%s", path, p)
        client = await self._get_client()

        for attempt in range(settings.MAX_RETRIES):
            try:
                resp = await client.get(url, params=p)
                resp.raise_for_status()

                data = resp.json()
                if not data:
                    raise ValueError(f"Empty response from feed: {path}")
                return data

            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt < settings.MAX_RETRIES - 1:
                    wait_time = min(2 ** (attempt + 1), 30)
                    log.warning(
                        "Retry %d/%d for %s: %s (waiting %ds)",
                        attempt + 1, settings.MAX_RETRIES, path, e, wait_time,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    log.error("All retries exhausted for %s: %s", path, e)
                    raise

    # ── Standings ────────────────────────────────────────────────────────────

    async def get_standings(self, league_id: str) -> Dict:
        """
        League table / standings.
        Endpoint: standings/{league_id}.xml?json=true
        Returns: {standings: {tournament: {team: [...]}}}
        """
        log.info("Fetching standings for league_id=%s", league_id)
        return await self._fetch_json(f"standings/{league_id}.xml")

    # ── League players (teams + squads + stats) ─────────────────────────────

    async def get_league_players(self, league_id: str) -> Dict:
        """
        All teams and their full squads with season stats.
        Endpoint: soccerleague/{league_id}?json=true
        Returns: {league: {team: [{squad: {player: [...]}}]}}
        """
        log.info("Fetching league players for league_id=%s", league_id)
        return await self._fetch_json(f"soccerleague/{league_id}")

    # ── Fixtures ─────────────────────────────────────────────────────────────

    async def get_fixtures(self, league_id: str) -> Dict:
        """
        All fixtures (past + upcoming) with embedded lineups.
        Endpoint: soccerfixtures/leagueid/{league_id}?json=true
        Returns: {results: {tournament: [{week: [{match: [...]}]}]}}
        """
        log.info("Fetching fixtures for league_id=%s", league_id)
        return await self._fetch_json(f"soccerfixtures/leagueid/{league_id}")

    # ── Injuries (global) ────────────────────────────────────────────────────

    async def get_injuries(self) -> Dict:
        """
        Global injuries feed (all leagues).
        Endpoint: soccernew/injuries?json=true
        Returns: {scores: {category: [...]}}
        Caller must filter by team IDs for their league.
        """
        log.info("Fetching global injuries feed")
        return await self._fetch_json("soccernew/injuries")

    # ── Player profile ───────────────────────────────────────────────────────

    async def get_player_profile(self, player_id: str) -> Dict:
        """
        Individual player career stats, transfers, trophies.
        Endpoint: soccerstats/player/{player_id}?json=true
        """
        log.debug("Fetching player profile for player_id=%s", player_id)
        return await self._fetch_json(f"soccerstats/player/{player_id}")

    # ── Leagues list ─────────────────────────────────────────────────────────

    async def get_leagues(self) -> list[dict]:
        """
        All available soccer leagues from GoalServe.
        Endpoint: soccer/leagues  (returns XML, no json=true mode)
        Returns a list of {id, name, category} dicts.
        """
        log.info("Fetching leagues list")
        await self._throttle()
        url = f"{self._base}/{self._key}/soccer/leagues"
        client = await self._get_client()

        for attempt in range(settings.MAX_RETRIES):
            try:
                resp = await client.get(url, timeout=settings.REQUEST_TIMEOUT, follow_redirects=True)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                return [
                    {
                        "id": cat.get("id"),
                        "name": cat.get("name"),
                        "category": cat.get("maincat"),
                    }
                    for cat in root.findall("category")
                    if cat.get("id") and cat.get("name")
                ]
            except Exception as e:
                if attempt < settings.MAX_RETRIES - 1:
                    wait_time = min(2 ** (attempt + 1), 30)
                    log.warning("Retry %d/%d for leagues: %s", attempt + 1, settings.MAX_RETRIES, e)
                    await asyncio.sleep(wait_time)
                else:
                    log.error("All retries exhausted for leagues: %s", e)
                    raise

    # ── Health check ─────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Quick connectivity check using a lightweight standings call."""
        try:
            data = await self._fetch_json("standings/1204.xml")
            return bool(data)
        except Exception:
            return False
