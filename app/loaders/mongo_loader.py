# app/loaders/mongo_loader.py — MongoDB persistence layer

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.core.database import get_db
from app.utils.logger import get_logger

log = get_logger(__name__)


async def save_pricing_run(
    league_id: str,
    season_label: str,
    format_type: str,
    player_count: int,
    squad_cost: float,
    scale_adj: float,
    current_gw: int,
    status: str = "success",
    error_message: str = None,
) -> str:
    """Save a pipeline run record. Returns the run_id as string."""
    db = get_db()
    doc = {
        "league_id": league_id,
        "season_label": season_label,
        "format_type": format_type,
        "player_count": player_count,
        "squad_cost": squad_cost,
        "calibration_scale_adj": scale_adj,
        "current_gw": current_gw,
        "status": status,
        "error_message": error_message,
        "run_at": datetime.now(timezone.utc),
    }
    result = await db.pricing_runs.insert_one(doc)
    run_id = str(result.inserted_id)
    log.info("Saved pricing run: %s (league=%s, %d players)", run_id, league_id, player_count)
    return run_id


async def save_player_prices(
    run_id: str,
    players: List[Dict],
    league_id: str,
    season_label: str,
) -> int:
    """Save priced players for a run. Returns count of inserted documents."""
    if not players:
        return 0
    db = get_db()
    docs = []
    for p in players:
        doc = {
            "run_id": run_id,
            "league_id": league_id,
            "season_label": season_label,
            "player_id": p.get("player_id"),
            "name": p.get("name"),
            "position": p.get("position"),
            "team_id": p.get("team_id"),
            "team_name": p.get("team_name"),
            "price": p.get("price"),
            "price_tier": p.get("price_tier"),
            "goals": p.get("goals"),
            "assists": p.get("assists"),
            "clean_sheets": p.get("clean_sheets"),
            "saves": p.get("saves"),
            "appearances": p.get("appearances"),
            "minutes_played": p.get("minutes_played"),
            "avg_minutes": p.get("avg_minutes"),
            "rating": p.get("rating"),
            "xi_probability": p.get("xi_probability"),
            "injury_status": p.get("injury_status"),
            "injury_discount": p.get("injury_discount"),
            "nt_factor": p.get("nt_factor"),
            "fdr_multiplier": p.get("fdr_multiplier"),
            "team_strength": p.get("team_strength"),
            "form_score": p.get("form_score"),
            "is_injured": p.get("is_injured"),
            "_pricing_debug": p.get("_pricing_debug"),
            "created_at": datetime.now(timezone.utc),
        }
        docs.append(doc)

    from pymongo import UpdateOne
    ops = [
        UpdateOne(
            {"league_id": league_id, "player_id": doc["player_id"]},
            {"$set": doc},
            upsert=True,
        )
        for doc in docs
    ]
    result = await db.player_prices.bulk_write(ops, ordered=False)
    written = result.modified_count + result.upserted_count
    log.info("Saved %d player prices for run %s", written, run_id)
    return written


async def save_standings_cache(
    league_id: str,
    season_label: str,
    standings: List[Dict],
) -> None:
    db = get_db()
    await db.standings_cache.update_one(
        {"league_id": league_id, "season_label": season_label},
        {"$set": {
            "teams": standings,
            "fetched_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def save_injuries_cache(
    league_id: str,
    injuries: Dict[str, Dict],
) -> None:
    db = get_db()
    await db.injuries_cache.update_one(
        {"league_id": league_id},
        {"$set": {
            "injuries": injuries,
            "fetched_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def save_teams(
    league_id: str,
    season_label: str,
    team_strength: Dict[str, float],
    team_ids: List[str],
) -> None:
    db = get_db()
    await db.teams.update_one(
        {"league_id": league_id, "season_label": season_label},
        {"$set": {
            "team_ids": team_ids,
            "strength_map": team_strength,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def save_goalserve_cache(
    feed_name: str,
    league_id: str,
    data: Any,
    ttl_hours: int = 4,
) -> None:
    """Cache raw GoalServe response with TTL."""
    db = get_db()
    await db.goalserve_cache.update_one(
        {"feed_name": feed_name, "league_id": league_id},
        {"$set": {
            "data": data,
            "fetched_at": datetime.now(timezone.utc),
            "ttl_expires_at": datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        }},
        upsert=True,
    )


async def load_goalserve_cache(feed_name: str, league_id: str) -> Optional[Dict]:
    """Load cached GoalServe response if not expired."""
    db = get_db()
    doc = await db.goalserve_cache.find_one({
        "feed_name": feed_name,
        "league_id": league_id,
        "ttl_expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if doc:
        log.info("Using cached %s for league %s (fetched %s)",
                 feed_name, league_id, doc.get("fetched_at"))
        return doc.get("data")
    return None


async def get_latest_players(
    league_id: str,
    position: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    limit: int = 500,
) -> List[Dict]:
    """Get players from the most recent pricing run for a league."""
    db = get_db()

    # Find latest run
    latest_run = await db.pricing_runs.find_one(
        {"league_id": league_id, "status": "success"},
        sort=[("run_at", -1)],
    )
    if not latest_run:
        return []

    run_id = str(latest_run["_id"])
    query: Dict = {"run_id": run_id}

    if position:
        query["position"] = position
    if min_price is not None:
        query["price"] = query.get("price", {})
        query["price"]["$gte"] = min_price
    if max_price is not None:
        query["price"] = query.get("price", {})
        query["price"]["$lte"] = max_price

    cursor = db.player_prices.find(query, {"_id": 0, "_pricing_debug": 0}).sort("price", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_player_by_id(league_id: str, player_id: str) -> Optional[Dict]:
    """Get a single player from the latest run with full pricing breakdown."""
    db = get_db()
    latest_run = await db.pricing_runs.find_one(
        {"league_id": league_id, "status": "success"},
        sort=[("run_at", -1)],
    )
    if not latest_run:
        return None

    run_id = str(latest_run["_id"])
    doc = await db.player_prices.find_one(
        {"run_id": run_id, "player_id": player_id},
        {"_id": 0},
    )
    return doc


async def get_pricing_runs(league_id: Optional[str] = None, limit: int = 20) -> List[Dict]:
    db = get_db()
    query = {"league_id": league_id} if league_id else {}
    cursor = db.pricing_runs.find(query, {"_id": 1, "league_id": 1, "season_label": 1,
                                           "status": 1, "player_count": 1, "squad_cost": 1,
                                           "run_at": 1}).sort("run_at", -1).limit(limit)
    runs = await cursor.to_list(length=limit)
    for r in runs:
        r["run_id"] = str(r.pop("_id"))
    return runs


async def save_prev_season_cache(league_id: str, player_stats: Dict[str, Dict]) -> None:
    """
    Persist previous-season stats keyed by player_id + league_id.
    Previous-season data is static once the season ends, so TTL is long (180 days).
    """
    from pymongo import UpdateOne
    db = get_db()
    now = datetime.now(timezone.utc)
    ops = [
        UpdateOne(
            {"player_id": pid, "league_id": league_id},
            {"$set": {"player_id": pid, "league_id": league_id,
                      "stats": stats, "cached_at": now}},
            upsert=True,
        )
        for pid, stats in player_stats.items()
    ]
    if ops:
        await db.prev_season_cache.bulk_write(ops)
        log.info("Saved prev-season cache: %d players (league=%s)", len(ops), league_id)


async def load_prev_season_cache(
    league_id: str,
    player_ids: List[str],
) -> Dict[str, Dict]:
    """Load previous-season stats from cache, skipping entries older than PREV_SEASON_CACHE_DAYS."""
    from app.core.config import settings
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.PREV_SEASON_CACHE_DAYS)
    cursor = db.prev_season_cache.find(
        {"player_id": {"$in": player_ids}, "league_id": league_id,
         "cached_at": {"$gt": cutoff}},
        {"_id": 0, "player_id": 1, "stats": 1},
    )
    result: Dict[str, Dict] = {}
    async for doc in cursor:
        result[doc["player_id"]] = doc["stats"]
    return result


async def ensure_indexes():
    """Create MongoDB indexes for performance."""
    db = get_db()
    await db.player_prices.create_index([("run_id", 1), ("league_id", 1)])
    await db.player_prices.create_index([("player_id", 1)])
    await db.player_prices.create_index([("position", 1), ("price", -1)])
    await db.pricing_runs.create_index([("league_id", 1), ("run_at", -1)])
    await db.goalserve_cache.create_index("ttl_expires_at", expireAfterSeconds=0)
    await db.prev_season_cache.create_index(
        [("player_id", 1), ("league_id", 1)], unique=True
    )
    await db.prev_season_cache.create_index("cached_at")
    # Player mapping indexes
    await db.player_mapping_jobs.create_index([("league_id", 1), ("started_at", -1)])
    await db.player_mapping_results.create_index([("job_id", 1), ("tier", 1)])
    await db.player_mapping_results.create_index([("job_id", 1), ("confidence", -1)])
    log.info("MongoDB indexes ensured")
