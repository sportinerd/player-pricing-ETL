# app/services/image_service.py — Image sync business logic
#
# Handles fetching player images from GoalServe, uploading to DigitalOcean
# Spaces CDN, and updating the Fantasy_Global_Livescore players collection.
#
# FGL player update per player:
#   { $set: { image: <cdn_url>, sportsmonk_image_url: <old_placeholder> } }
# Filter: { _id: player_doc['_id'] }  — never touches any other field.

import asyncio
import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.database import get_db
from app.extractors.goalserve import GoalServeClient
from app.utils.cdn import DOSpacesClient
from app.utils.logger import get_logger

log = get_logger(__name__)

PLACEHOLDER = "https://cdn.sportmonks.com/images/soccer/placeholder.png"


# ── Result models ─────────────────────────────────────────────────────────────

@dataclass
class PlayerSyncResult:
    api_player_id: Optional[int]
    name: str
    status: str  # updated | would_update | skipped_no_gs_id | skipped_empty_image | skipped_already_set | failed
    old_image: Optional[str] = None
    new_image: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BulkSyncResult:
    job_id: str
    total_attempted: int = 0
    updated: int = 0
    would_update: int = 0
    skipped_no_gs_id: int = 0
    skipped_empty_image: int = 0
    skipped_already_set: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)


# ── Service ───────────────────────────────────────────────────────────────────

class ImageService:
    def __init__(self):
        self.gs = GoalServeClient()
        self.cdn = DOSpacesClient()

    def _fgl_db(self):
        """FGL database on the same Motor client."""
        return get_db().client[settings.FGL_DB_NAME]

    def _pricing_db(self):
        return get_db()

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_placeholder_stats(self) -> dict:
        db = self._fgl_db()
        try:
            total = await db.players.count_documents({})
            placeholder_count = await db.players.count_documents({"image": PLACEHOLDER})
            with_gs_id = await db.players.count_documents({
                "image": PLACEHOLDER,
                "goalserve_player_id": {"$exists": True, "$ne": None},
            })
            without_gs_id = placeholder_count - with_gs_id
            return {
                "total_players": total,
                "placeholder_count": placeholder_count,
                "with_goalserve_id": with_gs_id,
                "without_goalserve_id": without_gs_id,
            }
        except Exception as e:
            log.error("Failed to fetch placeholder stats: %s", e)
            raise

    # ── Single player ─────────────────────────────────────────────────────────

    async def sync_single_player(
        self, api_player_id: int, dry_run: bool
    ) -> PlayerSyncResult:
        db = self._fgl_db()
        try:
            player = await db.players.find_one({"api_player_id": api_player_id})
        except Exception as e:
            log.error("DB lookup failed for api_player_id=%s: %s", api_player_id, e)
            return PlayerSyncResult(
                api_player_id=api_player_id,
                name="unknown",
                status="failed",
                error=f"DB lookup error: {e}",
            )

        if not player:
            return PlayerSyncResult(
                api_player_id=api_player_id,
                name="unknown",
                status="failed",
                error=f"Player with api_player_id={api_player_id} not found in FGL",
            )

        return await self._process_one(player, dry_run)

    # ── Bulk — dry run ────────────────────────────────────────────────────────

    async def sync_bulk_dry_run(
        self, batch_size: int, min_image_bytes: int
    ) -> BulkSyncResult:
        fgl_db = self._fgl_db()
        total = await fgl_db.players.count_documents({
            "image": PLACEHOLDER,
            "goalserve_player_id": {"$exists": True, "$ne": None},
        })

        result = BulkSyncResult(job_id="dry_run", total_attempted=total)
        cursor = fgl_db.players.find({
            "image": PLACEHOLDER,
            "goalserve_player_id": {"$exists": True, "$ne": None},
        })

        batch = []
        async for doc in cursor:
            batch.append(doc)
            if len(batch) >= batch_size:
                await self._process_batch(batch, result, min_image_bytes, dry_run=True)
                batch = []
        if batch:
            await self._process_batch(batch, result, min_image_bytes, dry_run=True)

        return result

    # ── Bulk — background job ─────────────────────────────────────────────────

    async def sync_bulk_background(
        self, job_id: str, batch_size: int, min_image_bytes: int
    ):
        pricing_db = self._pricing_db()
        fgl_db = self._fgl_db()

        try:
            total = await fgl_db.players.count_documents({
                "image": PLACEHOLDER,
                "goalserve_player_id": {"$exists": True, "$ne": None},
            })
            await pricing_db.image_sync_jobs.update_one(
                {"_id": job_id},
                {"$set": {"total": total, "status": "running"}},
            )

            result = BulkSyncResult(job_id=job_id, total_attempted=total)
            cursor = fgl_db.players.find({
                "image": PLACEHOLDER,
                "goalserve_player_id": {"$exists": True, "$ne": None},
            })

            batch = []
            async for doc in cursor:
                batch.append(doc)
                if len(batch) >= batch_size:
                    await self._process_batch(batch, result, min_image_bytes, dry_run=False)
                    batch = []
                    await self._update_job_progress(pricing_db, job_id, result)

            if batch:
                await self._process_batch(batch, result, min_image_bytes, dry_run=False)

            await pricing_db.image_sync_jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "status": "completed",
                    "processed": result.total_attempted,
                    "updated": result.updated,
                    "skipped_empty_image": result.skipped_empty_image,
                    "skipped_already_set": result.skipped_already_set,
                    "failed": result.failed,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            log.info(
                "Bulk image sync job %s completed — updated=%d skipped_empty=%d failed=%d",
                job_id, result.updated, result.skipped_empty_image, result.failed,
            )

        except Exception as e:
            log.error("Bulk image sync job %s crashed: %s", job_id, e)
            try:
                await pricing_db.image_sync_jobs.update_one(
                    {"_id": job_id},
                    {"$set": {
                        "status": "failed",
                        "error": str(e),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
            except Exception:
                pass

    async def _update_job_progress(self, pricing_db, job_id: str, result: BulkSyncResult):
        try:
            await pricing_db.image_sync_jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "processed": result.updated + result.skipped_empty_image + result.skipped_already_set + result.failed,
                    "updated": result.updated,
                    "skipped_empty_image": result.skipped_empty_image,
                    "skipped_already_set": result.skipped_already_set,
                    "failed": result.failed,
                }},
            )
        except Exception as e:
            log.warning("Failed to update job progress for %s: %s", job_id, e)

    # ── Batch processor ───────────────────────────────────────────────────────

    async def _process_batch(
        self,
        docs: list,
        result: BulkSyncResult,
        min_image_bytes: int,
        dry_run: bool,
    ):
        tasks = [self._process_one(doc, dry_run, min_image_bytes) for doc in docs]
        player_results = await asyncio.gather(*tasks, return_exceptions=True)
        for pr in player_results:
            if isinstance(pr, Exception):
                result.failed += 1
                result.errors.append({"player": "unknown", "error": str(pr)})
                continue
            if pr.status == "updated":
                result.updated += 1
            elif pr.status == "would_update":
                result.would_update += 1
            elif pr.status == "skipped_empty_image":
                result.skipped_empty_image += 1
            elif pr.status == "skipped_already_set":
                result.skipped_already_set += 1
            elif pr.status == "skipped_no_gs_id":
                result.skipped_no_gs_id += 1
            elif pr.status == "failed":
                result.failed += 1
                if pr.error:
                    result.errors.append({"player": pr.name, "error": pr.error})

    # ── Core per-player logic ─────────────────────────────────────────────────

    async def _process_one(
        self,
        player_doc: dict,
        dry_run: bool,
        min_image_bytes: int = 1000,
    ) -> PlayerSyncResult:
        api_player_id = player_doc.get("api_player_id")
        name = player_doc.get("name", "unknown")
        old_image = player_doc.get("image", "")

        # 1. Must have goalserve_player_id
        gs_id = player_doc.get("goalserve_player_id")
        if not gs_id:
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="skipped_no_gs_id", old_image=old_image,
            )

        # 2. Skip if image already replaced
        if old_image != PLACEHOLDER:
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="skipped_already_set", old_image=old_image,
            )

        # 3. Fetch player profile from GoalServe
        try:
            data = await self.gs.get_player_profile(str(gs_id))
            img_b64 = data.get("players", {}).get("player", {}).get("image", "")
        except Exception as e:
            log.warning("GS fetch failed for player '%s' (gs_id=%s): %s", name, gs_id, e)
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="failed", old_image=old_image, error=str(e),
            )

        # 4. Validate base64 image
        if not img_b64:
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="skipped_empty_image", old_image=old_image,
            )

        try:
            img_bytes = base64.b64decode(img_b64)
        except Exception as e:
            log.warning("Base64 decode failed for player '%s': %s", name, e)
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="skipped_empty_image", old_image=old_image,
                error="base64 decode error",
            )

        if len(img_bytes) < min_image_bytes:
            log.debug("Image too small for player '%s': %d bytes", name, len(img_bytes))
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="skipped_empty_image", old_image=old_image,
            )

        # 5. Dry run — report without writing
        if dry_run:
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="would_update", old_image=old_image,
            )

        # 6. Upload to CDN
        try:
            cdn_url = await self.cdn.upload_image(img_bytes, api_player_id)
        except Exception as e:
            log.error("CDN upload failed for player '%s': %s", name, e)
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="failed", old_image=old_image,
                error=f"CDN upload: {e}",
            )

        # 7. Update FGL DB — only image fields, nothing else
        try:
            fgl_db = self._fgl_db()
            await fgl_db.players.update_one(
                {"_id": player_doc["_id"]},
                {"$set": {"image": cdn_url, "sportsmonk_image_url": old_image}},
            )
        except Exception as e:
            log.error("DB update failed for player '%s': %s", name, e)
            return PlayerSyncResult(
                api_player_id=api_player_id, name=name,
                status="failed", old_image=old_image, new_image=cdn_url,
                error=f"DB update: {e}",
            )

        return PlayerSyncResult(
            api_player_id=api_player_id, name=name,
            status="updated", old_image=old_image, new_image=cdn_url,
        )
