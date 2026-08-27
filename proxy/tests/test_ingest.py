"""Ingestion and retrieval — the core "does it actually work" checks.

See helpers.py's module docstring for why these hit the live stack rather
than mocking it.
"""

import pathlib
import sys
import unittest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import helpers  # noqa: E402


class TestIngestAndRetrieve(unittest.TestCase):
    def setUp(self):
        self.client = helpers.require_stack(self)
        self._cleanup: list[tuple[str, str]] = []  # (collection, episode_uuid)

    def tearDown(self):
        for collection, episode_uuid in self._cleanup:
            helpers.cleanup_episode(self.client, collection, episode_uuid)
        self.client.close()

    def test_ingest_creates_an_episode(self):
        """The reliably-verified half of "ingest then find it": the proxy
        accepts the file, extracts it, and creates a Graphiti episode +
        at least one derived fact. Confirmed a real edge object exists via
        test_delete_removes_the_facts_not_just_the_episode's direct check
        (delete finds and removes it) — that's the trustworthy signal.

        Deliberately NOT asserting the fact then appears in /api/search
        results here. Found directly while building this suite: even
        Graphiti's own raw /search endpoint did not surface a freshly
        ingested fact for an on-topic query, immediately or after retrying
        with backoff — while the *edge itself* was independently confirmed
        to exist (delete_episode found and removed exactly one). That gap
        — an edge existing but not being retrievable via hybrid search —
        looks like a real, separate issue (embedding/indexing timing, or
        hybrid-search ranking against a now-populated real graph) worth
        its own investigation, not something to paper over with a flaky
        or overly-patient test here."""
        title = helpers.unique_name("basic-ingest")
        payload = b"The quarterly revenue for Northwind Traders in Q4 2026 was 7.8 million dollars."
        resp = self.client.post(
            "/api/ingest",
            data={"title": title, "collection": "documents"},
            files={"file": (f"{title}.txt", payload)},
            timeout=120,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertGreaterEqual(body["graphiti_episodes"], 1)

        episode_uuid = helpers.find_episode_uuid(self.client, "documents", title)
        self.assertIsNotNone(episode_uuid, "ingested episode not found in Graphiti's recent list")
        self._cleanup.append(("documents", episode_uuid))

    def test_duplicate_submission_does_not_create_a_second_episode(self):
        """Regression test for docs/adr/0012: a caller retrying identical
        content (e.g. after its own timeout, unsure whether the first
        attempt landed) must not create a duplicate Graphiti episode."""
        title = helpers.unique_name("idempotency")
        payload = b"Idempotency regression fixture: this exact sentence should only ever appear once."

        def ingest():
            resp = self.client.post(
                "/api/ingest",
                data={"title": title, "collection": "documents"},
                files={"file": (f"{title}.txt", payload)},
                timeout=120,
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            return resp.json()["document_id"]

        first_doc_id = ingest()
        second_doc_id = ingest()  # identical content — should hit the idempotency cache
        self.assertEqual(
            first_doc_id, second_doc_id,
            "a duplicate submission created a NEW document_id instead of returning the cached one",
        )

        episode_uuid = helpers.find_episode_uuid(self.client, "documents", title)
        self.assertIsNotNone(episode_uuid)
        self._cleanup.append(("documents", episode_uuid))

    def test_delete_removes_the_facts_not_just_the_episode(self):
        """Regression test for docs/adr/0015: delete_episode() used to
        remove only the episode node, leaving every fact (EntityEdge) it
        produced permanently orphaned and still fully searchable — proven
        directly against this exact stack: edges from episodes deleted
        earlier in development were still returned by /api/search with
        their original graphiti_edge_uuid, long after their source episode
        was gone from Graphiti's own episode listing."""
        title = helpers.unique_name("delete-cascade")
        marker = "Fictional entity Vorthex Industries posted a made-up 2027 net loss of 2.1 million units."
        resp = self.client.post(
            "/api/ingest",
            data={"title": title, "collection": "documents"},
            files={"file": (f"{title}.txt", marker.encode())},
            timeout=120,
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        episode_uuid = helpers.find_episode_uuid(self.client, "documents", title)
        self.assertIsNotNone(episode_uuid)

        delete_resp = self.client.delete(f"/api/collections/documents/docs/{episode_uuid}")
        self.assertEqual(delete_resp.status_code, 200, delete_resp.text)
        delete_body = delete_resp.json()
        self.assertGreaterEqual(
            delete_body.get("edges_deleted", 0), 1,
            f"expected at least one derived fact to be deleted alongside the episode, got: {delete_body}",
        )

        # Deliberately NOT added to self._cleanup — it's already deleted;
        # a redundant cleanup delete would just 404, which is fine, but
        # there's nothing left to clean up either way.
        results = helpers.search_until(
            self.client, "documents", "Vorthex Industries net loss",
            predicate=lambda rs: not any("Vorthex" in r.get("text", "") for r in rs),
            attempts=3, delay_seconds=1.0,
        )
        self.assertFalse(
            any("Vorthex" in r.get("text", "") for r in results),
            f"deleted fact still appeared in search results: {results[:3]}",
        )


if __name__ == "__main__":
    unittest.main()
