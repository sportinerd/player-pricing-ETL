# app/transformers/factors/national_team.py (v3.0)
# Adapted from v24 — import paths updated, logic unchanged.

from typing import Dict, List, Optional

from app.core.config import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_FIFA_RANK_STRENGTH: Dict[str, float] = {
    "Argentina": 1.00, "France": 0.98, "England": 0.95, "Brazil": 0.94,
    "Belgium": 0.92, "Portugal": 0.91, "Netherlands": 0.90, "Spain": 0.90,
    "Germany": 0.88, "Italy": 0.87, "Croatia": 0.86, "Morocco": 0.84,
    "USA": 0.82, "Uruguay": 0.81, "Colombia": 0.80, "Japan": 0.79,
    "Senegal": 0.78, "Mexico": 0.76, "Switzerland": 0.75, "Denmark": 0.74,
    "Ecuador": 0.72, "Australia": 0.70, "South Korea": 0.68, "Cameroon": 0.67,
    "Ghana": 0.66, "Tunisia": 0.62, "Poland": 0.61, "Serbia": 0.60,
    "Iran": 0.58, "Wales": 0.57, "Qatar": 0.53, "Saudi Arabia": 0.50,
}


def get_nation_strength(nationality: str) -> float:
    return _FIFA_RANK_STRENGTH.get(nationality, 0.65)


def compute_nt_perf_score(nt_player: Optional[Dict]) -> float:
    if not nt_player:
        return 0.0
    position = nt_player.get("position", "MID")
    apps = max(float(nt_player.get("appearances", 0)), 1.0)
    sw = settings.STAT_WEIGHTS
    cs_weight = {
        "GK": sw["clean_sheet_gk"], "DEF": sw["clean_sheet_def"],
        "MID": sw["clean_sheet_mid"], "FWD": sw["clean_sheet_fwd"],
    }.get(position, sw["clean_sheet_mid"])

    raw = (
        float(nt_player.get("goals", 0)) * sw["goals"] +
        float(nt_player.get("assists", 0)) * sw["assists"] +
        float(nt_player.get("clean_sheets", 0)) * cs_weight +
        (float(nt_player.get("saves", 0)) * sw["saves_per_game"]
         if position == "GK" else 0.0) +
        float(nt_player.get("yellow_cards", 0)) * sw["yellow_cards"] +
        float(nt_player.get("red_cards", 0)) * sw["red_cards"] +
        float(nt_player.get("avg_minutes", 0)) * sw["minutes"]
    )
    return raw / apps


def compute_nt_xi_prob(player_id: str, nt_lineup_rates: Dict[str, float]) -> float:
    if not nt_lineup_rates or player_id not in nt_lineup_rates:
        return 0.7
    return float(nt_lineup_rates[player_id])


def compute_nt_factor(player: Dict, nt_player_data: Optional[Dict], nt_xi_prob: float = 0.7) -> float:
    nationality = player.get("nationality") or player.get("country") or ""
    nation_strength = get_nation_strength(nationality)
    nt_apps = float(nt_player_data.get("appearances", 0)) if nt_player_data else 0

    if nt_apps >= settings.MIN_NT_APPEARANCES:
        nt_perf_norm = min(compute_nt_perf_score(nt_player_data) / 6.0, 1.0)
    else:
        nt_perf_norm = nation_strength * 0.8
        nt_xi_prob = 0.6

    nt_raw = 0.40 * nation_strength + 0.35 * nt_perf_norm + 0.25 * nt_xi_prob
    nt_range = settings.NT_FACTOR_MAX - settings.NT_FACTOR_MIN
    return round(max(settings.NT_FACTOR_MIN, min(settings.NT_FACTOR_MIN + nt_raw * nt_range, settings.NT_FACTOR_MAX)), 3)


def enrich_players_with_nt_factors(
    players: List[Dict],
    nt_players_by_id: Dict[str, Dict],
    nt_lineup_rates: Dict[str, float],
    format_type: str = "league",
) -> List[Dict]:
    if format_type == "league":
        for p in players:
            p["nt_factor"] = 1.0
        return players

    enriched = 0
    for player in players:
        pid = str(player.get("player_id", ""))
        nt_data = nt_players_by_id.get(pid)
        nt_xi = compute_nt_xi_prob(pid, nt_lineup_rates)
        player["nt_factor"] = compute_nt_factor(player, nt_data, nt_xi)
        if nt_data:
            enriched += 1

    log.info("NT factors: %d/%d had NT stats data", enriched, len(players))
    return players
