#!/usr/bin/env python3
"""Land source-system extracts in the lake's raw zone: RAW_DIR/** -> s3://BUCKET/raw/**

Skips objects that already exist with the same size, so re-runs only push changes.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

import config


def ensure_bucket(s3):
    try:
        s3.head_bucket(Bucket=config.BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=config.BUCKET)
        print(f"created bucket {config.BUCKET}")


def existing_sizes(s3, prefix):
    sizes = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            sizes[o["Key"]] = o["Size"]
    return sizes


def main():
    if not (config.RAW_DIR / "_manifest.json").exists():
        sys.exit(f"No generator output in {config.RAW_DIR}; run generator/generate.py first.")
    s3 = config.s3_client()
    ensure_bucket(s3)
    have = existing_sizes(s3, "raw/")
    files = [p for p in config.RAW_DIR.rglob("*") if p.is_file()]
    todo = [(p, "raw/" + p.relative_to(config.RAW_DIR).as_posix()) for p in files]
    todo = [(p, k) for p, k in todo if have.get(k) != p.stat().st_size]
    total = sum(p.stat().st_size for p, _ in todo)
    print(f"{len(files)} files in raw zone, {len(todo)} to upload ({total / 1e6:,.0f} MB)")

    tc = TransferConfig(multipart_threshold=64 * 2**20, max_concurrency=4)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(s3.upload_file, str(p), config.BUCKET, k, Config=tc): (p, k) for p, k in todo}
        for i, f in enumerate(as_completed(futs), 1):
            f.result()
            done += futs[f][0].stat().st_size
            if i % 50 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} files, {done / 1e6:,.0f} MB, {time.time() - t0:.0f}s")
    print("raw zone upload complete")


if __name__ == "__main__":
    main()
