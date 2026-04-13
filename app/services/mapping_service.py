# app/services/mapping_service.py — Multi-signal GS↔FGL player mapping
#
# Matches GoalServe players to FGL (SportMonks) players using 5 signals:
#   1. Name (7-tier cascade from fgl_sync)
#   2. Date of birth
#   3. Nationality
#   4. Position
#   5. Height / weight
#
# Each match gets a confidence score (0–1) and tier (HIGH/MEDIUM/LOW).
# Results are stored for review, then committed separately.
# The ONLY field written to FGL on commit: goalserve_player_id.

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from bson import ObjectId

from app.core.config import settings
from app.core.database import get_db
from app.extractors.goalserve import GoalServeClient
from app.loaders.fgl_sync import _norm, _tokens, _CHAR_MAP, FGLSync
from app.transformers.normalizer import normalize_position
from app.utils.logger import get_logger

log = get_logger(__name__)

# ── Confidence weights ────────────────────────────────────────────────────────

NAME_WEIGHT = 0.40
DOB_WEIGHT = 0.30
NATIONALITY_WEIGHT = 0.15
POSITION_WEIGHT = 0.10
PHYSICAL_WEIGHT = 0.05

TIER_HIGH = 0.85
TIER_MEDIUM = 0.55

# Name score by tier
_NAME_SCORE = {
    "T1": NAME_WEIGHT,
    "T2": NAME_WEIGHT,
    "T3": NAME_WEIGHT,
    "T4": NAME_WEIGHT * 0.75,
    "T5": NAME_WEIGHT * 0.75,
    "T6": NAME_WEIGHT * 0.50,
    "T7": NAME_WEIGHT * 0.50,
}

# ── Country aliases ───────────────────────────────────────────────────────────
# Maps common GoalServe nationality strings to their FGL country name or
# fifa_name so that slight naming differences don't break matching.

_COUNTRY_ALIASES: Dict[str, set] = {
    "usa": {"united states", "us", "usa"},
    "united states": {"usa", "us", "united states"},
    "south korea": {"korea republic", "korea, republic of", "south korea", "kor"},
    "korea republic": {"south korea", "korea, republic of", "korea republic", "kor"},
    "north korea": {"korea, democratic people's republic of", "north korea", "prk"},
    "ivory coast": {"cote d'ivoire", "côte d'ivoire", "ivory coast", "civ"},
    "cote d'ivoire": {"ivory coast", "côte d'ivoire", "cote d'ivoire", "civ"},
    "republic of ireland": {"ireland", "republic of ireland", "irl"},
    "ireland": {"republic of ireland", "ireland", "irl"},
    "dr congo": {"congo dr", "democratic republic of the congo", "congo, democratic republic of the", "cod"},
    "congo dr": {"dr congo", "democratic republic of the congo", "cod"},
    "bosnia-herzegovina": {"bosnia and herzegovina", "bosnia-herzegovina", "bih"},
    "bosnia and herzegovina": {"bosnia-herzegovina", "bosnia and herzegovina", "bih"},
    "cape verde": {"cabo verde", "cape verde", "cpv"},
    "cabo verde": {"cape verde", "cabo verde", "cpv"},
    "curacao": {"curaçao", "curacao"},
    "curaçao": {"curacao", "curaçao"},
    "türkiye": {"turkey", "türkiye", "tur"},
    "turkey": {"türkiye", "turkey", "tur"},
    "china pr": {"china", "china pr", "chn"},
    "china": {"china pr", "china", "chn"},
}


def _normalize_country(name: str) -> str:
    """Lowercase, strip accents for comparison."""
    return _norm(name)


def _countries_match(gs_nationality: str, fgl_country: Dict) -> bool:
    """Check if a GS nationality string matches an FGL country record."""
    if not gs_nationality or not fgl_country:
        return False

    gs_n = _normalize_country(gs_nationality)
    fgl_name = _normalize_country(fgl_country.get("name", ""))
    fgl_fifa = _normalize_country(fgl_country.get("fifa_name", ""))
    fgl_iso2 = (fgl_country.get("iso2") or "").lower()
    fgl_iso3 = (fgl_country.get("iso3") or "").lower()

    # Direct match
    if gs_n and gs_n in (fgl_name, fgl_fifa, fgl_iso2, fgl_iso3):
        return True

    # Alias match
    aliases = _COUNTRY_ALIASES.get(gs_n, set())
    if aliases & {fgl_name, fgl_fifa, fgl_iso2, fgl_iso3}:
        return True

    # Substring match as last resort (e.g., "england" in "England")
    if gs_n and fgl_name and (gs_n in fgl_name or fgl_name in gs_n):
        return True

    return False


def _parse_gs_birthdate(raw: str) -> Optional[datetime]:
    """Parse GoalServe birthdate 'dd.mm.yyyy' to datetime."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%Y")
    except ValueError:
        return None


def _dob_match(gs_birthdate: str, fgl_dob: Optional[datetime]) -> bool:
    """Check if GS birthdate matches FGL date_of_birth."""
    if not fgl_dob:
        return False
    gs_dt = _parse_gs_birthdate(gs_birthdate)
    if not gs_dt:
        return False
    # Compare date only (FGL DOB has time component as midnight)
    return gs_dt.date() == fgl_dob.date()


def _physical_match(gs_height: str, gs_weight: str,
                    fgl_height, fgl_weight) -> Tuple[bool, bool]:
    """Returns (height_match, weight_match) booleans."""
    h_match = w_match = False
    try:
        if gs_height and fgl_height:
            h_diff = abs(float(gs_height) - float(fgl_height))
            h_match = h_diff <= 2
    except (ValueError, TypeError):
        pass
    try:
        if gs_weight and fgl_weight:
            w_diff = abs(float(gs_weight) - float(fgl_weight))
            w_match = w_diff <= 3
    except (ValueError, TypeError):
        pass
    return h_match, w_match


def _tier_label(confidence: float) -> str:
    if confidence >= TIER_HIGH:
        return "high"
    elif confidence >= TIER_MEDIUM:
        return "medium"
    return "low"


def _name_score_for_tier(tier_label: str) -> float:
    """Get name score from the match tier label (e.g. 'T1_display_exact')."""
    for prefix, score in _NAME_SCORE.items():
        if tier_label.startswith(prefix):
            return score
    return 0.0


# ── Mapping Service ───────────────────────────────────────────────────────────

class MappingService:

    def __init__(self):
        self.gs = GoalServeClient()
        self._fgl_sync = FGLSync()

    def _fgl_db(self):
        return get_db().client[settings.FGL_DB_NAME]

    def _pricing_db(self):
        return get_db()

    # ── Country lookup ─────────────────────────────────────────────────────────

    async def _load_country_lookup(self) -> Dict[str, Dict]:
        """Build {nationality_id_str: {name, iso2, iso3, fifa_name}} lookup."""
        fgl = self._fgl_db()
        cursor = fgl.countries.find({}, {
            "_id": 1, "name": 1, "iso2": 1, "iso3": 1, "fifa_name": 1,
        })
        lookup = {}
        async for doc in cursor:
            lookup[str(doc["_id"])] = {
                "name": doc.get("name", ""),
                "iso2": doc.get("iso2", ""),
                "iso3": doc.get("iso3", ""),
                "fifa_name": doc.get("fifa_name", ""),
            }
        log.info("[Mapping] Loaded %d countries for nationality lookup", len(lookup))
        return lookup

    # ── FGL player loading ─────────────────────────────────────────────────────

    async def _load_fgl_players(self) -> List[Dict]:
        """Load FGL players with all matching-relevant fields."""
        fgl = self._fgl_db()
        cursor = fgl.players.find({}, {
            "_id": 1, "api_player_id": 1,
            "name": 1, "display_name": 1, "common_name": 1,
            "first_name": 1, "last_name": 1,
            "position": 1, "position_group": 1,
            "date_of_birth": 1, "nationality_id": 1,
            "height": 1, "weight": 1,
            "goalserve_player_id": 1,
        })
        players = await cursor.to_list(length=None)
        log.info("[Mapping] Loaded %d FGL players", len(players))
        return players

    # ── Profile cache ──────────────────────────────────────────────────────────

    async def _load_profile_cache(self, player_id: str) -> Optional[Dict]:
        """Load a cached GS player profile."""
        db = self._pricing_db()
        doc = await db.goalserve_cache.find_one({
            "feed_name": "player_profile",
            "league_id": player_id,
            "ttl_expires_at": {"$gt": datetime.now(timezone.utc)},
        })
        return doc.get("data") if doc else None

    async def _save_profile_cache(self, player_id: str, data: Dict) -> None:
        """Cache a GS player profile."""
        db = self._pricing_db()
        ttl_days = settings.MAPPING_PROFILE_CACHE_DAYS
        await db.goalserve_cache.update_one(
            {"feed_name": "player_profile", "league_id": player_id},
            {"$set": {
                "data": data,
                "fetched_at": datetime.now(timezone.utc),
                "ttl_expires_at": datetime.now(timezone.utc) + timedelta(days=ttl_days),
            }},
            upsert=True,
        )

    async def _fetch_gs_profile(self, gs_player_id: str) -> Tuple[Optional[Dict], bool]:
        """Fetch GS profile with cache-first strategy. Returns (data, from_cache)."""
        cached = await self._load_profile_cache(gs_player_id)
        if cached:
            return cached, True

        try:
            data = await self.gs.get_player_profile(gs_player_id)
            await self._save_profile_cache(gs_player_id, data)
            return data, False
        except Exception as e:
            log.warning("[Mapping] GS profile fetch failed for %s: %s", gs_player_id, e)
            return None, False

    # ── Scoring ────────────────────────────────────────────────────────────────

    def _score_match(
        self,
        name_tier: str,
        gs_profile: Optional[Dict],
        fgl_doc: Dict,
        country_lookup: Dict[str, Dict],
    ) -> Dict:
        """Compute multi-signal confidence score for a candidate match."""
        signals = {
            "name_tier": name_tier,
            "name_score": 0.0,
            "dob_match": False,
            "dob_score": 0.0,
            "nationality_match": False,
            "nationality_score": 0.0,
            "position_match": False,
            "position_score": 0.0,
            "physical_match": False,
            "physical_score": 0.0,
        }

        # Name score
        signals["name_score"] = _name_score_for_tier(name_tier)

        if gs_profile:
            player_node = gs_profile.get("players", {}).get("player", {})

            # DOB
            gs_bdate = player_node.get("birthdate", "")
            fgl_dob = fgl_doc.get("date_of_birth")
            if _dob_match(gs_bdate, fgl_dob):
                signals["dob_match"] = True
                signals["dob_score"] = DOB_WEIGHT

            # Nationality
            gs_nat = player_node.get("nationality") or player_node.get("birthcountry") or ""
            fgl_nat_id = fgl_doc.get("nationality_id")
            if fgl_nat_id:
                fgl_country = country_lookup.get(str(fgl_nat_id), {})
                if _countries_match(gs_nat, fgl_country):
                    signals["nationality_match"] = True
                    signals["nationality_score"] = NATIONALITY_WEIGHT

            # Position
            gs_pos_raw = player_node.get("position", "")
            gs_pos = normalize_position(gs_pos_raw)
            fgl_pos = (fgl_doc.get("position_group") or "").upper()
            if gs_pos and fgl_pos and gs_pos == fgl_pos:
                signals["position_match"] = True
                signals["position_score"] = POSITION_WEIGHT

            # Physical
            gs_h = player_node.get("height", "")
            gs_w = player_node.get("weight", "")
            fgl_h = fgl_doc.get("height")
            fgl_w = fgl_doc.get("weight")
            h_match, w_match = _physical_match(gs_h, gs_w, fgl_h, fgl_w)
            if h_match and w_match:
                signals["physical_match"] = True
                signals["physical_score"] = PHYSICAL_WEIGHT
            elif h_match or w_match:
                signals["physical_match"] = True
                signals["physical_score"] = PHYSICAL_WEIGHT / 2

        confidence = (
            signals["name_score"]
            + signals["dob_score"]
            + signals["nationality_score"]
            + signals["position_score"]
            + signals["physical_score"]
        )
        return {
            "confidence": round(confidence, 3),
            "tier": _tier_label(confidence),
            "signals": signals,
        }

    # ── DOB fallback index ─────────────────────────────────────────────────────

    def _build_dob_index(
        self, fgl_players: List[Dict]
    ) -> Dict[str, List[Dict]]:
        """Build index: 'YYYY-MM-DD|POS' → list of FGL docs."""
        idx: Dict[str, List[Dict]] = defaultdict(list)
        for p in fgl_players:
            dob = p.get("date_of_birth")
            pos = (p.get("position_group") or "").upper()
            if dob and pos:
                key = f"{dob.strftime('%Y-%m-%d')}|{pos}"
                idx[key].append(p)
        return idx

    def _fallback_dob_match(
        self,
        gs_profile: Dict,
        dob_index: Dict[str, List[Dict]],
        country_lookup: Dict[str, Dict],
    ) -> Optional[Tuple[Dict, Dict]]:
        """Try to match an unmatched GS player by DOB + position."""
        player_node = gs_profile.get("players", {}).get("player", {})
        gs_bdate = player_node.get("birthdate", "")
        gs_dt = _parse_gs_birthdate(gs_bdate)
        if not gs_dt:
            return None

        gs_pos = normalize_position(player_node.get("position", ""))
        key = f"{gs_dt.strftime('%Y-%m-%d')}|{gs_pos}"
        candidates = dob_index.get(key, [])

        if len(candidates) == 1:
            fgl_doc = candidates[0]
            # Score it — name_score is 0 since name didn't match, but DOB+pos matched
            signals = {
                "name_tier": "fallback_dob",
                "name_score": 0.0,
                "dob_match": True,
                "dob_score": DOB_WEIGHT,
                "nationality_match": False,
                "nationality_score": 0.0,
                "position_match": True,
                "position_score": POSITION_WEIGHT,
                "physical_match": False,
                "physical_score": 0.0,
            }
            # Also check nationality
            gs_nat = player_node.get("nationality") or player_node.get("birthcountry") or ""
            fgl_nat_id = fgl_doc.get("nationality_id")
            if fgl_nat_id:
                fgl_country = country_lookup.get(str(fgl_nat_id), {})
                if _countries_match(gs_nat, fgl_country):
                    signals["nationality_match"] = True
                    signals["nationality_score"] = NATIONALITY_WEIGHT

            confidence = sum(signals[k] for k in signals if k.endswith("_score"))
            return fgl_doc, {
                "confidence": round(confidence, 3),
                "tier": _tier_label(confidence),
                "signals": signals,
            }

        # Multiple candidates with same DOB+pos — try nationality to disambiguate
        if len(candidates) > 1:
            gs_nat = player_node.get("nationality") or player_node.get("birthcountry") or ""
            if gs_nat:
                nat_filtered = []
                for c in candidates:
                    fgl_nat_id = c.get("nationality_id")
                    if fgl_nat_id:
                        fgl_country = country_lookup.get(str(fgl_nat_id), {})
                        if _countries_match(gs_nat, fgl_country):
                            nat_filtered.append(c)
                if len(nat_filtered) == 1:
                    fgl_doc = nat_filtered[0]
                    signals = {
                        "name_tier": "fallback_dob_nat",
                        "name_score": 0.0,
                        "dob_match": True,
                        "dob_score": DOB_WEIGHT,
                        "nationality_match": True,
                        "nationality_score": NATIONALITY_WEIGHT,
                        "position_match": True,
                        "position_score": POSITION_WEIGHT,
                        "physical_match": False,
                        "physical_score": 0.0,
                    }
                    confidence = sum(signals[k] for k in signals if k.endswith("_score"))
                    return fgl_doc, {
                        "confidence": round(confidence, 3),
                        "tier": _tier_label(confidence),
                        "signals": signals,
                    }

        return None

    # ── Main pipeline ──────────────────────────────────────────────────────────

    async def run_mapping(
        self,
        job_id: str,
        league_id: str,
        batch_size: int = 10,
    ):
        """Full mapping pipeline — called as background task."""
        pricing_db = self._pricing_db()

        try:
            # ── Phase 1: Bulk name matching ────────────────────────────────────
            await pricing_db.player_mapping_jobs.update_one(
                {"_id": job_id},
                {"$set": {"status": "name_matching"}},
            )

            # Fetch GS league players (single API call)
            league_data = await self.gs.get_league_players(league_id)
            gs_players = self._extract_gs_players(league_data)
            log.info("[Mapping] Extracted %d GS players for league %s",
                     len(gs_players), league_id)

            # Load FGL data
            fgl_players = await self._load_fgl_players()
            country_lookup = await self._load_country_lookup()

            # Build name indexes using FGLSync internals
            self._fgl_sync._fgl_cache = fgl_players
            self._fgl_sync._idx_display = defaultdict(list)
            self._fgl_sync._idx_name = defaultdict(list)
            self._fgl_sync._idx_tokens = defaultdict(list)
            self._fgl_sync._idx_last = defaultdict(list)

            for p in fgl_players:
                dn = p.get("display_name") or p.get("name") or ""
                fn = p.get("name") or ""
                ln = p.get("last_name") or ""

                self._fgl_sync._idx_display[_norm(dn)].append(p)
                self._fgl_sync._idx_name[_norm(fn)].append(p)

                ts = _tokens(dn)
                if ts:
                    self._fgl_sync._idx_tokens[ts].append(p)

                last_source = _norm(ln) or _norm(dn)
                last_parts = last_source.split()
                if last_parts:
                    self._fgl_sync._idx_last[last_parts[-1]].append(p)

            # Run name matching
            name_matches: Dict[str, Tuple[Optional[Dict], str]] = {}
            for gs in gs_players:
                fgl_doc, method = self._fgl_sync._match(gs)
                name_matches[gs["id"]] = (fgl_doc, method)

            # Build DOB fallback index
            dob_index = self._build_dob_index(fgl_players)

            await pricing_db.player_mapping_jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "status": "fetching_profiles",
                    "total": len(gs_players),
                }},
            )

            # ── Phase 2: Profile enrichment (batched, rate-limited) ────────────
            gs_profiles: Dict[str, Optional[Dict]] = {}
            profiles_cached = 0
            profiles_fetched = 0

            for i in range(0, len(gs_players), batch_size):
                batch = gs_players[i:i + batch_size]
                tasks = [self._fetch_gs_profile(p["id"]) for p in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for gs_p, result in zip(batch, results):
                    if isinstance(result, Exception):
                        log.warning("[Mapping] Profile fetch error for %s: %s",
                                    gs_p["id"], result)
                        gs_profiles[gs_p["id"]] = None
                    else:
                        data, from_cache = result
                        gs_profiles[gs_p["id"]] = data
                        if from_cache:
                            profiles_cached += 1
                        else:
                            profiles_fetched += 1

                # Update progress
                processed = min(i + batch_size, len(gs_players))
                await pricing_db.player_mapping_jobs.update_one(
                    {"_id": job_id},
                    {"$set": {
                        "profiles_fetched": profiles_fetched,
                        "profiles_cached": profiles_cached,
                        "processed": processed,
                    }},
                )

            # ── Phase 3: Multi-signal scoring ──────────────────────────────────
            await pricing_db.player_mapping_jobs.update_one(
                {"_id": job_id},
                {"$set": {"status": "scoring"}},
            )

            results_docs = []
            tier_counts = {"high": 0, "medium": 0, "low": 0, "unmatched": 0}

            for gs in gs_players:
                gs_id = gs["id"]
                gs_name = gs.get("name", "")
                gs_team = gs.get("team_name", "")
                fgl_doc, name_method = name_matches[gs_id]
                gs_profile = gs_profiles.get(gs_id)

                if fgl_doc:
                    # Score the name-matched candidate
                    score_result = self._score_match(
                        name_method, gs_profile, fgl_doc, country_lookup
                    )
                    result_doc = {
                        "job_id": job_id,
                        "gs_id": gs_id,
                        "gs_name": gs_name,
                        "gs_team": gs_team,
                        "fgl_id": str(fgl_doc["_id"]),
                        "fgl_api_player_id": fgl_doc.get("api_player_id"),
                        "fgl_name": fgl_doc.get("display_name") or fgl_doc.get("name", ""),
                        "confidence": score_result["confidence"],
                        "tier": score_result["tier"],
                        "signals": score_result["signals"],
                        "committed": False,
                    }
                    # Attach profile summary
                    if gs_profile:
                        pn = gs_profile.get("players", {}).get("player", {})
                        result_doc["gs_profile"] = {
                            "birthdate": pn.get("birthdate"),
                            "nationality": pn.get("nationality"),
                            "height": pn.get("height"),
                            "weight": pn.get("weight"),
                        }
                    tier_counts[score_result["tier"]] += 1

                elif gs_profile:
                    # No name match — try DOB fallback
                    fallback = self._fallback_dob_match(
                        gs_profile, dob_index, country_lookup
                    )
                    if fallback:
                        fb_fgl, fb_score = fallback
                        pn = gs_profile.get("players", {}).get("player", {})
                        result_doc = {
                            "job_id": job_id,
                            "gs_id": gs_id,
                            "gs_name": gs_name,
                            "gs_team": gs_team,
                            "fgl_id": str(fb_fgl["_id"]),
                            "fgl_api_player_id": fb_fgl.get("api_player_id"),
                            "fgl_name": fb_fgl.get("display_name") or fb_fgl.get("name", ""),
                            "confidence": fb_score["confidence"],
                            "tier": fb_score["tier"],
                            "signals": fb_score["signals"],
                            "gs_profile": {
                                "birthdate": pn.get("birthdate"),
                                "nationality": pn.get("nationality"),
                                "height": pn.get("height"),
                                "weight": pn.get("weight"),
                            },
                            "committed": False,
                        }
                        tier_counts[fb_score["tier"]] += 1
                    else:
                        pn = gs_profile.get("players", {}).get("player", {})
                        result_doc = {
                            "job_id": job_id,
                            "gs_id": gs_id,
                            "gs_name": gs_name,
                            "gs_team": gs_team,
                            "fgl_id": None,
                            "fgl_api_player_id": None,
                            "fgl_name": None,
                            "confidence": 0.0,
                            "tier": "unmatched",
                            "signals": {},
                            "gs_profile": {
                                "birthdate": pn.get("birthdate"),
                                "nationality": pn.get("nationality"),
                                "height": pn.get("height"),
                                "weight": pn.get("weight"),
                            },
                            "committed": False,
                        }
                        tier_counts["unmatched"] += 1
                else:
                    # No name match, no profile
                    result_doc = {
                        "job_id": job_id,
                        "gs_id": gs_id,
                        "gs_name": gs_name,
                        "gs_team": gs_team,
                        "fgl_id": None,
                        "fgl_api_player_id": None,
                        "fgl_name": None,
                        "confidence": 0.0,
                        "tier": "unmatched",
                        "signals": {},
                        "committed": False,
                    }
                    tier_counts["unmatched"] += 1

                results_docs.append(result_doc)

            # ── Phase 4: Persist results ───────────────────────────────────────
            if results_docs:
                await pricing_db.player_mapping_results.insert_many(results_docs)

            await pricing_db.player_mapping_jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "status": "completed",
                    "total": len(gs_players),
                    "profiles_fetched": profiles_fetched,
                    "profiles_cached": profiles_cached,
                    "high_confidence": tier_counts["high"],
                    "medium_confidence": tier_counts["medium"],
                    "low_confidence": tier_counts["low"],
                    "unmatched": tier_counts["unmatched"],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }},
            )

            log.info(
                "[Mapping] Job %s completed — high=%d medium=%d low=%d unmatched=%d",
                job_id, tier_counts["high"], tier_counts["medium"],
                tier_counts["low"], tier_counts["unmatched"],
            )

        except Exception as e:
            log.error("[Mapping] Job %s failed: %s", job_id, e, exc_info=True)
            try:
                await pricing_db.player_mapping_jobs.update_one(
                    {"_id": job_id},
                    {"$set": {
                        "status": "failed",
                        "error": str(e),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
            except Exception:
                pass
        finally:
            await self.gs.close()

    # ── GS player extraction ───────────────────────────────────────────────────

    def _extract_gs_players(self, feed_data: Dict) -> List[Dict]:
        """Extract {id, name, position, team_name} from soccerleague response."""
        league = feed_data.get("league", {})
        teams = league.get("team", [])
        if isinstance(teams, dict):
            teams = [teams]

        players = []
        seen = set()
        for team in teams:
            tname = team.get("name", "")
            squad = team.get("squad") or {}
            squad_players = squad.get("player", [])
            if isinstance(squad_players, dict):
                squad_players = [squad_players]

            for p in squad_players:
                pid = str(p.get("id", "")).strip()
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                players.append({
                    "id": pid,
                    "name": p.get("name", ""),
                    "position": normalize_position(p.get("position")),
                    "team_name": tname,
                })
        return players

    # ── Commit ─────────────────────────────────────────────────────────────────

    async def commit_mappings(
        self,
        job_id: str,
        confidence_threshold: float = 0.85,
        overrides: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Write goalserve_player_id to FGL player docs for matches above threshold.
        overrides: [{gs_id: "...", fgl_id: "..."}] for manual corrections.
        """
        from pymongo import UpdateOne

        pricing_db = self._pricing_db()
        fgl_db = self._fgl_db()

        # Load results above threshold
        cursor = pricing_db.player_mapping_results.find({
            "job_id": job_id,
            "fgl_id": {"$ne": None},
            "confidence": {"$gte": confidence_threshold},
            "committed": False,
        })
        results = await cursor.to_list(length=None)

        # Apply overrides
        override_map = {}
        if overrides:
            for ov in overrides:
                override_map[ov["gs_id"]] = ov["fgl_id"]

        ops = []
        committed_gs_ids = []

        for r in results:
            fgl_id = override_map.pop(r["gs_id"], r["fgl_id"])
            ops.append(UpdateOne(
                {"_id": ObjectId(fgl_id)},
                {"$set": {"goalserve_player_id": int(r["gs_id"])}},
            ))
            committed_gs_ids.append(r["gs_id"])

        # Process remaining overrides (manual matches not in threshold results)
        for gs_id, fgl_id in override_map.items():
            ops.append(UpdateOne(
                {"_id": ObjectId(fgl_id)},
                {"$set": {"goalserve_player_id": int(gs_id)}},
            ))
            committed_gs_ids.append(gs_id)

        committed = 0
        if ops:
            result = await fgl_db.players.bulk_write(ops, ordered=False)
            committed = result.modified_count + result.upserted_count

            # Mark as committed
            await pricing_db.player_mapping_results.update_many(
                {"job_id": job_id, "gs_id": {"$in": committed_gs_ids}},
                {"$set": {"committed": True}},
            )

            # Update job
            await pricing_db.player_mapping_jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "committed": committed,
                    "committed_at": datetime.now(timezone.utc).isoformat(),
                }},
            )

        log.info("[Mapping] Committed %d mappings for job %s", committed, job_id)
        return {"committed": committed, "total_ops": len(ops)}
