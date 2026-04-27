"""Download Korean patent .docx files from S3 to a local directory."""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from translate.s3 import fetch_inputs


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Download .docx files from an S3 bucket to a local directory."
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET"),
        help="S3 bucket name (default: S3_BUCKET from .env)",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("S3_PREFIX", ""),
        help="Key prefix to filter (default: S3_PREFIX from .env)",
    )
    parser.add_argument(
        "--dir",
        default=os.getenv("S3_DOWNLOAD_DIR", "data/s3"),
        help="Local directory to download into (default: S3_DOWNLOAD_DIR from .env)",
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.getenv("S3_ENDPOINT_URL"),
        help="S3-compatible endpoint URL for MinIO, R2, etc. (default: S3_ENDPOINT_URL from .env)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.bucket:
        raise SystemExit("Error: S3 bucket not specified. Set S3_BUCKET in .env or pass --bucket.")

    print(f"Downloading from s3://{args.bucket}/{args.prefix} → {args.dir}")
    paths = fetch_inputs(
        bucket=args.bucket,
        prefix=args.prefix,
        download_dir=args.dir,
        endpoint_url=args.endpoint_url,
    )

    print(f"\nDownloaded {len(paths)} file(s) to {Path(args.dir).resolve()}")


if __name__ == "__main__":
    main()
