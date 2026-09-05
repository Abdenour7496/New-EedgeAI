"""
Shared test utilities — integration tests against the LIVE running stack.

These are not unit tests with mocks (see test_collection_metadata.py /
test_governance.py for that style, which IS the CI release gate — see
.github/workflows/ci.yml and docs/adr/0005). This session's real bugs
(message-flattening reading the wrong field, timeout mismatches between a
client and the proxy, duplicate episodes from a naive retry, a shared
concurrency lane getting starved) were all integration-level — a caller, a
real backend, real timing. A mocked unit test would not have caught any of
them. Same category as test_openwebui_stream_smoke.py: a post-deploy
check, not a CI gate — needs a live stack (GCOR_API_KEY, a reachable
proxy), same as that one needs PROXY_CHAT_URL. See
docs/adr/0015-integration-test-suite.md.

Run via (from inside the proxy container, where httpx/the env vars it
needs already are):
    docker exec eedgeai-proxy-1 python3 -m unittest tests/test_ingest.py -v

Skips (not failures) if the proxy isn't reachable or GCOR_API_KEY isn't
set, so accidentally running this in an environment without a live stack
reports "skipped", not a false failure.
"""

from __future__ import annotations

import os
import time
import unittest
import uuid
from typing import Callable

import httpx

BASE_URL = os.environ.get("TEST_PROXY_BASE_URL", "http://localhost:5001")
GCOR_API_KEY = os.environ.get("GCOR_API_KEY", "")
AUTH_HEADERS = {"Authorization": f"Bearer {GCOR_API_KEY}"} if GCOR_API_KEY else {}

# Every test-created document/session gets this prefix so it's unambiguous
# in Graphiti/MinIO which episodes are test fixtures versus real user data —
# and so cleanup_episode() below can find them without guessing.
TEST_MARKER = "eedgeai-test-"


def unique_name(label: str) -> str:
    return f"{TEST_MARKER}{label}-{uuid.uuid4().hex[:8]}"


def require_stack(test_case: unittest.TestCase) -> httpx.Client:
    """Return a configured httpx.Client, or skip the test if the proxy
    (and by extension the auth key it needs) isn't reachable — this suite
    is meant to run against a live stack, not simulate one being absent."""
    client = httpx.Client(base_url=BASE_URL, headers=AUTH_HEADERS, timeout=30)
    try:
        r = client.get("/health")
        r.raise_for_status()
    except Exception as exc:
        client.close()
        test_case.skipTest(f"proxy not reachable at {BASE_URL}: {exc}")
    if not GCOR_API_KEY:
        client.close()
        test_case.skipTest("GCOR_API_KEY not set in this environment — required to call /api/* and /v1/*")
    return client


def cleanup_episode(client: httpx.Client, collection: str, episode_uuid: str) -> None:
    """Best-effort delete — a test's own teardown failing to find/delete an
    episode (e.g. because the test itself failed before creating one)
    should never mask the original assertion failure."""
    try:
        client.delete(f"/api/collections/{collection}/docs/{episode_uuid}", timeout=15)
    except Exception:
        pass


def search_until(
    client: httpx.Client, collection: str, query: str,
    predicate: Callable[[list[dict]], bool],
    attempts: int = 5, delay_seconds: float = 2.0,
) -> list[dict]:
    """Poll /api/search until `predicate(results)` is true or attempts run
    out. Observed directly: a fact from an ingest that had just returned
    200 was not always immediately present/highly-ranked in search results
    on the very next call — a short propagation lag, not a bug in the
    ingest response itself (confirmed separately: the episode and its
    edge both existed immediately, checked directly rather than via
    search ranking). Retrying with backoff avoids a flaky false failure
    for that lag while still catching an actual missing-fact regression."""
    results: list[dict] = []
    for attempt in range(attempts):
        resp = client.get("/api/search", params={"collection": collection, "q": query})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if predicate(results):
            return results
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return results


def find_episode_uuid(client: httpx.Client, group_id: str, name_contains: str) -> str | None:
    """Graphiti's /episodes/{group} doesn't support filtering server-side —
    fetch recent ones and find by name substring, same pattern used
    throughout this session's manual verification."""
    try:
        r = client.get(f"http://graphiti:8000/episodes/{group_id}", params={"last_n": 20})
        r.raise_for_status()
    except Exception:
        return None
    for ep in r.json():
        if name_contains in (ep.get("name") or ""):
            return ep.get("uuid")
    return None
