# app/api/routes/images.py — Image sync endpoints
#
# Endpoints:
#   GET  /images/placeholder-stats           — count placeholder images in FGL
#   POST /images/sync/bulk                   — bulk replace GS images (background job)
#   GET  /images/sync/bulk/status/{job_id}   — poll bulk job progress
#   GET  /images/sync/runs                   — list all past bulk sync jobs
#   POST /images/sync/player/{api_player_id} — replace image for a single player

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.services.image_service import ImageService
from app.utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/images", tags=["Images"])


# ── Request schemas ───────────────────────────────────────────────────────────

class BulkSyncRequest(BaseModel):
    dry_run: bool = False
    batch_size: int = Field(default=10, ge=1, le=50)
    min_image_bytes: int = Field(default=1000, ge=100)


class SingleSyncRequest(BaseModel):
    dry_run: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "/placeholder-stats",
    summary="Count players with placeholder images",
    response_description="Breakdown of placeholder vs real images in FGL players collection",
)
async def get_placeholder_stats():
    """
    Returns the number of players in Fantasy_Global_Livescore that still have
    the SportMonks placeholder image, and how many of those have a GoalServe ID
    (and therefore can have their image replaced).
    """
    try:
        svc = ImageService()
        return await svc.get_placeholder_stats()
    except Exception as e:
        log.error("placeholder-stats failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/sync/bulk",
    summary="Bulk replace placeholder images from GoalServe",
    status_code=202,
)
async def sync_bulk(body: BulkSyncRequest, background_tasks: BackgroundTasks):
    """
    Fetches images from GoalServe for all FGL players that have a
    `goalserve_player_id` and still carry the SM placeholder image,
    uploads them to DigitalOcean Spaces CDN, and updates the `image` field.
    The old placeholder URL is preserved in `sportsmonk_image_url`.

    - **dry_run=true** — scans and reports counts without writing anything.
    - **dry_run=false** — starts a background job; poll `/images/sync/bulk/status/{job_id}`.
    - **batch_size** — concurrent GS requests per batch (default 10).
    - **min_image_bytes** — skip images smaller than this (default 1000 bytes).
    """
    svc = ImageService()

    if body.dry_run:
        try:
            result = await svc.sync_bulk_dry_run(body.batch_size, body.min_image_bytes)
        except Exception as e:
            log.error("Dry run bulk sync failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
        return {
            "dry_run": True,
            "total_attempted": result.total_attempted,
            "would_update": result.would_update,
            "skipped_empty_image": result.skipped_empty_image,
            "skipped_already_set": result.skipped_already_set,
            "failed": result.failed,
        }

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        await get_db().image_sync_jobs.insert_one({
            "_id": job_id,
            "started_at": now,
            "status": "queued",
            "total": 0,
            "processed": 0,
            "updated": 0,
            "skipped_empty_image": 0,
            "skipped_already_set": 0,
            "failed": 0,
            "completed_at": None,
        })
    except Exception as e:
        log.error("Failed to create image_sync_job record: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not create job: {e}")

    background_tasks.add_task(
        svc.sync_bulk_background,
        job_id=job_id,
        batch_size=body.batch_size,
        min_image_bytes=body.min_image_bytes,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Bulk image sync started. Poll /images/sync/bulk/status/{job_id} for progress.",
    }


@router.get(
    "/sync/bulk/status/{job_id}",
    summary="Poll bulk image sync job status",
)
async def get_bulk_sync_status(job_id: str):
    """
    Returns the current progress of a bulk image sync job.
    Status values: `queued` → `running` → `completed` | `failed`
    """
    try:
        job = await get_db().image_sync_jobs.find_one({"_id": job_id}, {"_id": 0})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@router.get(
    "/sync/runs",
    summary="List all bulk image sync runs",
)
async def list_sync_runs(
    limit: int = Query(default=20, ge=1, le=100),
    status: str = Query(default=None, description="Filter by status: queued, running, completed, failed"),
):
    """
    Returns past and current bulk image sync jobs, most recent first.
    """
    try:
        query = {}
        if status:
            query["status"] = status
        cursor = get_db().image_sync_jobs.find(query, {"_id": 1, "started_at": 1, "status": 1, "total": 1, "processed": 1, "updated": 1, "skipped_empty_image": 1, "skipped_already_set": 1, "failed": 1, "completed_at": 1}).sort("started_at", -1).limit(limit)
        runs = await cursor.to_list(length=limit)
        for r in runs:
            r["job_id"] = str(r.pop("_id"))
        return {"total": len(runs), "runs": runs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/sync/player/{api_player_id}",
    summary="Replace image for a single player by SM player ID",
)
async def sync_single_player(api_player_id: int, body: SingleSyncRequest):
    """
    Fetches a GoalServe image for one player (identified by their SportMonks
    `api_player_id`), uploads it to the CDN, and updates the FGL player document.

    - **dry_run=true** — shows what would happen without writing anything.
    """
    try:
        svc = ImageService()
        result = await svc.sync_single_player(api_player_id, body.dry_run)
    except Exception as e:
        log.error("Single player sync failed for api_player_id=%s: %s", api_player_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    if result.status == "failed" and result.error and "not found" in result.error:
        raise HTTPException(status_code=404, detail=result.error)

    return {
        "api_player_id": result.api_player_id,
        "name": result.name,
        "status": result.status,
        "old_image": result.old_image,
        "new_image": result.new_image,
        "error": result.error,
    }
