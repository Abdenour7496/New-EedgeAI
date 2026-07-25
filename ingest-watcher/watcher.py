"""
ingest-watcher — bridges MinIO document storage into the GCOR knowledge base.

Drop a file into the "documents" bucket under inbox/ (via mc, an S3 SDK, or
any S3-compatible client pointed at MinIO) and this polls the prefix, pushes
each new object to the proxy's POST /api/ingest, then moves it out of inbox/
so it isn't reprocessed:

  success → processed/<same relative path>
  failure → failed/<same relative path>  (+ a sibling .error.txt with the reason)

This exists because /api/ingest is a synchronous upload endpoint with no
notion of a durable drop folder — MinIO is the durable storage, this is the
thing that notices new arrivals and feeds them in.
"""

import logging
import os
import time

import boto3
import httpx
from botocore.client import Config as BotoConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s ingest-watcher: %(message)s")
log = logging.getLogger("ingest-watcher")

S3_ENDPOINT_URL  = os.environ["S3_ENDPOINT_URL"]
S3_ACCESS_KEY    = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY    = os.environ["S3_SECRET_KEY"]
S3_BUCKET        = os.environ.get("S3_BUCKET", "documents")
INBOX_PREFIX     = os.environ.get("S3_INBOX_PREFIX", "inbox/")
PROCESSED_PREFIX = os.environ.get("S3_PROCESSED_PREFIX", "processed/")
FAILED_PREFIX    = os.environ.get("S3_FAILED_PREFIX", "failed/")

PROXY_INGEST_URL = os.environ.get("PROXY_INGEST_URL", "http://proxy:5001/api/ingest")
POLL_INTERVAL    = float(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

INGEST_COLLECTION    = os.environ.get("INGEST_COLLECTION", "")
INGEST_ENABLE_DOCINT = os.environ.get("INGEST_ENABLE_DOCINT", "false")
INGEST_ACCESS_LEVEL  = os.environ.get("INGEST_ACCESS_LEVEL", "public")
INGEST_AGENT_ID      = os.environ.get("INGEST_AGENT_ID", "")

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT_URL,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=BotoConfig(signature_version="s3v4"),
    region_name="us-east-1",
)


def _list_inbox():
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=INBOX_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            yield key


def _move(src_key: str, dest_key: str):
    s3.copy_object(Bucket=S3_BUCKET, CopySource={"Bucket": S3_BUCKET, "Key": src_key}, Key=dest_key)
    s3.delete_object(Bucket=S3_BUCKET, Key=src_key)


def _ingest_one(key: str):
    rel = key[len(INBOX_PREFIX):]
    filename = rel.rsplit("/", 1)[-1]
    log.info("picked up %s", key)

    data = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()

    form = {
        "title": filename,
        "agent_id": INGEST_AGENT_ID,
        "access_level": INGEST_ACCESS_LEVEL,
        "enable_docint": INGEST_ENABLE_DOCINT,
        "collection": INGEST_COLLECTION,
    }
    try:
        resp = httpx.post(PROXY_INGEST_URL, data=form, files={"file": (filename, data)}, timeout=300)
        resp.raise_for_status()
        log.info("ingested %s -> document_id=%s", key, resp.json().get("document_id"))
        _move(key, f"{PROCESSED_PREFIX}{rel}")
    except Exception as exc:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        log.error("ingest failed for %s: %s", key, body)
        _move(key, f"{FAILED_PREFIX}{rel}")
        s3.put_object(Bucket=S3_BUCKET, Key=f"{FAILED_PREFIX}{rel}.error.txt", Body=body.encode())


def main():
    log.info("watching s3://%s/%s every %ss -> %s", S3_BUCKET, INBOX_PREFIX, POLL_INTERVAL, PROXY_INGEST_URL)
    while True:
        try:
            for key in _list_inbox():
                _ingest_one(key)
        except Exception:
            log.exception("poll loop error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
