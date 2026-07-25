import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request


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


class _FakeAsyncClient:
    def __init__(self, payloads):
        self._payloads = payloads

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        if url.endswith("/collections"):
            return _FakeResponse(self._payloads["collections"])

        name = url.rsplit("/", 1)[-1]
        return _FakeResponse(self._payloads["details"][name])


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


class _NoGetAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        raise AssertionError("A workspace-scoped search must not discover extra collections")


def _request(headers: dict[str, str] | None = None) -> Request:
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

    async def test_qdrant_search_scopes_to_selected_collection(self):
        hit = {
            "id": "finance-hit",
            "score": 0.85,
            "payload": {},
            "_collection": "bills_and_expenses",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=_NoGetAsyncClient()), \
             patch.object(
                 main, "_qdrant_search_collection", new=AsyncMock(return_value=[hit])
             ) as search_mock:
            results = await main.qdrant_search([0.1, 0.2], collection="bills_and_expenses")

        search_mock.assert_awaited_once()
        self.assertEqual(search_mock.await_args.args[1], "bills_and_expenses")
        self.assertEqual(results[0]["_collection"], "bills_and_expenses")

    def test_memory_expand_query_uses_aggregated_event_ordering(self):
        memory_query = main._EXPAND_CYPHER["memory"]

        self.assertIn("max(evt.timestamp) AS latest_event_ts", memory_query)
        self.assertIn("ORDER BY latest_event_ts DESC", memory_query)
        self.assertNotIn("ORDER BY evt.timestamp DESC", memory_query)

    async def test_api_collection_docs_uses_collection_doc_ids(self):
        docs = [
            {
                "doc_id": "doc-x",
                "title": "Doc X",
                "created_at": "2026-04-05T14:01:39Z",
                "access_level": "public",
                "chunk_count": 1,
            }
        ]

        with patch.object(main, "_qdrant_collection_doc_ids", new=AsyncMock(return_value=["doc-x"])) as doc_ids_mock, \
             patch.object(main, "_neo4j_fetch_documents", new=AsyncMock(return_value=docs)) as fetch_docs_mock:
            result = await main.api_collection_docs("ingestion_smoketest")

        self.assertEqual(result["collection"], "ingestion_smoketest")
        self.assertEqual(result["docs"], docs)
        doc_ids_mock.assert_awaited_once_with("ingestion_smoketest", limit=200)
        fetch_docs_mock.assert_awaited_once_with(["doc-x"])

    async def test_api_collection_docs_preserves_not_found_status(self):
        not_found = main.HTTPException(status_code=404, detail="Collection missing")

        with patch.object(main, "_qdrant_collection_doc_ids", new=AsyncMock(side_effect=not_found)):
            with self.assertRaises(main.HTTPException) as exc_info:
                await main.api_collection_docs("missing")

        self.assertEqual(exc_info.exception.status_code, 404)

    async def test_api_collections_enriches_each_collection_independently(self):
        payloads = {
            "collections": {
                "result": {
                    "collections": [
                        {"name": "documents"},
                        {"name": "buddy_memory"},
                    ]
                }
            },
            "details": {
                "documents": {"result": {"points_count": 3}},
                "buddy_memory": {"result": {"points_count": 1}},
            },
        }
        fake_client = _FakeAsyncClient(payloads)
        doc_side_effect = [["doc-a", "doc-b"], []]
        recent_docs_side_effect = [[
            {
                "doc_id": "doc-a",
                "title": "Doc A",
                "created_at": "2026-04-05T14:01:39Z",
                "access_level": "public",
                "chunk_count": 2,
            },
            {
                "doc_id": "doc-b",
                "title": "Doc B",
                "created_at": "2026-04-05T14:01:40Z",
                "access_level": "restricted",
                "chunk_count": 1,
            },
        ], []]

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client), \
             patch.object(main, "_qdrant_collection_doc_ids", new=AsyncMock(side_effect=doc_side_effect)) as doc_ids_mock, \
             patch.object(main, "_neo4j_fetch_documents", new=AsyncMock(side_effect=recent_docs_side_effect)) as fetch_docs_mock:
            result = await main.api_collections()

        collections = {col["name"]: col for col in result["collections"]}

        self.assertEqual(collections["documents"]["doc_count"], 2)
        self.assertEqual(len(collections["documents"]["recent_docs"]), 2)
        self.assertEqual(collections["documents"]["recent_docs"][0]["doc_id"], "doc-a")
        self.assertEqual(collections["buddy_memory"]["doc_count"], 0)
        self.assertEqual(collections["buddy_memory"]["recent_docs"], [])
        self.assertEqual(doc_ids_mock.await_count, 2)
        fetch_docs_mock.assert_any_await(["doc-a", "doc-b"], limit=5)
        fetch_docs_mock.assert_any_await([], limit=5)

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