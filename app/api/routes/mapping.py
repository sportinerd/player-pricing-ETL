# app/api/routes/mapping.py — Multi-signal player mapping endpoints
#
# Endpoints:
#   POST /mapping/run                      — start mapping job for a league
#   GET  /mapping/status/{job_id}          — poll job progress
#   GET  /mapping/results/{job_id}         — review per-player mapping results
#   POST /mapping/commit/{job_id}          — commit approved mappings to FGL
#   GET  /mapping/runs                     — list past mapping jobs

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.services.mapping_service import MappingService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/mapping", tags=["Mapping"])


# ── Request schemas ───────────────────────────────────────────────────────────

class MappingRunRequest(BaseModel):
    league_id: str
    batch_size: int = Field(default=10, ge=1, le=50)


class OverrideEntry(BaseModel):
    gs_id: str
    fgl_id: str


class CommitRequest(BaseModel):
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    overrides: List[OverrideEntry] = Field(default_factory=list)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/run",
    summary="Start a multi-signal player mapping job",
    status_code=202,
)
async def run_mapping(body: MappingRunRequest, background_tasks: BackgroundTasks):
    """
    Fetches all GS players for the league, enriches each with profile data
    (DOB, nationality, height, weight), and scores matches against FGL players
    using a 5-signal confidence model.

    Results are stored for review — nothing is written to FGL until you call
    POST /mapping/commit/{job_id}.
    """
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        await get_db().player_mapping_jobs.insert_one({
            "_id": job_id,
            "league_id": body.league_id,
            "started_at": now,
            "status": "queued",
            "total": 0,
            "processed": 0,
            "profiles_fetched": 0,
            "profiles_cached": 0,
            "high_confidence": 0,
            "medium_confidence": 0,
            "low_confidence": 0,
            "unmatched": 0,
            "committed": 0,
            "committed_at": None,
            "completed_at": None,
            "error": None,
        })
    except Exception as e:
        log.error("Failed to create mapping job: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not create job: {e}")

    svc = MappingService()
    background_tasks.add_task(
        svc.run_mapping,
        job_id=job_id,
        league_id=body.league_id,
        batch_size=body.batch_size,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Mapping job started. Poll /mapping/status/{job_id} for progress.",
    }


@router.get(
    "/status/{job_id}",
    summary="Poll mapping job status",
)
async def get_mapping_status(job_id: str):
    """
    Returns current progress of a mapping job.
    Status flow: queued → name_matching → fetching_profiles → scoring → completed | failed
    """
    try:
        job = await get_db().player_mapping_jobs.find_one({"_id": job_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job["job_id"] = str(job.pop("_id"))
    return job


@router.get(
    "/results/{job_id}",
    summary="Review per-player mapping results",
)
async def get_mapping_results(
    job_id: str,
    tier: Optional[str] = Query(default=None, description="Filter: high, medium, low, unmatched"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """
    Returns detailed per-player mapping results for review.
    Each result includes confidence score, tier, and signal breakdown.
    """
    try:
        query = {"job_id": job_id}
        if tier:
            query["tier"] = tier

        cursor = (
            get_db().player_mapping_results
            .find(query, {"_id": 0})
            .sort("confidence", -1)
            .skip(offset)
            .limit(limit)
        )
        results = await cursor.to_list(length=limit)

        total = await get_db().player_mapping_results.count_documents(query)

        return {
            "job_id": job_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/commit/{job_id}",
    summary="Commit approved mappings to FGL",
)
async def commit_mappings(job_id: str, body: CommitRequest):
    """
    Writes `goalserve_player_id` to FGL player documents for all matches
    at or above the confidence threshold. Manual overrides are applied on top.

    This is the ONLY field written to FGL — no other fields are touched.
    """
    # Verify job exists and is completed
    job = await get_db().player_mapping_jobs.find_one({"_id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.get("status") != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job status is '{job.get('status')}' — must be 'completed' to commit",
        )

    try:
        svc = MappingService()
        overrides = [{"gs_id": o.gs_id, "fgl_id": o.fgl_id} for o in body.overrides]
        result = await svc.commit_mappings(
            job_id=job_id,
            confidence_threshold=body.confidence_threshold,
            overrides=overrides,
        )
        return result
    except Exception as e:
        log.error("Commit failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/runs",
    summary="List past mapping jobs",
)
async def list_mapping_runs(
    limit: int = Query(default=20, ge=1, le=100),
    league_id: Optional[str] = Query(default=None),
):
    """Returns past and current mapping jobs, most recent first."""
    try:
        query = {}
        if league_id:
            query["league_id"] = league_id

        cursor = (
            get_db().player_mapping_jobs
            .find(query)
            .sort("started_at", -1)
            .limit(limit)
        )
        runs = await cursor.to_list(length=limit)
        for r in runs:
            r["job_id"] = str(r.pop("_id"))
        return {"total": len(runs), "runs": runs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
