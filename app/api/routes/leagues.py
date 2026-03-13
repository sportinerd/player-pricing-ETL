# app/api/routes/leagues.py — League data endpoints

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.database import get_db
from app.extractors.goalserve import GoalServeClient
from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/leagues", tags=["leagues"])


@router.get("")
async def list_leagues(
    q: Optional[str] = Query(None, description="Filter by league name or category (case-insensitive)"),
    category: Optional[str] = Query(None, description="Filter by exact category (e.g. 'England', 'European Cups')"),
):
    """
    List all available GoalServe soccer leagues with their IDs.

    Use `?q=premier` to search by name, or `?category=England` to filter by category.
    """
    client = GoalServeClient()
    try:
        leagues = await client.get_leagues()
    finally:
        await client.close()

    if category:
        leagues = [l for l in leagues if l["category"].lower() == category.lower()]

    if q:
        q_lower = q.lower()
        leagues = [
            l for l in leagues
            if q_lower in l["name"].lower() or q_lower in l["category"].lower()
        ]

    return {"total": len(leagues), "leagues": leagues}


@router.get("/{league_id}/standings")
async def get_standings(league_id: str):
    """Get latest cached standings for a league."""
    db = get_db()
    doc = await db.standings_cache.find_one(
        {"league_id": league_id},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, f"No standings found for league {league_id}")
    return doc


@router.get("/{league_id}/injuries")
async def get_injuries(league_id: str):
    """Get latest cached injuries for a league."""
    db = get_db()
    doc = await db.injuries_cache.find_one(
        {"league_id": league_id},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, f"No injuries found for league {league_id}")
    return doc


@router.get("/{league_id}/teams")
async def get_teams(league_id: str):
    """Get teams with strength map for a league."""
    db = get_db()
    doc = await db.teams.find_one(
        {"league_id": league_id},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, f"No team data found for league {league_id}")
    return doc
