# app/transformers/normalizer.py (v3.0)
#
# Adapted from v24 normalizer.py for actual GoalServe JSON structure.
# Key changes:
#   - Position mapping: G/D/M/A → GK/DEF/MID/FWD (GoalServe single-letter codes)
#   - Field names: "appearences" (GoalServe typo), "keyPasses", "shotsOn", etc.
#   - Targeted extraction: league.team[].squad.player[] (not recursive walk)
#   - Standings: standings.tournament.team[] with overall/home/away/total structure

from typing import Dict, List, Optional

from app.utils.logger import get_logger

log = get_logger(__name__)


# ── Custom exceptions ────────────────────────────────────────────────────────

class SchemaError(Exception):
    """Raised when GoalServe API structure has changed and fields are missing."""


# ── safe_float ───────────────────────────────────────────────────────────────

def safe_float(val, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Position normalisation ───────────────────────────────────────────────────

_POS_MAP: Dict[str, str] = {
    # GoalServe single-letter codes
    "g": "GK", "d": "DEF", "m": "MID", "a": "FWD",
    # Standard verbose names
    "goalkeeper": "GK", "gk": "GK", "keeper": "GK",
    "defender": "DEF", "centre-back": "DEF", "center-back": "DEF",
    "cb": "DEF", "left back": "DEF", "right back": "DEF",
    "left-back": "DEF", "right-back": "DEF", "lb": "DEF", "rb": "DEF",
    "wing back": "DEF", "wing-back": "DEF", "wb": "DEF",
    "sweeper": "DEF", "libero": "DEF",
    "midfielder": "MID", "central midfielder": "MID", "cm": "MID",
    "defensive midfielder": "MID", "dm": "MID", "cdm": "MID",
    "attacking midfielder": "MID", "am": "MID", "cam": "MID",
    "left midfielder": "MID", "right midfielder": "MID",
    "left winger": "MID", "right winger": "MID",
    "lm": "MID", "rm": "MID", "lw": "MID", "rw": "MID",
    "forward": "FWD", "striker": "FWD", "centre-forward": "FWD",
    "center-forward": "FWD", "cf": "FWD",
    "second striker": "FWD", "ss": "FWD", "st": "FWD",
    "inside forward": "FWD", "attacker": "FWD",
}


def normalize_position(raw: Optional[str]) -> str:
    if not raw:
        return "MID"
    clean = raw.lower().strip()
    if clean in _POS_MAP:
        return _POS_MAP[clean]
    for key, val in _POS_MAP.items():
        if key in clean:
            return val
    log.debug("Unknown position '%s' — defaulting MID", raw)
    return "MID"


# ── Schema validation (raw GoalServe nodes) ─────────────────────────────────

def validate_raw_player_schema(
    raw_nodes: List[Dict],
    feed_label: str = "unknown",
    error_threshold: float = 0.50,
    sample_size: int = 20,
) -> None:
    """
    Inspect RAW GoalServe JSON nodes BEFORE normalisation.
    Checks for position key and stats presence (GoalServe soccerleague format).
    """
    if not raw_nodes:
        log.warning("Schema validation: empty raw batch for feed '%s'", feed_label)
        return

    sample = raw_nodes[:sample_size]
    n = len(sample)
    failures: Dict[str, int] = {}

    for raw in sample:
        if "position" not in raw and "pos" not in raw:
            failures["position"] = failures.get("position", 0) + 1

        # GoalServe soccerleague uses flat keys: appearences, goals, minutes, etc.
        has_stats = any(
            k in raw
            for k in ("appearences", "appearances", "goals", "minutes", "rating")
        )
        if not has_stats:
            failures["stats_block"] = failures.get("stats_block", 0) + 1

    for key, count in failures.items():
        frac = count / n
        if frac > error_threshold:
            raise SchemaError(
                f"Schema drift in raw feed '{feed_label}': '{key}' absent in "
                f"{count}/{n} ({frac:.0%}) nodes. Pipeline aborted."
            )
        elif frac > 0:
            log.warning(
                "Schema advisory [%s]: '%s' absent in %d/%d nodes.",
                feed_label, key, count, n,
            )

    log.info("RAW schema validation OK [%s]: %d nodes sampled.", feed_label, n)


# ── Player normalisation (GoalServe soccerleague format) ─────────────────────

def normalize_player(
    raw: Dict,
    team_id: str = "",
    team_name: str = "",
    league_id: str = "",
    season_label: str = "current",
) -> Optional[Dict]:
    """
    Normalize a single player dict from GoalServe soccerleague response.
    GoalServe field names: appearences (typo), keyPasses, shotsOn, etc.
    """
    player_id = str(raw.get("id") or raw.get("player_id") or "").strip()
    if not player_id:
        return None

    name = raw.get("name") or raw.get("common_name") or ""
    if not name:
        return None

    position = normalize_position(raw.get("position") or raw.get("pos"))

    # GoalServe uses "appearences" (typo) — handle both spellings
    appearances = safe_float(raw.get("appearences") or raw.get("appearances"))
    minutes = safe_float(raw.get("minutes"))
    goals = safe_float(raw.get("goals"))
    assists = safe_float(raw.get("assists"))
    yellow_cards = safe_float(raw.get("yellowcards") or raw.get("yellow_cards"))
    red_cards = safe_float(raw.get("redcards") or raw.get("red_cards"))
    saves = safe_float(raw.get("saves"))
    key_passes = safe_float(raw.get("keyPasses") or raw.get("key_passes"))
    shots_on_target = safe_float(raw.get("shotsOn") or raw.get("shots_on_target"))
    lineups_count = safe_float(raw.get("lineups"))
    rating = safe_float(raw.get("rating"))
    squad_number = str(raw.get("number") or "")

    avg_minutes = (minutes / appearances) if appearances > 0 else 0.0
    lineup_rate = (lineups_count / appearances) if appearances > 0 else 0.0

    # GoalServe doesn't provide clean_sheets per player directly.
    # For GK/DEF, goalsConceded=0 in a match = clean sheet, but that's per-match data.
    # Use goalsConceded as a proxy: fewer goals conceded → more clean sheets (approximate).
    clean_sheets = safe_float(raw.get("clean_sheets"))  # may be 0 if not in feed

    return {
        "player_id": player_id,
        "name": name,
        "position": position,
        "team_id": team_id or str(raw.get("team_id") or raw.get("teamid") or ""),
        "team_name": team_name or raw.get("team_name") or "",
        "squad_number": squad_number,
        "league_id": league_id,
        "season": season_label,

        "goals": goals,
        "assists": assists,
        "clean_sheets": clean_sheets,
        "saves": saves,
        "yellow_cards": yellow_cards,
        "red_cards": red_cards,
        "minutes_played": minutes,
        "appearances": appearances,
        "avg_minutes": round(avg_minutes, 1),
        "shots_on_target": shots_on_target,
        "key_passes": key_passes,
        "rating": rating,

        # XI probability signals
        "lineups_count": lineups_count,
        "lineup_rate": round(lineup_rate, 3),
        "substitute_in": safe_float(raw.get("substitute_in")),
        "substitute_out": safe_float(raw.get("substitute_out")),

        # Additional stats
        "tackles": safe_float(raw.get("tackles")),
        "interceptions": safe_float(raw.get("interceptions")),
        "clearances": safe_float(raw.get("clearances")),
        "blocks": safe_float(raw.get("blocks")),
        "passes": safe_float(raw.get("passes")),
        "pass_accuracy": safe_float(raw.get("pAccuracy")),
        "dribble_attempts": safe_float(raw.get("dribbleAttempts")),
        "dribble_success": safe_float(raw.get("dribbleSucc")),
        "duels_total": safe_float(raw.get("duelsTotal")),
        "duels_won": safe_float(raw.get("duelsWon")),
        "is_injured": str(raw.get("injured", "")).lower() == "true",

        # Pricing placeholders (filled by pipeline)
        "price": None,
        "price_tier": None,
        "xi_probability": None,
        "injury_status": None,
        "injury_discount": 1.0,
        "rival_injury_bonus": 0.0,
        "rival_injury_bonus_gbp": 0.0,
        "nt_factor": 1.0,
        "fdr_multiplier": 1.0,
        "team_strength": 0.5,
        "previous_season": None,
        "has_previous_data": False,
        "recent_match_stats": None,
    }


# ── Targeted player extraction (soccerleague feed) ──────────────────────────

def extract_all_players(
    feed_data: Dict,
    league_id: str = "",
    season_label: str = "current",
) -> List[Dict]:
    """
    Extract all players from GoalServe soccerleague/{id} response.

    Structure: response["league"]["team"][] → each team has squad.player[]
    This is a targeted extraction, not a recursive walk.
    """
    league = feed_data.get("league", {})
    teams = league.get("team", [])
    if isinstance(teams, dict):
        teams = [teams]

    raw_nodes: List[Dict] = []
    team_info: List[tuple] = []  # (team_id, team_name) parallel to raw_nodes

    for team in teams:
        tid = str(team.get("id", ""))
        tname = team.get("name", "")
        squad = team.get("squad") or {}
        players = squad.get("player", [])
        if isinstance(players, dict):
            players = [players]
        for p in players:
            raw_nodes.append(p)
            team_info.append((tid, tname))

    log.info("Collected %d raw player nodes from %d teams", len(raw_nodes), len(teams))

    # Validate raw structure before normalization
    validate_raw_player_schema(raw_nodes, feed_label=f"soccerleague_{season_label}")

    # Normalize
    players: List[Dict] = []
    seen_ids = set()
    for raw, (tid, tname) in zip(raw_nodes, team_info):
        p = normalize_player(raw, team_id=tid, team_name=tname,
                             league_id=league_id, season_label=season_label)
        if p and p["player_id"] not in seen_ids:
            seen_ids.add(p["player_id"])
            players.append(p)

    log.info("Extracted %d unique players (season=%s)", len(players), season_label)
    return players


# ── Previous season extraction (soccerstats/player profile) ─────────────────

def _current_gs_season() -> str:
    """Derive current GoalServe season string from today's date.
    E.g. March 2026 → '2025/2026'.
    """
    from datetime import datetime
    now = datetime.now()
    start_year = now.year if now.month >= 8 else now.year - 1
    return f"{start_year}/{start_year + 1}"


def extract_prev_season_from_profile(
    profile_data: Dict,
    league_id: str,
    season_label: str = "current",
) -> Optional[Dict]:
    """
    Extract previous-season stats from a soccerstats/player/{id} profile.

    Looks through statistic.club[] entries, skips the current season, and
    returns the most recent prior season — preferring the same league, but
    falling back to any league if the player transferred.

    Returns a dict compatible with _perf_score() / compute_blended_perf_score(),
    or None if no usable prior-season record exists.
    """
    player_node = profile_data.get("players", {}).get("player", {})
    club_entries = player_node.get("statistic", {}).get("club", [])
    if isinstance(club_entries, dict):
        club_entries = [club_entries]
    if not club_entries:
        return None

    # Determine current GoalServe season string to exclude it
    current_gs = _current_gs_season()

    def _season_start_year(entry: Dict) -> int:
        parts = str(entry.get("season", "0/0")).split("/")
        try:
            return int(parts[0])
        except ValueError:
            return 0

    sorted_entries = sorted(club_entries, key=_season_start_year, reverse=True)

    prev_same_league: Optional[Dict] = None
    prev_any_league: Optional[Dict] = None

    for entry in sorted_entries:
        if entry.get("season") == current_gs:
            continue
        if entry.get("league_id") == league_id and prev_same_league is None:
            prev_same_league = entry
        if prev_any_league is None:
            prev_any_league = entry

    best = prev_same_league or prev_any_league
    if not best:
        return None

    apps = safe_float(best.get("appearences") or best.get("appearances"))
    if apps < 1:
        return None

    minutes_total = safe_float(best.get("minutes"))
    return {
        "appearances": apps,
        "goals": safe_float(best.get("goals")),
        "assists": safe_float(best.get("assists")),
        "clean_sheets": safe_float(best.get("clean_sheets")),
        "saves": safe_float(best.get("saves")),
        "yellow_cards": safe_float(best.get("yellowcards") or best.get("yellow_cards")),
        "red_cards": safe_float(best.get("redcards") or best.get("red_cards")),
        "avg_minutes": round(minutes_total / apps, 1) if apps > 0 else 0.0,
        "shots_on_target": safe_float(best.get("shotsOn")),
        "key_passes": safe_float(best.get("keyPasses")),
        "_prev_season": best.get("season", ""),
        "_prev_league_id": best.get("league_id", ""),
    }


# ── Standings extraction ─────────────────────────────────────────────────────

def extract_standings(standings_data: Dict) -> List[Dict]:
    """
    Extract standings from GoalServe standings/{id}.xml response.
    Structure: standings.tournament.team[]
    Each team has: id, name, position, overall{gp,w,d,l,gs,ga}, total{p,gd}, recent_form
    """
    standings = standings_data.get("standings", {})
    tournament = standings.get("tournament", {})
    teams = tournament.get("team", [])
    if isinstance(teams, dict):
        teams = [teams]

    season = tournament.get("season", "")

    rows: List[Dict] = []
    for team in teams:
        overall = team.get("overall", {})
        total = team.get("total", {})
        rows.append({
            "team_id": str(team.get("id", "")),
            "team_name": team.get("name", ""),
            "position": int(safe_float(team.get("position"), 99)),
            "points": safe_float(total.get("p")),
            "won": safe_float(overall.get("w")),
            "drawn": safe_float(overall.get("d")),
            "lost": safe_float(overall.get("l")),
            "gf": safe_float(overall.get("gs")),
            "ga": safe_float(overall.get("ga")),
            "gd": safe_float(total.get("gd")),
            "games_played": safe_float(overall.get("gp")),
            "recent_form": team.get("recent_form", ""),
            "season": season,
        })

    rows.sort(key=lambda r: r["position"])
    log.info("Extracted %d standing rows", len(rows))
    return rows


# ── Team strength map ────────────────────────────────────────────────────────

def _rows_to_strength(rows: List[Dict]) -> Dict[str, float]:
    if not rows:
        return {}
    max_pts = max((r.get("points", 0) for r in rows), default=1) or 1
    return {
        r["team_id"]: round(r.get("points", 0) / max_pts, 3)
        for r in rows if r.get("team_id")
    }


def build_team_strength_map(
    current_rows: List[Dict],
    prev_rows: Optional[List[Dict]] = None,
    current_gw: int = 38,
) -> Dict[str, float]:
    """
    Points-based team strength with cold-start blend (GW0→GW20).
    Only iterates over current season teams (relegated teams excluded).
    """
    from app.core.config import settings

    curr_map = _rows_to_strength(current_rows)

    if not prev_rows or current_gw >= settings.TEAM_STRENGTH_BLEND_GW:
        return curr_map

    prev_map = _rows_to_strength(prev_rows)
    w_curr = current_gw / settings.TEAM_STRENGTH_BLEND_GW
    w_prev = 1.0 - w_curr

    blended: Dict[str, float] = {}
    for tid in curr_map:
        c = curr_map[tid]
        p = prev_map.get(tid)
        if p is not None:
            blended[tid] = round(w_curr * c + w_prev * p, 3)
        else:
            blended[tid] = round(w_curr * c + w_prev * 0.3, 3)

    log.info(
        "Team strength: GW%d → w_curr=%.0f%% w_prev=%.0f%% | %d teams",
        current_gw, w_curr * 100, w_prev * 100, len(blended),
    )
    return blended
