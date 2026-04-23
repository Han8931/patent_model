"""S3 helpers — list and download Korean patent docx files from a bucket."""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from dotenv import load_dotenv


def _client():
    load_dotenv()
    kwargs = dict(
        region_name          = os.environ["AWS_REGION"],
        aws_access_key_id    = os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key= os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    endpoint_url = os.getenv("S3_ENDPOINT_URL")
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **kwargs)


def list_docx_keys(bucket: str, prefix: str = "") -> list[str]:
    """Return all .docx object keys under prefix in bucket."""
    s3 = _client()
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".docx"):
                keys.append(key)
    return keys


def download_files(
    keys: list[str],
    bucket: str,
    download_dir: str | Path,
) -> list[Path]:
    """Download a list of S3 keys into download_dir. Returns local paths."""
    s3 = _client()
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    local_paths: list[Path] = []
    for key in keys:
        local_path = download_dir / Path(key).name
        if local_path.exists():
            print(f"[S3] Already exists, skipping: {local_path.name}")
        else:
            print(f"[S3] Downloading: {key} → {local_path}")
            s3.download_file(bucket, key, str(local_path))
        local_paths.append(local_path)

    return local_paths


def fetch_inputs(
    bucket: str | None = None,
    prefix: str | None = None,
    download_dir: str | Path | None = None,
) -> list[Path]:
    """
    Convenience function: load config from .env, list all .docx files
    under prefix in bucket, download them, and return local paths.

    Any argument explicitly passed overrides the .env value.
    """
    load_dotenv()
    bucket       = bucket       or os.environ["S3_BUCKET"]
    prefix       = prefix       if prefix is not None else os.getenv("S3_PREFIX", "")
    download_dir = download_dir or os.getenv("S3_DOWNLOAD_DIR", "data/s3")

    keys = list_docx_keys(bucket, prefix)
    if not keys:
        print(f"[S3] No .docx files found in s3://{bucket}/{prefix}")
        return []

    print(f"[S3] Found {len(keys)} file(s) in s3://{bucket}/{prefix}")
    return download_files(keys, bucket, download_dir)
