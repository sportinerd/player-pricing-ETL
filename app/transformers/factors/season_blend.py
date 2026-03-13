# app/transformers/factors/season_blend.py (v3.0)
# Adapted from v24 — import paths updated, logic unchanged.

from datetime import datetime
from typing import Dict, List, Optional

from app.core.config import settings
from app.transformers.normalizer import safe_float
from app.utils.logger import get_logger

log = get_logger(__name__)


def _perf_score(player: Dict) -> float:
    """FPL-style per-game score."""
    position = player.get("position", "MID")
    apps = max(safe_float(player.get("appearances")), 1.0)

    goals = safe_float(player.get("goals"))
    ast = safe_float(player.get("assists"))
    cs = safe_float(player.get("clean_sheets"))
    saves = safe_float(player.get("saves"))
    yc = safe_float(player.get("yellow_cards"))
    rc = safe_float(player.get("red_cards"))
    avg_m = safe_float(player.get("avg_minutes"))
    sot = safe_float(player.get("shots_on_target"))
    kp = safe_float(player.get("key_passes"))

    sw = settings.STAT_WEIGHTS
    cs_weight = {
        "GK": sw["clean_sheet_gk"], "DEF": sw["clean_sheet_def"],
        "MID": sw["clean_sheet_mid"], "FWD": sw["clean_sheet_fwd"],
    }.get(position, sw["clean_sheet_mid"])

    raw = (
        goals * sw["goals"] + ast * sw["assists"] + cs * cs_weight +
        (saves * sw["saves_per_game"] if position == "GK" else 0.0) +
        yc * sw["yellow_cards"] + rc * sw["red_cards"] +
        avg_m * sw["minutes"] + sot * sw["shots_on_target"] + kp * sw["key_passes"]
    )
    return raw / apps


def _blend_weights(curr_apps: float, prev_apps: float):
    if curr_apps < 1 and prev_apps >= 1:
        return 0.0, 1.0
    if curr_apps >= 1 and prev_apps < 1:
        return 1.0, 0.0
    if curr_apps < 1 and prev_apps < 1:
        return 0.0, 0.0
    ramp = min(curr_apps / settings.BLEND_FULL_GW, 1.0)
    w_curr = settings.CURRENT_SEASON_WEIGHT * ramp
    w_prev = 1.0 - w_curr
    return w_curr, w_prev


def compute_blended_perf_score(current: Optional[Dict], previous: Optional[Dict]) -> float:
    curr_apps = safe_float(current.get("appearances")) if current else 0.0
    prev_apps = safe_float(previous.get("appearances")) if previous else 0.0
    curr_score = _perf_score(current) if current and curr_apps >= 1 else 0.0
    prev_score = _perf_score(previous) if previous and prev_apps >= 1 else 0.0
    w_curr, w_prev = _blend_weights(curr_apps, prev_apps)
    if w_curr == 0.0 and w_prev == 0.0:
        return 0.0
    return w_curr * curr_score + w_prev * prev_score


def _match_age_days(match: Dict) -> Optional[int]:
    raw = match.get("match_date") or match.get("date") or ""
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(raw.strip(), fmt)
            return max((datetime.now() - d).days, 0)
        except ValueError:
            continue
    return None


def compute_form_score(recent_match_stats: List[Dict]) -> float:
    """Time-gated form score from last FORM_WINDOW matches within FORM_MAX_AGE_DAYS."""
    if not recent_match_stats:
        return 0.0

    time_valid: List[Dict] = []
    missing_date_count = 0

    for match in recent_match_stats:
        age = _match_age_days(match)
        if age is None:
            missing_date_count += 1
            time_valid.append(match)
        elif age <= settings.FORM_MAX_AGE_DAYS:
            time_valid.append(match)

    total = len(recent_match_stats)
    if missing_date_count > total * 0.5:
        log.warning("Form decay bypassed: %d/%d matches have no parseable date.", missing_date_count, total)
        return 0.0

    if not time_valid:
        return 0.0

    window = time_valid[:settings.FORM_WINDOW]
    n = len(window)
    total_w = sum(1.0 - i * 0.1 for i in range(n))

    weighted_sum = 0.0
    for i, match in enumerate(window):
        md_dict = {
            **match,
            "appearances": 1,
            "avg_minutes": safe_float(match.get("minutes_played"), 90),
        }
        weight = 1.0 - i * 0.1
        weighted_sum += _perf_score(md_dict) * weight

    return weighted_sum / total_w if total_w > 0 else 0.0


def compute_final_perf_score(
    current: Optional[Dict],
    previous: Optional[Dict],
    recent_match_stats: Optional[List[Dict]] = None,
) -> float:
    season_score = compute_blended_perf_score(current, previous)
    if recent_match_stats:
        form_score = compute_form_score(recent_match_stats)
        if form_score > 0:
            return settings.SEASON_STAT_WEIGHT * season_score + settings.FORM_WEIGHT * form_score
    return season_score


def merge_player_seasons(current_players: List[Dict], previous_players: List[Dict]) -> List[Dict]:
    prev_by_id = {p["player_id"]: p for p in previous_players}
    for player in current_players:
        prev = prev_by_id.get(player.get("player_id", ""))
        player["previous_season"] = prev
        player["has_previous_data"] = prev is not None
    matched = sum(1 for p in current_players if p["has_previous_data"])
    log.info("Season merge: %d current, %d matched to previous (%.0f%%)",
             len(current_players), matched,
             100 * matched / len(current_players) if current_players else 0)
    return current_players
