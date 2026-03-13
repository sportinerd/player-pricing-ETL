# app/pricing/engine.py (v3.0)
# Adapted from v24 — import paths updated, logic unchanged.

import math
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.transformers.factors.season_blend import compute_final_perf_score
from app.transformers.normalizer import safe_float
from app.utils.logger import get_logger

log = get_logger(__name__)


class CalibrationPlateauError(Exception):
    """Raised when calibration solver detects scale_adj has no effect on squad cost."""


def _snap(value: float) -> float:
    if not math.isfinite(value):
        log.warning("_snap received non-finite value %s — defaulting to 0.0", value)
        return 0.0
    try:
        return float((Decimal(str(value)) / Decimal("0.5")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("0.5"))
    except InvalidOperation:
        log.warning("Decimal quantize failed for value %s — defaulting to 0.0", value)
        return 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def detect_format(league_id) -> str:
    if isinstance(league_id, dict):
        override = league_id.get("format_override")
        if override in ("league", "tournament"):
            return override
        lid = league_id.get("id") or league_id.get("league_id") or 0
    else:
        lid = league_id
    try:
        if int(lid) in settings.TOURNAMENT_LEAGUE_IDS:
            return "tournament"
    except (ValueError, TypeError):
        pass
    return "league"


def _label_tier(price: float, position: str) -> str:
    base = settings.POSITION_BASELINE[position]
    top = settings.POSITION_MAX[position]
    mid_cut = base + (top - base) * 0.35
    high_cut = base + (top - base) * 0.65
    if price <= base + 0.5:
        return "budget"
    if price <= mid_cut:
        return "mid-range"
    if price <= high_cut:
        return "premium"
    return "elite"


def compute_fdr_multiplier(
    team_id: str,
    upcoming_fixtures: List[Dict],
    team_strength_map: Dict[str, float],
    n: int = 5,
) -> float:
    if not upcoming_fixtures or not team_strength_map:
        return 1.0
    opponent_strengths = []
    for fix in upcoming_fixtures[:n]:
        home = str(fix.get("localteam_id", ""))
        away = str(fix.get("visitorteam_id", ""))
        opp = away if home == team_id else home
        if opp in team_strength_map:
            opponent_strengths.append(team_strength_map[opp])
    if not opponent_strengths:
        return 1.0
    avg_opp = sum(opponent_strengths) / len(opponent_strengths)
    fdr = settings.FDR_MAX - avg_opp * (settings.FDR_MAX - settings.FDR_MIN)
    return round(_clamp(fdr, settings.FDR_MIN, settings.FDR_MAX), 3)


def price_player(
    player: Dict,
    format_type: str = "league",
    scale_adj: float = 1.0,
) -> Tuple[float, str]:
    position = player.get("position", "MID")
    apps = safe_float(player.get("appearances"))
    baseline = settings.POSITION_BASELINE[position]
    max_price = settings.POSITION_MAX[position]
    base_scale = settings.LEAGUE_SCALE if format_type == "league" else settings.LEAGUE_SCALE * settings.TOURNAMENT_SCALE
    scale = base_scale * scale_adj

    if apps < settings.MIN_MATCHES_THRESHOLD:
        team_str = safe_float(player.get("team_strength"), 0.5)
        floor_adj = _snap(_clamp(baseline + team_str * 0.3, baseline, baseline + 0.5))
        return (floor_adj, "budget")

    final_perf = compute_final_perf_score(
        current=player,
        previous=player.get("previous_season"),
        recent_match_stats=player.get("recent_match_stats"),
    )
    xi_prob = safe_float(player.get("xi_probability"), 0.7)
    inj_discount = safe_float(player.get("injury_discount"), 1.0)
    rival_bonus = safe_float(player.get("rival_injury_bonus_gbp"))
    nt_factor = safe_float(player.get("nt_factor"), 1.0)
    fdr = safe_float(player.get("fdr_multiplier"), 1.0)
    team_str = safe_float(player.get("team_strength"), 0.5)
    team_boost = settings.TEAM_STRENGTH_WEIGHT * team_str * (max_price - baseline)

    perf_contrib = final_perf * scale * xi_prob
    above_baseline = perf_contrib + team_boost
    adjusted_above = above_baseline * inj_discount * nt_factor * fdr
    adjusted = baseline + adjusted_above + rival_bonus

    final = _snap(_clamp(adjusted, baseline, max_price))
    tier = _label_tier(final, position)
    return (final, tier)


def _simulate_squad_cost(players: List[Dict]) -> float:
    SLOTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    total = 0.0
    for pos, count in SLOTS.items():
        pos_players = sorted(
            [p for p in players if p.get("position") == pos and p.get("price")],
            key=lambda p: p["price"],
        )
        if not pos_players:
            continue
        n = len(pos_players)
        step = max(n // (count + 1), 1)
        selected = [pos_players[min(i * step, n - 1)] for i in range(1, count + 1)]
        total += sum(p["price"] for p in selected)
    return total


def _price_batch(players: List[Dict], format_type: str, scale_adj: float) -> List[Dict]:
    result = []
    for player in players:
        if not player:
            continue
        price, tier = price_player(player, format_type, scale_adj)
        result.append({**player, "price": price, "price_tier": tier})
    return result


def price_all_players(players: List[Dict], format_type: str) -> List[Dict]:
    lo_adj, hi_adj = 0.3, 3.0
    best_adj, best_cost, best_priced = 1.0, None, None
    target_mid = (settings.CALIBRATION_TARGET_MIN + settings.CALIBRATION_TARGET_MAX) / 2.0

    log.info("Calibration solver: target £%.0f–£%.0fm",
             settings.CALIBRATION_TARGET_MIN, settings.CALIBRATION_TARGET_MAX)

    try:
        cost_lo = _simulate_squad_cost(_price_batch(players, format_type, lo_adj))
        cost_hi = _simulate_squad_cost(_price_batch(players, format_type, hi_adj))
        log.info("  Calibration bounds: cost(%.1f)=£%.1fm  cost(%.1f)=£%.1fm",
                 lo_adj, cost_lo, hi_adj, cost_hi)
        if cost_lo == cost_hi:
            raise CalibrationPlateauError(
                f"Squad cost is £{cost_lo:.1f}m at both scale_adj={lo_adj} and {hi_adj}."
            )

        mid_init = _price_batch(players, format_type, 1.0)
        cost_init = _simulate_squad_cost(mid_init)
        best_adj, best_cost, best_priced = 1.0, cost_init, mid_init

        prev_cost = None
        for iteration in range(settings.CALIBRATION_MAX_ITER):
            mid_adj = (lo_adj + hi_adj) / 2.0
            priced = _price_batch(players, format_type, mid_adj)
            cost = _simulate_squad_cost(priced)

            log.info("  Calibration iter %d: scale_adj=%.4f → squad cost £%.1fm",
                     iteration + 1, mid_adj, cost)

            if abs(cost - target_mid) < abs(best_cost - target_mid):
                best_adj, best_cost, best_priced = mid_adj, cost, priced

            if settings.CALIBRATION_TARGET_MIN <= cost <= settings.CALIBRATION_TARGET_MAX:
                log.info("  Calibration converged at iter %d: £%.1fm (scale_adj=%.4f)",
                         iteration + 1, cost, mid_adj)
                best_adj, best_priced = mid_adj, priced
                break

            if prev_cost is not None and cost == prev_cost:
                raise CalibrationPlateauError(
                    f"Squad cost stuck at £{cost:.1f}m for two consecutive iterations."
                )
            prev_cost = cost

            if cost > settings.CALIBRATION_TARGET_MAX:
                hi_adj = mid_adj
            else:
                lo_adj = mid_adj
        else:
            log.warning("Calibration did not converge in %d iterations. Best: £%.1fm",
                        settings.CALIBRATION_MAX_ITER, best_cost)

    except CalibrationPlateauError as e:
        log.warning("CalibrationPlateauError: %s", e)
        if best_priced is None:
            best_adj = 1.0
            best_priced = _price_batch(players, format_type, 1.0)
            best_cost = _simulate_squad_cost(best_priced)

    tier_ct = {"budget": 0, "mid-range": 0, "premium": 0, "elite": 0}
    final_players = []
    for player in best_priced:
        player["_pricing_debug"] = {
            "xi_probability": player.get("xi_probability"),
            "injury_discount": player.get("injury_discount"),
            "rival_injury_bonus": player.get("rival_injury_bonus"),
            "rival_bonus_gbp": player.get("rival_injury_bonus_gbp"),
            "nt_factor": player.get("nt_factor"),
            "fdr_multiplier": player.get("fdr_multiplier"),
            "team_strength": player.get("team_strength"),
            "has_previous_data": player.get("has_previous_data"),
            "form_used": player.get("recent_match_stats") is not None,
            "calibration_scale_adj": round(best_adj, 4),
            "calibration_squad_cost": round(best_cost, 1),
        }
        tier = player.get("price_tier", "budget")
        tier_ct[tier] = tier_ct.get(tier, 0) + 1
        final_players.append(player)

    log.info(
        "Pricing complete: %d players | squad=£%.1fm | scale_adj=%.4f | "
        "budget=%d mid=%d premium=%d elite=%d",
        len(final_players), best_cost, best_adj,
        tier_ct["budget"], tier_ct["mid-range"], tier_ct["premium"], tier_ct["elite"],
    )
    return final_players
