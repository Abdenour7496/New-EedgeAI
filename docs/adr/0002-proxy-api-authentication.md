# ADR 0002: Shared-secret authentication for the GCOR proxy, OpenWebUI as the user-facing front door

**Status:** Accepted
**Date:** 2026-08-22
**Decision owners:** EedgeAI maintainers

## Context

A production-readiness review found that `/api/*` (ingest, search, collection
management) and `/v1/*` (chat completions, models, embeddings) on the GCOR
proxy had no authentication at all — any client that could reach port 5001
could read or write the entire knowledge base. `openwebui-functions/README.md`
already documented the assumption in place at the time: "the current proxy
accepts the collection selector from a trusted OpenWebUI request. Do not
expose the proxy directly to untrusted clients." That assumption was never
enforced in code, and the proxy's port was published to the host by default.

A second, related bug: `/api/search` returned raw Graphiti hits, bypassing
`_filter_hits()` — the confidence/temporal-validity/access-control filter the
chat RAG path (`_run_chat_completion`) already applied. A document marked
`access_level="restricted"` was correctly hidden from chat but fully
retrievable via `/api/search`.

## Decision drivers

- Close the unauthenticated read/write hole without inventing a second,
  bespoke user-login system alongside OpenWebUI's own.
- OpenWebUI already has full user accounts, sessions, and JWT-based auth
  (`WEBUI_AUTH`) — it is the intended user-facing interface. The proxy and its
  Knowledge UI (`/knowledge`) are operational/service surfaces, not a second
  product front door.
- Every non-human caller of the proxy (OpenWebUI's backend, its Functions,
  `ingest-watcher`, admin scripts) is already configured via environment
  variables / Valve settings — a shared secret fits that model with no new
  moving parts.

## Decision

1. **Shared-secret auth on the proxy.** `GCOR_API_KEY` gates every `/api/*`
   and `/v1/*` route via an ASGI middleware (`proxy/main.py`). Callers send
   `Authorization: Bearer <GCOR_API_KEY>`. If `GCOR_API_KEY` is unset, auth
   stays open (dev convenience, consistent with this repo's fail-open-with-a-
   warning pattern elsewhere) but logs a startup warning so it can't be
   silently shipped that way.
2. **OpenWebUI is the authenticated human front door**, not the proxy or the
   Knowledge UI. `WEBUI_AUTH` stays on; `WEBUI_SECRET_KEY` is now pinned via
   env (previously left to auto-generate, which invalidates sessions on every
   restart); `ENABLE_SIGNUP` defaults closed after the first admin bootstrap.
   Human users never talk to the proxy directly — OpenWebUI's backend and its
   installed Functions do, using `GCOR_API_KEY`.
3. **Every trusted caller gets the key via configuration:**
   `openwebui`'s `OPENAI_API_KEY`/`RAG_OPENAI_API_KEY` are now
   `${GCOR_API_KEY}` instead of the fixed string `"openclaw"`; `ingest-watcher`
   and the three `openwebui-functions/gcor_*.py` Functions gained a
   `gcor_api_key` setting and attach the header on their calls into the proxy.
4. **`/api/search` now calls `_filter_hits()`**, so direct search and the chat
   RAG path enforce the same access policy.
5. **The proxy's host port is no longer published on all interfaces** by
   default — see the compose file's port bindings. Admin/ops surfaces
   (Knowledge UI, Graphiti REST docs, Grafana, Prometheus, MinIO console,
   FalkorDB Browser, OpenClaw control UI) are reached via `127.0.0.1` (SSH
   tunnel / `kubectl port-forward` in a real deployment), not the open
   network. OpenWebUI (`8080`) remains the one port meant to be reachable by
   end users — put a real TLS-terminating reverse proxy in front of it for
   anything beyond a trusted LAN.

## Consequences

Positive: closes the unauthenticated data-exposure gap without a second auth
system; `/api/search` and the chat path can no longer disagree about what a
caller is allowed to see; OpenWebUI's sessions survive a restart.

Negative: every deployment must now set `GCOR_API_KEY` (and propagate it to
`ingest-watcher` and the three Functions' Valves) for the stack to be
protected — a config step, not a code guarantee. `/knowledge` still has no
per-user identity of its own; it now carries a Bearer token instead (entered
once, stored in the browser's `localStorage`), which is sufficient for a
single shared operator surface but is not multi-user access control. If
per-user attribution or per-workspace isolation is needed on the Knowledge UI
itself, that requires a real identity provider (e.g. Supabase, as
`openwebui-functions/README.md` already flagged for `gcor_collection_scope`),
not just a shared key.

## Rollback plan

Unset `GCOR_API_KEY` everywhere it was added; the middleware falls back to
open (with its startup warning). The `/api/search` filtering fix and the port
rebinding are independent of the key and are not part of this rollback.
