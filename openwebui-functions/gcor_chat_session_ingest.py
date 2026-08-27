"""
title: GCOR Chat Session Ingest
author: EedgeAI
version: 1.0.0
required_open_webui_version: 0.10.0
"""

import asyncio
import hashlib
import logging
import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("gcor_chat_session_ingest")


class Filter:
    """Archive the full chat session transcript into the GCOR knowledge pipeline.

    Companion to gcor_file_ingest.py, which handles files attached to a chat
    message. This filter handles the conversation itself: it fetches the
    chat's current title + full message history back from OpenWebUI's own
    chat API and forwards it to the GCOR proxy's /api/ingest/session. That
    endpoint writes a JSON snapshot of the transcript to MinIO under
    chat-sessions/<session_id>/<date-time>_<name>.json — named with the real
    chat title once OpenWebUI has generated one, or a snippet of the first
    user message before that (OpenWebUI's placeholder title, e.g. "New Chat",
    is never used as the name) — and indexes it into Graphiti (group:
    chat_sessions by default), so past conversations become retrievable —
    not just uploaded documents.

    Debounced, not fired on every turn: each snapshot re-sends the FULL
    transcript (not a diff), so archiving after every single assistant turn
    would re-run full extraction over the whole growing conversation every
    time — wasted, redundant work that also stacks concurrent extraction
    jobs for the same chat under active back-and-forth use. Instead, every
    outlet (re)starts a debounce_seconds timer for that chat_id, cancelling
    any timer already pending for it; the transcript only actually archives
    once the conversation goes quiet for that long. A guard also skips
    starting a new archive if one is already in flight for that chat_id
    (e.g. a slow extraction from an earlier quiet period still running).

    Ingestion runs in the background (fire-and-forget) so it never delays
    the chat response; failures are logged, not surfaced to the user.
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
        debounce_seconds: float = Field(
            default=60.0,
            description="Wait for this many seconds of quiet on a chat before archiving it — "
                        "each new assistant turn resets the timer for that chat. Coalesces a "
                        "burst of back-and-forth turns into one archive call instead of "
                        "re-extracting the whole growing transcript after every single turn.",
        )
        proxy_timeout_seconds: float = Field(
            default_factory=lambda: float(os.environ.get("GRAPHITI_INGEST_TIMEOUT_SECONDS", "1800")),
            description="How long to wait for the proxy's /api/ingest/session call, which blocks "
                        "on synchronous Graphiti extraction. Defaults to the same "
                        "GRAPHITI_INGEST_TIMEOUT_SECONDS env var the proxy itself uses, so this "
                        "Function never gives up before the proxy would.",
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
        # chat_id -> md5 of the last transcript ingested, so an outlet firing
        # again on an unchanged conversation doesn't write a duplicate snapshot.
        self._last_hash: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        # chat_id -> its currently-pending debounce timer task, so a new
        # turn can cancel and replace it rather than stacking another one.
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        # chat_ids with an archive actively in flight right now (past the
        # debounce wait, mid-extraction) — belt-and-suspenders against a
        # slow archive from an earlier quiet period still running when a
        # new one would otherwise start.
        self._in_flight: set[str] = set()

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

        if chat_id in self._in_flight:
            # An archive for this chat is already past its debounce wait
            # and actively running — let it finish rather than cancelling
            # it (a task in _debounce_tasks is ONLY ever still in its
            # debounce sleep, never mid-archive; checking _in_flight first,
            # before touching _debounce_tasks at all, keeps that invariant
            # true and avoids a race — verified directly: without this
            # ordering, a turn arriving mid-archive cancelled the in-flight
            # task itself via the "supersede" logic below, its finally:
            # cleared _in_flight, and a second archive started concurrently).
            # The next turn after this one finishes will debounce normally.
            return body

        # A new turn on this chat supersedes any debounce timer still
        # waiting to fire — restart the quiet-period clock instead of
        # letting both eventually archive.
        pending = self._debounce_tasks.get(chat_id)
        if pending is not None and not pending.done():
            pending.cancel()

        task = asyncio.create_task(
            self._debounced_ingest(chat_id, auth_header, cookie_header)
        )
        self._debounce_tasks[chat_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return body

    async def _debounced_ingest(
        self,
        chat_id: str,
        auth_header: Optional[str],
        cookie_header: Optional[str],
    ) -> None:
        try:
            await asyncio.sleep(self.valves.debounce_seconds)
        except asyncio.CancelledError:
            return  # a newer turn on this chat superseded this timer

        self._in_flight.add(chat_id)
        try:
            await self._ingest_session(chat_id, auth_header, cookie_header)
        finally:
            self._in_flight.discard(chat_id)

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

            proxy_headers = (
                {"Authorization": f"Bearer {self.valves.gcor_api_key}"}
                if self.valves.gcor_api_key else {}
            )
            # Graphiti extraction on a local model genuinely takes several
            # minutes per episode (see docs/adr/0003) — the proxy itself
            # waits up to GRAPHITI_INGEST_TIMEOUT_SECONDS (default 1800s)
            # before giving up on it. A short client-side timeout here just
            # means *we* give up first and log a ReadTimeout, even though
            # the episode still lands a bit later. Match the proxy's budget.
            async with httpx.AsyncClient(timeout=self.valves.proxy_timeout_seconds) as client:
                ingest_resp = await client.post(
                    self.valves.proxy_ingest_url, json=form, headers=proxy_headers,
                    timeout=self.valves.proxy_timeout_seconds,
                )
                ingest_resp.raise_for_status()

            self._last_hash[chat_id] = transcript_hash
            log.info(
                "[gcor_chat_session_ingest] archived chat_id=%s (%d messages) -> %s",
                chat_id, len(messages), ingest_resp.json().get("document_id"),
            )
        except Exception as exc:
            log.warning(
                "[gcor_chat_session_ingest] failed for chat_id=%s: %s: %s",
                chat_id, type(exc).__name__, exc,
            )


def _flatten_messages(chat: dict) -> list[dict]:
    """Normalize OpenWebUI's stored chat into an ordered [{role, content}, ...].

    chat.history.messages (an id-keyed dict) is the authoritative, complete
    conversation tree. chat.messages (a flat array) is NOT reliably kept in
    sync with it — observed directly: after a real user/assistant exchange,
    chat.messages held only the latest user turn (1 item) while
    chat.history.messages had the full 6-message history. Prefer history;
    fall back to the flat array only if history is missing (defensive, not
    observed as a real case)."""
    history = chat.get("history")
    raw = history.get("messages") if isinstance(history, dict) else None
    if not raw:
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
