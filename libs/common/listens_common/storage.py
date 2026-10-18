"""Thin S3 wrapper (boto3). Targets Backblaze B2's S3-compatible API in production; the test
suite points the same client at an in-process moto server instead (see listens_common.testing)."""
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import boto3
from botocore.client import Config

from .config import Settings, get_settings


@lru_cache
def get_s3():
    s: Settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": s.s3_addressing_style}, retries={"max_attempts": 6, "mode": "standard"}),
    )


def delete_prefix(client, bucket: str, prefix: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objs})


def abort_stale_multipart(client, bucket: str, older_than: timedelta, prefix: str = "originals/") -> int:
    """Abort multipart uploads that were started but never finished (an upload the user walked away from). Their parts sit in
    the bucket and count as storage until the upload is aborted. Returns how many were aborted."""
    cutoff = datetime.now(UTC) - older_than
    aborted = 0
    kw = {"Bucket": bucket, "Prefix": prefix, "MaxUploads": 1000}
    while True:
        page = client.list_multipart_uploads(**kw)
        for upload in page.get("Uploads", []):
            if upload["Initiated"] < cutoff:
                client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
                aborted += 1
        if not page.get("IsTruncated"):
            return aborted
        kw["KeyMarker"], kw["UploadIdMarker"] = page.get("NextKeyMarker"), page.get("NextUploadIdMarker")
