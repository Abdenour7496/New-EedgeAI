import pathlib
import sys
import unittest
from unittest.mock import patch


PROXY_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import main  # noqa: E402


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGetClient:
    """Mimics httpx.AsyncClient for GET-only callers (api_collections,
    api_collection_docs), routing by the last URL path segment."""

    def __init__(self, payloads_by_name=None, payload=None):
        self._payloads_by_name = payloads_by_name
        self._payload = payload
        self.requested_urls = []
        self.requested_params = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        self.requested_urls.append(url)
        self.requested_params.append(params)
        if self._payloads_by_name is not None:
            name = url.rsplit("/", 1)[-1]
            return _FakeResponse(self._payloads_by_name.get(name, []))
        return _FakeResponse(self._payload)


class _FailingGetClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        raise RuntimeError("graphiti unreachable")


class _FakePostClient:
    """Mimics httpx.AsyncClient for graphiti_search's single POST /search call."""

    def __init__(self, payload):
        self._payload = payload
        self.requested_url = None
        self.requested_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        self.requested_url = url
        self.requested_json = json
        return _FakeResponse(self._payload)


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStreamingClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, json, headers):
        return self._response


def _request(headers: dict[str, str] | None = None):
    from starlette.requests import Request

    return Request({
        "type": "http",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
    })


class CollectionMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_request_rag_collection_accepts_body_selector(self):
        body = {"gcor_collection": "  bills_and_expenses  "}

        collection = main._request_rag_collection(body, _request())

        self.assertEqual(collection, "bills_and_expenses")
        self.assertNotIn("gcor_collection", body)

    def test_request_rag_collection_prefers_header(self):
        body = {"gcor_collection": "documents"}

        collection = main._request_rag_collection(
            body, _request({"X-GCOR-Collection": "doc_archive"})
        )

        self.assertEqual(collection, "doc_archive")
        self.assertNotIn("gcor_collection", body)

    def test_request_rag_collection_rejects_path_characters(self):
        with self.assertRaises(main.HTTPException) as exc_info:
            main._request_rag_collection(
                {"gcor_collection": "documents/other"}, _request()
            )

        self.assertEqual(exc_info.exception.status_code, 400)

    async def test_graphiti_search_maps_facts_to_hits_and_records(self):
        facts_payload = {
            "facts": [
                {
                    "uuid": "fact-1",
                    "fact": "Invoice #42 is due 2026-09-01",
                    "name": "Invoice #42",
                    "episodes": ["ep-1"],
                    "valid_at": "2026-08-01T00:00:00Z",
                },
                {
                    "uuid": "fact-2",
                    "fact": "Invoice #43 was paid",
                    "name": "Invoice #43",
                    "episodes": ["ep-2"],
                },
            ]
        }
        fake_client = _FakePostClient(facts_payload)

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client):
            hits, records = await main.graphiti_search("invoice status", group_id="documents")

        self.assertTrue(fake_client.requested_url.endswith("/search"))
        self.assertEqual(fake_client.requested_json["group_ids"], ["documents"])
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["id"], "fact-1")
        self.assertEqual(hits[0]["payload"]["document_id"], "ep-1")
        self.assertEqual(hits[0]["payload"]["access_level"], "public")
        self.assertEqual(hits[0]["payload"]["confidence"], 1.0)
        # Rank 0 scores strictly higher than rank 1.
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertEqual(records[0]["labels"], ["TemporalFact"])

    async def test_api_collection_docs_maps_graphiti_episodes(self):
        episodes = [
            {"uuid": "ep-1", "name": "Doc A", "created_at": "2026-04-05T14:01:39Z"},
            {"uuid": "ep-2", "name": "Doc B", "valid_at": "2026-04-06T09:00:00Z"},
        ]
        fake_client = _FakeGetClient(payload=episodes)

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client):
            result = await main.api_collection_docs("documents")

        self.assertEqual(result["collection"], "documents")
        self.assertEqual(len(result["docs"]), 2)
        self.assertEqual(result["docs"][0]["doc_id"], "ep-1")
        self.assertEqual(result["docs"][0]["title"], "Doc A")
        self.assertEqual(result["docs"][0]["created_at"], "2026-04-05T14:01:39Z")
        # Falls back to valid_at when created_at is absent.
        self.assertEqual(result["docs"][1]["created_at"], "2026-04-06T09:00:00Z")
        self.assertTrue(fake_client.requested_urls[0].endswith("/episodes/documents"))

    async def test_api_collection_docs_raises_502_on_upstream_failure(self):
        with patch.object(main.httpx, "AsyncClient", return_value=_FailingGetClient()):
            with self.assertRaises(main.HTTPException) as exc_info:
                await main.api_collection_docs("documents")

        self.assertEqual(exc_info.exception.status_code, 502)

    async def test_api_collections_reports_per_group_episode_counts(self):
        payloads_by_name = {
            "documents": [
                {"uuid": "ep-1", "name": "Doc A", "created_at": "2026-04-05T14:01:39Z"},
                {"uuid": "ep-2", "name": "Doc B", "created_at": "2026-04-06T09:00:00Z"},
            ],
            "buddy_memory": [],
            "chat_sessions": [],
        }
        fake_client = _FakeGetClient(payloads_by_name=payloads_by_name)

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client), \
             patch.object(main, "GRAPHITI_GROUP_ID", "documents"), \
             patch.object(main, "RAG_EXTRA_COLLECTIONS", ["buddy_memory"]), \
             patch.object(main, "GRAPHITI_CHAT_SESSIONS_GROUP_ID", "chat_sessions"):
            result = await main.api_collections()

        by_name = {c["name"]: c for c in result["collections"]}
        self.assertEqual(set(by_name), {"documents", "buddy_memory", "chat_sessions"})
        self.assertEqual(by_name["documents"]["doc_count"], 2)
        self.assertEqual(len(by_name["documents"]["recent_docs"]), 2)
        self.assertEqual(by_name["documents"]["recent_docs"][0]["doc_id"], "ep-1")
        self.assertEqual(by_name["buddy_memory"]["doc_count"], 0)
        self.assertEqual(by_name["buddy_memory"]["recent_docs"], [])

    async def test_call_openclaw_stream_adds_terminal_stop_chunk(self):
        upstream_lines = [
            'data: {"id":"chatcmpl_upstream","object":"chat.completion.chunk","created":1775389806,"model":"openclaw","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
            'data: {"id":"chatcmpl_upstream","object":"chat.completion.chunk","created":1775389806,"model":"openclaw","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}',
            'data: [DONE]',
        ]
        fake_client = _FakeStreamingClient(_FakeStreamResponse(upstream_lines))

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client):
            response = await main.call_openclaw({
                "model": "openclaw",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            })
            chunks = [chunk async for chunk in response.body_iterator]

        payload = b"".join(chunks).decode()

        self.assertIn('"delta": {"role": "assistant"}, "finish_reason": null', payload)
        self.assertIn('"delta": {}, "finish_reason": "stop"', payload)
        self.assertTrue(payload.rstrip().endswith('data: [DONE]'))


if __name__ == "__main__":
    unittest.main()
