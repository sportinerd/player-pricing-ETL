# app/transformers/factors/xi_estimator.py (v3.0)
# Adapted from v24 — import paths updated, logic unchanged.

from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

from app.core.config import settings
from app.transformers.normalizer import normalize_position
from app.utils.logger import get_logger

log = get_logger(__name__)


def _parse_lineup_data(lineup_dict: Dict) -> Tuple[List[str], List[str]]:
    """Extract (starter_ids, sub_ids) from one lineup dict."""
    starters, subs = [], []
    for team_key in ("localteam", "visitorteam"):
        team = lineup_dict.get(team_key) or {}
        players = team.get("player") or []
        if isinstance(players, dict):
            players = [players]
        for p in players:
            pid = str(p.get("id") or p.get("player_id") or "")
            ptype = (p.get("type") or p.get("lineup_type") or "").lower()
            if not pid:
                continue
            if "starter" in ptype or "start" in ptype:
                starters.append(pid)
            elif "sub" in ptype or "substitute" in ptype or "bench" in ptype:
                subs.append(pid)
    return starters, subs


def compute_lineup_rates(lineup_history: List[Dict]) -> Dict[str, Dict]:
    player_stats: Dict[str, Dict] = {}
    total_matches = len(lineup_history)
    for lineup in lineup_history:
        starters, subs = _parse_lineup_data(lineup)
        for pid in starters:
            if pid not in player_stats:
                player_stats[pid] = {"started": 0, "subbed": 0, "appeared_in": 0}
            player_stats[pid]["started"] += 1
            player_stats[pid]["appeared_in"] += 1
        for pid in subs:
            if pid not in player_stats:
                player_stats[pid] = {"started": 0, "subbed": 0, "appeared_in": 0}
            player_stats[pid]["subbed"] += 1
            player_stats[pid]["appeared_in"] += 1
    for pid, s in player_stats.items():
        eligible = s["appeared_in"]
        s["total_team_matches"] = total_matches
        s["lineup_rate"] = round(s["started"] / eligible, 3) if eligible > 0 else 0.0
    return player_stats


def _parse_squad_depth(players_list: List[Dict]) -> Dict[str, int]:
    """
    Parse player list → {player_id: squad_rank (1|2|3)}.
    Adapted for soccerleague feed where players are already extracted.
    """
    players_by_pos: Dict[str, List[Dict]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}

    for p in players_list:
        pid = str(p.get("player_id") or p.get("id") or "")
        if not pid:
            continue
        pos = p.get("position", "MID")
        try:
            squad_num = int(p.get("squad_number") or p.get("number") or 99)
        except (ValueError, TypeError):
            squad_num = 99
        players_by_pos[pos].append({"id": pid, "squad_num": squad_num})

    STARTER_COUNTS = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}
    ROTATION_EXTRA = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 2}

    rank_map: Dict[str, int] = {}
    for pos, players in players_by_pos.items():
        sorted_p = sorted(players, key=lambda x: x["squad_num"])
        n_starter = STARTER_COUNTS.get(pos, 3)
        n_rotation = n_starter + ROTATION_EXTRA.get(pos, 2)
        for i, p in enumerate(sorted_p):
            rank_map[p["id"]] = 1 if i < n_starter else 2 if i < n_rotation else 3

    return rank_map


def compute_xi_probability(
    player: Dict,
    lineup_stats: Optional[Dict] = None,
    squad_rank: Optional[int] = None,
) -> float:
    avg_min = float(player.get("avg_minutes", 0))
    minutes_ratio = min(avg_min / 90.0, 1.0)

    have_lineup = lineup_stats is not None
    weights = settings.XI_WEIGHTS if have_lineup else settings.XI_WEIGHTS_NO_LINEUP

    lineup_rate = (
        float(lineup_stats.get("lineup_rate", minutes_ratio)) if have_lineup
        else 0.0
    )

    rank = squad_rank or (1 if minutes_ratio > 0.7 else 2 if minutes_ratio > 0.4 else 3)
    rank_score = settings.SQUAD_RANK_SCORE.get(str(rank), 0.65)

    xi_prob = (
        weights["minutes_ratio"] * minutes_ratio +
        weights["lineup_rate"] * lineup_rate +
        weights["squad_rank"] * rank_score
    )

    return round(max(settings.XI_PROB_MIN, min(xi_prob, settings.XI_PROB_MAX)), 3)


def compute_rival_injury_bonuses(
    players: List[Dict],
    injured_player_ids: Set[str],
    squad_rank_map: Dict[str, int],
) -> List[Dict]:
    groups: Dict[tuple, List[Dict]] = defaultdict(list)
    for p in players:
        key = (p.get("team_id", ""), p.get("position", "MID"))
        groups[key].append(p)

    for key, group in groups.items():
        pos = key[1]
        sorted_group = sorted(
            group, key=lambda p: squad_rank_map.get(p.get("player_id", ""), 3)
        )
        first_healthy_idx = None
        for i, p in enumerate(sorted_group):
            if p.get("player_id") not in injured_player_ids:
                first_healthy_idx = i
                break

        if first_healthy_idx is not None and first_healthy_idx > 0:
            beneficiary = sorted_group[first_healthy_idx]
            scarcity = settings.RIVAL_INJURY_SCARCITY.get(pos, 0.50)
            bonus = round(settings.RIVAL_INJURY_BONUS_BASE * scarcity, 2)
            beneficiary["rival_injury_bonus"] = True
            beneficiary["rival_injury_bonus_gbp"] = bonus

    return players
