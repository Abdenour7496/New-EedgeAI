"""
title: GCOR File Ingest
author: EedgeAI
version: 1.0.0
required_open_webui_version: 0.10.0
"""

import asyncio
import logging
import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("gcor_file_ingest")


class Filter:
    """Push files attached to a chat message into the GCOR knowledge pipeline.

    OpenWebUI already runs its own local RAG over attached files — this filter
    does not touch or replace that. It runs alongside it: on every inlet, any
    newly attached file (body["files"], type == "file") is fetched back from
    OpenWebUI's own file API and forwarded to the GCOR proxy's /api/ingest, the
    same endpoint the Knowledge UI uses. That lands the document in Graphiti
    (FalkorDB) and persists the original to MinIO, so it becomes retrievable
    from the Knowledge UI and OpenClaw too — not just this one OpenWebUI chat.

    Ingestion runs in the background (fire-and-forget) so it never delays the
    chat response; failures are logged, not surfaced to the user.
    """

    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Push attached files into GCOR.")
        proxy_ingest_url: str = Field(
            default="http://proxy:5001/api/ingest",
            description="GCOR proxy ingest endpoint (docker-internal URL).",
        )
        openwebui_base_url: str = Field(
            default="http://localhost:8080",
            description="In-container URL used to fetch the raw file back from OpenWebUI's own file API.",
        )
        collection: str = Field(
            default="",
            description="Target Graphiti group. Empty = proxy default (GRAPHITI_GROUP_ID).",
        )
        access_level: str = Field(default="public")
        agent_id: str = Field(default="")
        enable_docint: bool = Field(
            default=False,
            description="Run full Document Intelligence (OCR, tables, entities) on attached files.",
        )
        proxy_timeout_seconds: float = Field(
            default_factory=lambda: float(os.environ.get("GRAPHITI_INGEST_TIMEOUT_SECONDS", "1800")),
            description="How long to wait for the proxy's /api/ingest call, which blocks on "
                        "synchronous Graphiti extraction. Defaults to the same "
                        "GRAPHITI_INGEST_TIMEOUT_SECONDS env var the proxy itself uses (see "
                        "docs/adr/0010), matching gcor_chat_session_ingest.py's same fix — "
                        "was previously a hardcoded 300s, shorter than the proxy's own patience.",
        )
        gcor_api_key: str = Field(
            default_factory=lambda: os.environ.get("GCOR_API_KEY", ""),
            description="Shared secret the GCOR proxy requires on /api/* (its GCOR_API_KEY). "
                        "Defaults to this container's own GCOR_API_KEY env var; override here "
                        "only if this Function needs a different value.",
        )
        priority: int = Field(default=0)

    def __init__(self):
        self.valves = self.Valves()
        self._ingested_ids: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        if not self.valves.enabled:
            return body

        files = body.get("files") or []
        if not files:
            return body

        auth_header = None
        cookie_header = None
        if __request__ is not None:
            auth_header = __request__.headers.get("authorization")
            cookie_header = __request__.headers.get("cookie")

        for item in files:
            if item.get("type") != "file":
                continue
            file_id = item.get("id")
            filename = item.get("name") or file_id
            if not file_id or file_id in self._ingested_ids:
                continue
            self._ingested_ids.add(file_id)

            task = asyncio.create_task(
                self._ingest_file(file_id, filename, auth_header, cookie_header)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return body

    async def _ingest_file(
        self,
        file_id: str,
        filename: str,
        auth_header: Optional[str],
        cookie_header: Optional[str],
    ) -> None:
        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{self.valves.openwebui_base_url}/api/v1/files/{file_id}/content",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.content

                form = {
                    "title": filename,
                    "agent_id": self.valves.agent_id,
                    "access_level": self.valves.access_level,
                    "enable_docint": "true" if self.valves.enable_docint else "false",
                    "collection": self.valves.collection,
                }
                proxy_headers = (
                    {"Authorization": f"Bearer {self.valves.gcor_api_key}"}
                    if self.valves.gcor_api_key else {}
                )
                ingest_resp = await client.post(
                    self.valves.proxy_ingest_url,
                    data=form,
                    files={"file": (filename, data)},
                    headers=proxy_headers,
                    timeout=self.valves.proxy_timeout_seconds,
                )
                ingest_resp.raise_for_status()
                log.info(
                    "[gcor_file_ingest] ingested %s (file_id=%s) -> %s",
                    filename, file_id, ingest_resp.json().get("document_id"),
                )
        except Exception as exc:
            self._ingested_ids.discard(file_id)
            log.warning(
                "[gcor_file_ingest] failed to ingest %s (file_id=%s): %s: %s",
                filename, file_id, type(exc).__name__, exc,
            )
