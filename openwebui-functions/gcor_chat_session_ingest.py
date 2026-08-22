"""
title: GCOR Chat Session Ingest
author: EedgeAI
version: 1.0.0
required_open_webui_version: 0.10.0
"""

import asyncio
import hashlib
import logging
from typing import Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("gcor_chat_session_ingest")


class Filter:
    """Archive the full chat session transcript into the GCOR knowledge pipeline.

    Companion to gcor_file_ingest.py, which handles files attached to a chat
    message. This filter handles the conversation itself: on every outlet
    (i.e. after each assistant turn finishes), it fetches the chat's current
    title + full message history back from OpenWebUI's own chat API and
    forwards it to the GCOR proxy's /api/ingest/session. That endpoint writes
    a JSON snapshot of the transcript to MinIO under
    chat-sessions/<session_id>/<date-time>_<name>.json — named with the real
    chat title once OpenWebUI has generated one, or a snippet of the first
    user message before that (OpenWebUI's placeholder title, e.g. "New Chat",
    is never used as the name) — and indexes it into Graphiti (group:
    chat_sessions by default), so past conversations become retrievable —
    not just uploaded documents.

    Ingestion runs in the background (fire-and-forget) so it never delays the
    chat response; failures are logged, not surfaced to the user. A session
    accumulates one snapshot object per assistant turn — that's intentional:
    it gives a timestamped history of how the conversation grew, not just its
    final state.
    """

    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Archive chat sessions into GCOR.")
        proxy_ingest_url: str = Field(
            default="http://proxy:5001/api/ingest/session",
            description="GCOR proxy chat-session ingest endpoint (docker-internal URL).",
        )
        openwebui_base_url: str = Field(
            default="http://localhost:8080",
            description="In-container URL used to fetch the chat's title + full history back from OpenWebUI's own API.",
        )
        collection: str = Field(
            default="",
            description="Target Graphiti group for chat sessions. Empty = proxy default (chat_sessions).",
        )
        access_level: str = Field(default="public")
        agent_id: str = Field(default="")
        min_messages: int = Field(
            default=2,
            description="Skip ingesting a session until it has at least this many messages.",
        )
        priority: int = Field(default=0)

    def __init__(self):
        self.valves = self.Valves()
        # chat_id -> md5 of the last transcript ingested, so an outlet firing
        # again on an unchanged conversation doesn't write a duplicate snapshot.
        self._last_hash: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        if not self.valves.enabled:
            return body

        chat_id = (
            body.get("chat_id")
            or (__metadata__ or {}).get("chat_id")
            or (__metadata__ or {}).get("session_id")
        )
        if not chat_id:
            return body

        auth_header = None
        cookie_header = None
        if __request__ is not None:
            auth_header = __request__.headers.get("authorization")
            cookie_header = __request__.headers.get("cookie")

        task = asyncio.create_task(
            self._ingest_session(chat_id, auth_header, cookie_header)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return body

    async def _ingest_session(
        self,
        chat_id: str,
        auth_header: Optional[str],
        cookie_header: Optional[str],
    ) -> None:
        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.valves.openwebui_base_url}/api/v1/chats/{chat_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                chat_data = resp.json()

            title = (chat_data.get("title") or "").strip()
            messages = _flatten_messages(chat_data.get("chat") or {})
            if len(messages) < self.valves.min_messages:
                return

            transcript_hash = hashlib.md5(
                "".join(f"{m.get('role')}:{m.get('content')}" for m in messages).encode(
                    "utf-8", errors="ignore"
                )
            ).hexdigest()
            if self._last_hash.get(chat_id) == transcript_hash:
                return  # unchanged since the last snapshot — nothing new to archive

            model = ""
            if messages:
                model = (chat_data.get("chat") or {}).get("models", [""])[0] if (
                    chat_data.get("chat") or {}
                ).get("models") else ""

            form = {
                "session_id": chat_id,
                "title": title,
                "model": model,
                "agent_id": self.valves.agent_id,
                "access_level": self.valves.access_level,
                "messages": messages,
            }
            if self.valves.collection:
                form["collection"] = self.valves.collection

            async with httpx.AsyncClient(timeout=60) as client:
                ingest_resp = await client.post(
                    self.valves.proxy_ingest_url, json=form, timeout=60,
                )
                ingest_resp.raise_for_status()

            self._last_hash[chat_id] = transcript_hash
            log.info(
                "[gcor_chat_session_ingest] archived chat_id=%s (%d messages) -> %s",
                chat_id, len(messages), ingest_resp.json().get("document_id"),
            )
        except Exception as exc:
            log.warning("[gcor_chat_session_ingest] failed for chat_id=%s: %s", chat_id, exc)


def _flatten_messages(chat: dict) -> list[dict]:
    """Normalize OpenWebUI's stored chat.messages (list in newer versions,
    id-keyed dict tree in older ones) into an ordered [{role, content}, ...]."""
    raw = chat.get("messages")
    items: list[dict]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = list(raw.values())
        items.sort(key=lambda m: m.get("timestamp") or 0)
    else:
        return []

    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role and content:
            out.append({"role": role, "content": content})
    return out
