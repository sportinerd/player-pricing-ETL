# app/utils/cdn.py — DigitalOcean Spaces client (S3-compatible via boto3)
#
# boto3 is synchronous; uploads are wrapped in asyncio.to_thread() so they
# don't block the async event loop.

import asyncio

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.utils.logger import get_logger

log = get_logger(__name__)


class DOSpacesClient:
    """Async-friendly wrapper around boto3 for DigitalOcean Spaces uploads."""

    def __init__(self):
        if not settings.DO_ACCESS_KEY_ID or not settings.DO_BUCKET_SECRET_KEY:
            raise ValueError(
                "DO_ACCESS_KEY_ID and DO_BUCKET_SECRET_KEY must be set in .env"
            )
        region = settings.DO_ORIGIN_ENDPOINT.split("//")[1].split(".digitaloceanspaces.com")[0].replace(f"{settings.DO_BUCKET_NAME}.", "")
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://{region}.digitaloceanspaces.com",
            aws_access_key_id=settings.DO_ACCESS_KEY_ID,
            aws_secret_access_key=settings.DO_BUCKET_SECRET_KEY,
        )

    def _upload_sync(self, image_bytes: bytes, key: str) -> str:
        """Blocking S3 put — called via asyncio.to_thread."""
        self._s3.put_object(
            Bucket=settings.DO_BUCKET_NAME,
            Key=key,
            Body=image_bytes,
            ContentType="image/png",
            ACL="public-read",
        )
        return f"{settings.DO_CDN_ENDPOINT}/{key}"

    async def upload_image(self, image_bytes: bytes, api_player_id: int) -> str:
        """
        Upload a player image PNG to DO Spaces and return its CDN URL.
        File path in bucket: {DO_IMAGES_FOLDER}/{api_player_id}.png
        """
        key = f"{settings.DO_IMAGES_FOLDER}/{api_player_id}.png"
        try:
            cdn_url = await asyncio.to_thread(self._upload_sync, image_bytes, key)
            log.info("Uploaded image for player %s → %s", api_player_id, cdn_url)
            return cdn_url
        except (BotoCoreError, ClientError) as e:
            log.error("CDN upload failed for player %s: %s", api_player_id, e)
            raise
