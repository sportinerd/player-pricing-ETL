# app/pipeline/orchestrator.py (v3.0)
#
# Async ETL pipeline using 4 GoalServe API calls (down from 100+).
# Structure preserved from v24 pipeline.py — only I/O boundaries changed.

import datetime
from typing import Dict, List, Optional, Set

from app.core.config import settings
from app.extractors.goalserve import GoalServeClient
from app.transformers.normalizer import (
    extract_all_players, extract_standings, build_team_strength_map, safe_float,
    extract_prev_season_from_profile,
)
from app.transformers.factors.season_blend import merge_player_seasons
from app.transformers.factors.xi_estimator import (
    _parse_squad_depth, compute_xi_probability, compute_rival_injury_bonuses,
)
from app.transformers.factors.injury_analyzer import (
    parse_injury_feed, apply_current_injury_status, apply_historical_injury_discounts,
)
from app.transformers.factors.national_team import enrich_players_with_nt_factors
from app.pricing.engine import (
    detect_format, price_all_players, compute_fdr_multiplier,
)
from app.loaders.mongo_loader import (
    save_pricing_run, save_player_prices, save_standings_cache,
    save_injuries_cache, save_teams, save_goalserve_cache, load_goalserve_cache,
    save_prev_season_cache, load_prev_season_cache,
)
from app.loaders.fgl_sync import FGLSync
from app.utils.logger import get_logger

log = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_current_gw(standing_rows: List[Dict]) -> int:
    if not standing_rows:
        return 38
    total_games = sum(
        r.get("won", 0) + r.get("drawn", 0) + r.get("lost", 0)
        for r in standing_rows
    )
    n_teams = len(standing_rows)
    if n_teams == 0:
        return 38
    gw = round(total_games / n_teams)
    log.info("Detected current GW ≈ %d", gw)
    return max(gw, 0)


def _extract_upcoming_fixtures(
    fixtures_data: Dict, team_id: str, n: int = 5,
) -> List[Dict]:
    """Extract upcoming (not yet played) fixtures for a team from soccerfixtures response."""
    upcoming: List[Dict] = []

    tournaments = fixtures_data.get("results", {}).get("tournament", [])
    if isinstance(tournaments, dict):
        tournaments = [tournaments]

    for tournament in tournaments:
        weeks = tournament.get("week", [])
        if isinstance(weeks, dict):
            weeks = [weeks]
        for week in weeks:
            matches = week.get("match", [])
            if isinstance(matches, dict):
                matches = [matches]
            for match in matches:
                status = (match.get("status") or "").upper()
                if status in ("NS", "TBD", "SCHED", "SCHEDULED", "NOT STARTED", ""):
                    home_id = str(match.get("localteam", {}).get("id", ""))
                    away_id = str(match.get("visitorteam", {}).get("id", ""))
                    if team_id in (home_id, away_id):
                        upcoming.append({
                            "localteam_id": home_id,
                            "visitorteam_id": away_id,
                            "date": match.get("date", ""),
                            "status": status,
                        })

    def _date_key(m):
        raw = m.get("date", "01.01.9999")
        try:
            return datetime.datetime.strptime(raw, "%d.%m.%Y")
        except ValueError:
            return datetime.datetime.max

    upcoming.sort(key=_date_key)
    return upcoming[:n]


def _extract_completed_matches(fixtures_data: Dict) -> List[Dict]:
    """Extract completed matches from soccerfixtures response."""
    completed: List[Dict] = []

    tournaments = fixtures_data.get("results", {}).get("tournament", [])
    if isinstance(tournaments, dict):
        tournaments = [tournaments]

    for tournament in tournaments:
        weeks = tournament.get("week", [])
        if isinstance(weeks, dict):
            weeks = [weeks]
        for week in weeks:
            matches = week.get("match", [])
            if isinstance(matches, dict):
                matches = [matches]
            for match in matches:
                status = (match.get("status") or "").upper()
                if status in ("FT", "AET", "PEN", "FT_PEN"):
                    completed.append(match)

    def _date_key(m):
        raw = m.get("date", "01.01.1970")
        try:
            return datetime.datetime.strptime(raw, "%d.%m.%Y")
        except ValueError:
            return datetime.datetime.min

    completed.sort(key=_date_key, reverse=True)
    return completed


def _extract_form_from_fixtures(
    completed_matches: List[Dict],
    team_ids: List[str],
    last_n: int = 5,
) -> Dict[str, List[Dict]]:
    """
    Extract per-player recent match stats from completed fixture lineups.
    Returns {player_id: [list of per-match stat dicts]}.
    """
    form_lookup: Dict[str, List[Dict]] = {}

    # Group completed matches by team, take last_n per team
    team_matches: Dict[str, List[Dict]] = {tid: [] for tid in team_ids}
    for match in completed_matches:
        home_id = str(match.get("localteam", {}).get("id", ""))
        away_id = str(match.get("visitorteam", {}).get("id", ""))
        match_date = match.get("date", "")

        for tid in (home_id, away_id):
            if tid in team_matches and len(team_matches[tid]) < last_n:
                team_matches[tid].append(match)

    for tid, matches in team_matches.items():
        for match in matches:
            match_date = match.get("date", "")
            lineups = match.get("lineups", {})

            for team_key in ("localteam", "visitorteam"):
                team_lineup = lineups.get(team_key, {})
                team_node = match.get(team_key, {})
                lineup_team_id = str(team_node.get("id", ""))

                if lineup_team_id != tid:
                    continue

                players = team_lineup.get("player", [])
                if isinstance(players, dict):
                    players = [players]

                for p in players:
                    pid = str(p.get("id", "")).strip()
                    if not pid:
                        continue
                    form_entry = {
                        "match_date": match_date,
                        "date": match_date,
                        "minutes_played": 90,  # default for starters in lineups
                        "goals": 0,
                        "assists": 0,
                        "yellow_cards": 0,
                        "red_cards": 0,
                        "saves": 0,
                        "clean_sheets": 0,
                        "shots_on_target": 0,
                        "key_passes": 0,
                    }
                    # Parse booking info from lineup
                    booking = p.get("booking", "")
                    if "YC" in str(booking):
                        form_entry["yellow_cards"] = 1
                    if "RC" in str(booking):
                        form_entry["red_cards"] = 1

                    form_lookup.setdefault(pid, []).append(form_entry)

    return form_lookup


def _extract_lineup_data_from_fixtures(
    completed_matches: List[Dict],
    team_ids: List[str],
    last_n: int = 10,
) -> Dict[str, Dict]:
    """
    Extract lineup rates from completed fixtures.
    Returns {player_id: {started: N, subbed: N, appeared_in: N, lineup_rate: float}}
    """
    player_stats: Dict[str, Dict] = {}

    team_match_counts: Dict[str, int] = {tid: 0 for tid in team_ids}

    for match in completed_matches:
        home_id = str(match.get("localteam", {}).get("id", ""))
        away_id = str(match.get("visitorteam", {}).get("id", ""))
        lineups = match.get("lineups", {})

        for team_key, tid in [("localteam", home_id), ("visitorteam", away_id)]:
            if tid not in team_match_counts:
                continue
            if team_match_counts[tid] >= last_n:
                continue
            team_match_counts[tid] += 1

            team_lineup = lineups.get(team_key, {})
            players = team_lineup.get("player", [])
            if isinstance(players, dict):
                players = [players]

            # In soccerfixtures lineups, all listed players are starters
            # (substitutes are in the substitutions node, not lineups)
            for p in players:
                pid = str(p.get("id", "")).strip()
                if not pid:
                    continue
                if pid not in player_stats:
                    player_stats[pid] = {"started": 0, "subbed": 0, "appeared_in": 0}
                player_stats[pid]["started"] += 1
                player_stats[pid]["appeared_in"] += 1

        # Check substitutions for sub appearances
        subs = match.get("substitutions", {})
        for team_key, tid in [("localteam", home_id), ("visitorteam", away_id)]:
            if tid not in team_match_counts:
                continue
            team_subs = subs.get(team_key, {})
            sub_list = team_subs.get("substitution", [])
            if isinstance(sub_list, dict):
                sub_list = [sub_list]
            for sub in sub_list:
                pid_in = str(sub.get("player_in_id", "")).strip()
                if pid_in:
                    if pid_in not in player_stats:
                        player_stats[pid_in] = {"started": 0, "subbed": 0, "appeared_in": 0}
                    player_stats[pid_in]["subbed"] += 1
                    player_stats[pid_in]["appeared_in"] += 1

    for pid, s in player_stats.items():
        eligible = s["appeared_in"]
        s["lineup_rate"] = round(s["started"] / eligible, 3) if eligible > 0 else 0.0

    return player_stats


# ── Previous season enrichment ───────────────────────────────────────────────

async def _fetch_previous_season_stats(
    client,
    curr_players: List[Dict],
    league_id: str,
    season_label: str,
) -> List[Dict]:
    """
    Fetch previous-season stats for all players via soccerstats/player/{id}.

    Strategy:
    - Load what's already in MongoDB cache (180-day TTL, prev season is static).
    - Fetch only the remainder from GoalServe API (rate-limited, logged).
    - Persist new fetches back to cache for future runs.

    First run: O(N) API calls (~10 min for 618 EPL players at 60 req/min).
    Subsequent runs: 0 API calls (all loaded from cache).
    """
    if not settings.FETCH_PREV_SEASON_STATS:
        log.info("[2b] FETCH_PREV_SEASON_STATS=False — skipping (100% current season blend)")
        return []

    log.info("[2b] Previous season stats enrichment...")
    player_ids = [str(p["player_id"]) for p in curr_players if p.get("player_id")]

    cached = await load_prev_season_cache(league_id, player_ids)
    uncached_ids = [pid for pid in player_ids if pid not in cached]
    log.info("  Cache: %d hit, %d miss", len(cached), len(uncached_ids))

    freshly_fetched: Dict[str, Dict] = {}
    if uncached_ids:
        eta_s = len(uncached_ids) * 60 // settings.REQUESTS_PER_MINUTE
        log.info(
            "  Fetching %d player profiles from GoalServe (~%ds at %d req/min)...",
            len(uncached_ids), eta_s, settings.REQUESTS_PER_MINUTE,
        )
        for i, pid in enumerate(uncached_ids):
            try:
                profile = await client.get_player_profile(pid)
                prev_stats = extract_prev_season_from_profile(profile, league_id, season_label)
                if prev_stats:
                    freshly_fetched[pid] = prev_stats
            except Exception as e:
                log.debug("Profile fetch failed player_id=%s: %s", pid, e)

            if (i + 1) % 50 == 0:
                log.info("  ... %d/%d profiles fetched", i + 1, len(uncached_ids))

        if freshly_fetched:
            await save_prev_season_cache(league_id, freshly_fetched)

    all_prev = {**cached, **freshly_fetched}
    prev_players = [
        {"player_id": pid, **stats}
        for pid, stats in all_prev.items()
    ]

    matched = len(prev_players)
    log.info(
        "  Previous season data: %d/%d players (%.0f%%) — blend is %.0f%% current / %.0f%% prev",
        matched, len(player_ids),
        100 * matched / len(player_ids) if player_ids else 0,
        settings.CURRENT_SEASON_WEIGHT * 100,
        (1 - settings.CURRENT_SEASON_WEIGHT) * 100,
    )
    return prev_players


# ── Critical fetch with MongoDB cache fallback ──────────────────────────────

async def _critical_fetch(
    feed_name: str,
    league_id: str,
    fetch_fn,
    parse_fn,
):
    """Fetch + parse a critical feed. Fall back to MongoDB cache on failure."""
    try:
        raw = await fetch_fn()
        result = parse_fn(raw)
        await save_goalserve_cache(feed_name, league_id, result)
        return result
    except Exception as live_err:
        log.warning("Live fetch failed for '%s': %s", feed_name, live_err)
        cached = await load_goalserve_cache(feed_name, league_id)
        if cached is not None:
            return cached
        raise RuntimeError(
            f"CRITICAL: Cannot fetch '{feed_name}' for league={league_id} "
            f"and no cache exists. Pipeline aborted. Error: {live_err}"
        )


# ── Full pipeline ────────────────────────────────────────────────────────────

async def run_full_pipeline(
    league_id: str,
    season_label: str = "current",
    nt_league_ids: List[str] = None,
    format_override: Optional[str] = None,
    league_name: str = "",
    prev_standing_rows: Optional[List[Dict]] = None,
) -> Dict:
    """
    Full ETL: Extract → Transform → Price → Save to MongoDB.
    Uses 4 GoalServe API calls (standings, league players, fixtures, injuries).
    """
    nt_league_ids = nt_league_ids or []
    log.info("=" * 72)
    log.info("SPORTINERD ETL v3.0  league_id=%s  season=%s", league_id, season_label)
    log.info("=" * 72)

    client = GoalServeClient()
    fmt = format_override or detect_format(league_id)
    log.info("[format] %s", fmt)

    try:
        # ── [1/4] Standings → team strength ──────────────────────────────────
        log.info("[1/4] Standings & team strength...")
        standing_rows = await _critical_fetch(
            feed_name="standings",
            league_id=league_id,
            fetch_fn=lambda: client.get_standings(league_id),
            parse_fn=extract_standings,
        )
        current_gw = _detect_current_gw(standing_rows)
        team_strength = build_team_strength_map(
            current_rows=standing_rows,
            prev_rows=prev_standing_rows,
            current_gw=current_gw,
        )
        await save_standings_cache(league_id, season_label, standing_rows)
        team_ids = [row["team_id"] for row in standing_rows]
        team_id_set: Set[str] = set(team_ids)
        log.info("  %d teams | GW≈%d", len(team_ids), current_gw)

        # ── [2/4] League players (soccerleague) ─────────────────────────────
        log.info("[2/4] League players (teams + squads + stats)...")
        curr_players = await _critical_fetch(
            feed_name="league_players",
            league_id=league_id,
            fetch_fn=lambda: client.get_league_players(league_id),
            parse_fn=lambda raw: extract_all_players(raw, league_id, "current"),
        )
        log.info("  %d players extracted", len(curr_players))

        # Squad depth from the same player list
        squad_rank_map = _parse_squad_depth(curr_players)
        log.info("  %d players ranked by squad depth", len(squad_rank_map))

        # ── [2b] Previous season stats (soccerstats/player per-player profiles) ─
        prev_players = await _fetch_previous_season_stats(
            client, curr_players, league_id, season_label,
        )
        players = merge_player_seasons(curr_players, prev_players)

        for p in players:
            p["team_strength"] = team_strength.get(str(p.get("team_id", "")), 0.5)

        # ── [3/4] Fixtures → FDR + lineups + form ───────────────────────────
        log.info("[3/4] Fixtures (FDR + lineups + form)...")
        fixtures_by_team: Dict[str, List[Dict]] = {}
        all_lineup_stats: Dict[str, Dict] = {}
        form_lookup: Dict[str, List[Dict]] = {}

        try:
            fixtures_raw = await client.get_fixtures(league_id)

            # FDR: upcoming fixtures
            for tid in team_ids:
                fixtures_by_team[tid] = _extract_upcoming_fixtures(fixtures_raw, tid, n=5)
            log.info("  Upcoming fixtures extracted for %d teams", len(fixtures_by_team))

            # Lineups from completed matches
            completed = _extract_completed_matches(fixtures_raw)
            log.info("  %d completed matches found", len(completed))

            all_lineup_stats = _extract_lineup_data_from_fixtures(completed, team_ids, last_n=10)
            log.info("  %d player lineup records extracted", len(all_lineup_stats))

            # Form from last 5 completed matches
            form_lookup = _extract_form_from_fixtures(completed, team_ids, last_n=5)
            log.info("  %d players with form data", len(form_lookup))

        except Exception as e:
            log.warning("Fixture fetch failed: %s — FDR/form/lineups will use defaults", e)

        # Apply FDR
        for p in players:
            tid = str(p.get("team_id", ""))
            p["fdr_multiplier"] = compute_fdr_multiplier(
                tid, fixtures_by_team.get(tid, []), team_strength,
            )

        # Apply XI probability
        for p in players:
            pid = str(p.get("player_id", ""))
            p["xi_probability"] = compute_xi_probability(
                p,
                lineup_stats=all_lineup_stats.get(pid),
                squad_rank=squad_rank_map.get(pid),
            )

        # Apply form
        for p in players:
            pid = str(p.get("player_id", ""))
            p["recent_match_stats"] = form_lookup.get(pid) or None

        # ── [4/4] Injuries (global feed, filtered) ───────────────────────────
        log.info("[4/4] Injuries...")
        injury_lookup: Dict[str, Dict] = {}
        try:
            injuries_raw = await client.get_injuries()
            injury_lookup = parse_injury_feed(injuries_raw, team_ids=team_id_set)
            await save_injuries_cache(league_id, injury_lookup)
        except Exception as e:
            log.warning("Injury fetch failed: %s — no injury discounts applied", e)

        players, injured_ids = apply_current_injury_status(players, injury_lookup)
        # Historical injury discounts — empty for now (no snapshots yet)
        players = apply_historical_injury_discounts(players, {})
        players = compute_rival_injury_bonuses(players, injured_ids, squad_rank_map)

        # ── NT factors (tournament mode only) ────────────────────────────────
        if fmt == "tournament" and nt_league_ids:
            log.info("[NT] National team factors...")
            # NT enrichment would go here — skipped for league mode
        players = enrich_players_with_nt_factors(players, {}, {}, fmt)

        # ── PRICE ────────────────────────────────────────────────────────────
        log.info("[PRICE] Calibration solver → pricing all players...")
        priced = price_all_players(players, fmt)

        # ── SAVE to MongoDB ──────────────────────────────────────────────────
        calib_meta = {}
        if priced:
            dbg = priced[0].get("_pricing_debug", {})
            calib_meta = {
                "calibration_scale_adj": dbg.get("calibration_scale_adj"),
                "calibration_squad_cost": dbg.get("calibration_squad_cost"),
            }

        run_id = await save_pricing_run(
            league_id=league_id,
            season_label=season_label,
            format_type=fmt,
            player_count=len(priced),
            squad_cost=calib_meta.get("calibration_squad_cost", 0),
            scale_adj=calib_meta.get("calibration_scale_adj", 1.0),
            current_gw=current_gw,
        )

        await save_player_prices(run_id, priced, league_id, season_label)
        await save_teams(league_id, season_label, team_strength, team_ids)

        # ── Sync prices → Fantasy_Global_Livescore ───────────────────────────
        fgl_summary = {"matched": 0, "unmatched": 0, "written": 0}
        try:
            fgl_summary = await FGLSync().sync_pricing(priced, league_id, season_label)
        except Exception as fgl_err:
            log.warning("[FGLSync] Sync failed (non-critical): %s", fgl_err)

        summary = {
            "run_id": run_id,
            "league_id": league_id,
            "season_label": season_label,
            "format_type": fmt,
            "players_priced": len(priced),
            "injured": len(injured_ids),
            "current_gw": current_gw,
            "budget": settings.SQUAD_BUDGET,
            "status": "success",
            "fgl_matched": fgl_summary.get("matched", 0),
            "fgl_unmatched": fgl_summary.get("unmatched", 0),
            "fgl_written": fgl_summary.get("written", 0),
            **calib_meta,
        }
        log.info("ETL COMPLETE: %s", summary)
        log.info("=" * 72)
        return summary

    except Exception as e:
        log.error("Pipeline failed: %s", e, exc_info=True)
        await save_pricing_run(
            league_id=league_id,
            season_label=season_label,
            format_type=fmt,
            player_count=0,
            squad_cost=0,
            scale_adj=1.0,
            current_gw=0,
            status="failed",
            error_message=str(e),
        )
        raise
    finally:
        await client.close()
