# app/api/routes/pricing.py — Pricing pipeline endpoints

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from typing import Optional

from app.api.schemas import PricingRunRequest, PricingRunResponse
from app.pipeline.orchestrator import run_full_pipeline
from app.loaders.mongo_loader import (
    get_latest_players, get_player_by_id, get_pricing_runs,
)
from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/pricing", tags=["pricing"])


async def _run_pipeline_task(req: PricingRunRequest):
    """Background task wrapper for the pricing pipeline."""
    try:
        await run_full_pipeline(
            league_id=req.league_id,
            season_label=req.season_label,
            format_override=req.format_override,
            nt_league_ids=req.nt_league_ids,
            league_name=req.league_name,
        )
    except Exception as e:
        log.error("Background pipeline failed: %s", e)


@router.post("/run", response_model=PricingRunResponse, status_code=202)
async def trigger_pricing_run(req: PricingRunRequest, background_tasks: BackgroundTasks):
    """Trigger a pricing pipeline run in the background."""
    background_tasks.add_task(_run_pipeline_task, req)
    return PricingRunResponse(
        run_id="pending",
        status="started",
        message=f"Pricing pipeline started for league {req.league_id}",
    )


@router.post("/run/sync")
async def trigger_pricing_run_sync(req: PricingRunRequest):
    """Trigger a pricing pipeline run synchronously (waits for completion)."""
    result = await run_full_pipeline(
        league_id=req.league_id,
        season_label=req.season_label,
        format_override=req.format_override,
        nt_league_ids=req.nt_league_ids,
        league_name=req.league_name,
    )
    return result


@router.get("/runs")
async def list_pricing_runs(league_id: Optional[str] = None, limit: int = 20):
    """List recent pricing runs."""
    return await get_pricing_runs(league_id=league_id, limit=limit)


# ── Player endpoints ─────────────────────────────────────────────────────────

players_router = APIRouter(prefix="/players", tags=["players"])


@players_router.get("")
async def list_players(
    league_id: str,
    position: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    limit: int = Query(default=500, le=1000),
):
    """Get latest priced players for a league."""
    players = await get_latest_players(
        league_id=league_id,
        position=position,
        min_price=min_price,
        max_price=max_price,
        limit=limit,
    )
    if not players:
        raise HTTPException(404, f"No priced players found for league {league_id}")
    return {"count": len(players), "players": players}


@players_router.get("/{player_id}")
async def get_player(player_id: str, league_id: str):
    """Get a single player with full pricing breakdown."""
    player = await get_player_by_id(league_id=league_id, player_id=player_id)
    if not player:
        raise HTTPException(404, f"Player {player_id} not found in league {league_id}")
    return player
