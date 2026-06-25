"""S3 wrapper used for document and signature storage.

Mirrors the NestJS `S3Service` so callers can be ported with no behavior change.
"""
from __future__ import annotations

import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError
from loguru import logger

from app.core.config import settings


class S3Service:
    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
        self._bucket = settings.AWS_S3_BUCKET

    def upload_file(
        self,
        data: bytes,
        original_file_name: str,
        mime_type: str,
        folder: str = "documents",
    ) -> dict[str, str]:
        extension = original_file_name.rsplit(".", 1)[-1] if "." in original_file_name else "bin"
        key = f"{folder}/{uuid.uuid4()}.{extension}"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=mime_type,
        )
        return {"key": key, "url": self.get_public_url(key)}

    def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def get_presigned_upload_url(
        self, file_name: str, mime_type: str, folder: str = "documents", expires_in: int = 3600
    ) -> dict[str, str]:
        extension = file_name.rsplit(".", 1)[-1] if "." in file_name else "bin"
        key = f"{folder}/{uuid.uuid4()}.{extension}"
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentType": mime_type},
            ExpiresIn=expires_in,
        )
        return {"upload_url": url, "key": key}

    def delete_file(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def file_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def get_public_url(self, key: str) -> str:
        return f"https://{self._bucket}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"


_instance: S3Service | None = None


def s3_service() -> S3Service:
    """Lazily instantiate the singleton so import-time failures don't crash the app."""
    global _instance
    if _instance is None:
        try:
            _instance = S3Service()
        except Exception as exc:
            logger.warning(f"S3Service init failed; uploads will error until configured: {exc}")
            raise
    return _instance
