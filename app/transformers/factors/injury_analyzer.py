# app/transformers/factors/injury_analyzer.py (v3.0)
#
# Rewritten for GoalServe global injuries feed (soccernew/injuries).
# Structure: scores.category[].matches.match[].{localteam,visitorteam}.sidelined
# Key changes:
#   - Global feed: must filter by known team IDs
#   - to_miss → injured (discount 0.0), questionable → doubtful (discount 0.70)
#   - player nodes can be single dict OR array (XML-to-JSON quirk)

from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.utils.logger import get_logger

log = get_logger(__name__)


def _ensure_list(val) -> list:
    """Handle GoalServe XML-to-JSON quirk: single item → dict, multiple → list."""
    if val is None:
        return []
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list):
        return val
    return []


def parse_injury_feed(
    injury_data: Dict,
    team_ids: Optional[Set[str]] = None,
) -> Dict[str, Dict]:
    """
    Parse GoalServe global injuries feed → {player_id: injury_record}.

    Structure: scores.category[].matches.match[].{localteam,visitorteam}.sidelined
    Each sidelined has:
      - to_miss.player[] → confirmed out
      - questionable.player{} → uncertain

    If team_ids is provided, only injuries for those teams are kept.
    """
    records: Dict[str, Dict] = {}
    scores = injury_data.get("scores", {})
    categories = _ensure_list(scores.get("category"))

    for category in categories:
        matches_node = category.get("matches", {})
        matches = _ensure_list(matches_node.get("match"))

        for match in matches:
            match_date = match.get("formatted_date", "")

            for team_key in ("localteam", "visitorteam"):
                team_node = match.get(team_key, {})
                team_id = str(team_node.get("id", ""))
                team_name = team_node.get("name", "")

                # Filter by league teams if provided
                if team_ids and team_id not in team_ids:
                    continue

                sidelined = team_node.get("sidelined", {})
                if not sidelined or not isinstance(sidelined, dict):
                    continue

                # to_miss → confirmed out (injured)
                to_miss = sidelined.get("to_miss", {})
                if to_miss and isinstance(to_miss, dict):
                    miss_players = _ensure_list(to_miss.get("player"))
                    for p in miss_players:
                        pid = str(p.get("id", "")).strip()
                        if not pid:
                            continue
                        records[pid] = {
                            "player_id": pid,
                            "name": p.get("name", ""),
                            "team_id": team_id,
                            "team_name": team_name,
                            "status": "injured",
                            "injury_type": p.get("status", "Unknown"),
                            "days_until_return": 0,
                            "is_long_term": False,
                            "match_date": match_date,
                            "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
                        }

                # questionable → doubtful
                questionable = sidelined.get("questionable", {})
                if questionable and isinstance(questionable, dict):
                    q_players = _ensure_list(questionable.get("player"))
                    for p in q_players:
                        pid = str(p.get("id", "")).strip()
                        if not pid:
                            continue
                        # Don't overwrite a to_miss record with questionable
                        if pid not in records:
                            records[pid] = {
                                "player_id": pid,
                                "name": p.get("name", ""),
                                "team_id": team_id,
                                "team_name": team_name,
                                "status": "doubtful",
                                "injury_type": p.get("status", "Unknown"),
                                "days_until_return": 0,
                                "is_long_term": False,
                                "match_date": match_date,
                                "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
                            }

    log.info("Parsed %d injury records (filtered to %s teams)",
             len(records), len(team_ids) if team_ids else "all")
    return records


def apply_current_injury_status(
    players: List[Dict],
    injury_lookup: Dict[str, Dict],
) -> Tuple[List[Dict], Set[str]]:
    """Update player injury_status and injury_discount."""
    injured_ids: Set[str] = set()

    for player in players:
        pid = str(player.get("player_id", ""))
        record = injury_lookup.get(pid)

        if record and record.get("status"):
            player["injury_status"] = record["status"]
            player["injury_days_remaining"] = record.get("days_until_return", 0)

            if record["status"] == "injured":
                player["injury_discount"] = settings.CURRENT_INJURY_DISCOUNT
                injured_ids.add(pid)
            elif record["status"] == "doubtful":
                player["injury_discount"] = 0.70
            elif record["status"] == "suspended":
                player["injury_discount"] = 0.85
        else:
            # Also check is_injured flag from soccerleague feed
            if player.get("is_injured"):
                player["injury_status"] = "injured"
                player["injury_discount"] = settings.CURRENT_INJURY_DISCOUNT
                injured_ids.add(pid)
            else:
                player["injury_status"] = None
                player["injury_discount"] = 1.0

    log.info("Injury status: %d injured, %d doubtful/suspended",
             sum(1 for p in players if p.get("injury_status") == "injured"),
             sum(1 for p in players if p.get("injury_status") in ("doubtful", "suspended")))
    return players, injured_ids


def apply_historical_injury_discounts(
    players: List[Dict],
    historical_snapshots: Dict[str, List[Dict]],
) -> List[Dict]:
    """Count long-term injuries per player across snapshots for historical discount."""
    for player in players:
        pid = str(player.get("player_id", ""))
        records = historical_snapshots.get(pid, [])

        long_term_months: set = set()
        for rec in records:
            if rec.get("is_long_term") and rec.get("injury_type"):
                snap_date = rec.get("snapshot_date", "")
                month_key = snap_date[:7] if snap_date else ""
                if month_key:
                    long_term_months.add((rec.get("injury_type", ""), month_key))

        long_term_count = len(long_term_months)
        hist_discount = max(
            settings.INJURY_DISCOUNT_MIN,
            min(1.0 - long_term_count * settings.INJURY_DISCOUNT_PER_LONG_TERM, 1.0),
        )

        current_discount = float(player.get("injury_discount", 1.0))
        player["injury_discount"] = min(current_discount, hist_discount)

    return players
