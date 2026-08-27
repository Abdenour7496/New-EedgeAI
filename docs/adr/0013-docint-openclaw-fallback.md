# ADR 0013: DocInt routed through the proxy's own openclaw passthrough, with an explicit vision fallback

**Status:** Accepted
**Date:** 2026-08-27
**Decision owners:** EedgeAI maintainers

## Context

Assessing production readiness surfaced a real gap: `document_intel.py`'s
`_chat()` and `_vision()` (table/form vision extraction, classification,
entity extraction) called openclaw directly
(`http://openclaw:18799/v1/chat/completions`) — unlike the main chat
pipeline (`proxy/main.py`'s `call_openclaw()`) and, since ADR 0010,
Graphiti's own extraction, neither of which had this gap anymore. An
openclaw outage would leave DocInt's enrichment silently degraded (caught
by the existing per-step `except Exception` handling, logged as a warning,
defaulted to empty) rather than falling back to a working local model the
way the rest of the stack now does.

Text and vision needed different treatment. Text is straightforward:
`call_openclaw()`'s existing fallback already uses `OLLAMA_MODEL`
(`llama3.2`), a text model — fine for `_chat()`. Vision is not: that same
fallback would send multimodal `image_url` content to `llama3.2`, a
text-only model, silently mishandling the image rather than actually
describing it.

## Decision

- `_chat()` now calls this proxy's own `POST /v1/openclaw/chat/completions`
  (added in ADR 0012's addendum, originally for Graphiti) instead of
  openclaw directly — a self-referential `localhost:5001` call, since
  `document_intel.py` runs inside the same container/process as the proxy.
  Inherits `call_openclaw()`'s existing Ollama fallback unchanged; correct
  for text.
- `_vision()` also calls the same passthrough first, but wraps it in its
  own `try/except`: on any failure, falls back explicitly to a new
  `_vision_via_ollama()` helper using `OLLAMA_VISION_MODEL` (`llava:7b`) —
  not relying on `call_openclaw()`'s own (text-only) fallback for this
  case. `_vision_via_ollama()` is shared with the existing
  `VISION_BACKEND=ollama` branch, which already called the correct model
  (fixed in an earlier pass — see git history for that specific line).
- Authentication changed accordingly: `GCOR_API_KEY` (this proxy's own
  shared secret, required on `/v1/*` by its auth middleware) instead of
  `OPENCLAW_GATEWAY_TOKEN`, which `document_intel.py` no longer uses at
  all now that it never talks to openclaw directly.

Verified directly, not just by inspection:
- Happy path: `_chat()` via the new route returns a correct response.
- The genuinely new code: `_vision_via_ollama()` called directly against a
  real image returns a correct, sensible description via `llava:7b`.
- The shared `call_openclaw()` fallback-to-Ollama mechanism `_chat()` now
  depends on was already observed firing correctly in production earlier
  the same day (`OpenClaw provider chain exhausted; falling back to Ollama
  model llama3.2`, under real load) — not re-triggered artificially here,
  since doing so would have required disrupting the live `openclaw`
  container that real usage depends on.
- Full-stack regression check afterward: all containers healthy, main
  chat completions endpoint still returns 200.

## Consequences

- DocInt enrichment (tables, forms, classification, entities) now degrades
  to local models on an openclaw outage instead of silently going empty —
  lower quality than Codex/Claude Code, but functional, consistent with
  how the rest of the stack behaves under the same failure.
- One additional localhost hop per DocInt LLM/vision call (document_intel
  → proxy's own `/v1/openclaw` route → openclaw). Negligible latency
  (loopback), but worth knowing if ever debugging a request trace that
  looks like the proxy calling itself — that's intentional, not a bug.
- `OPENCLAW_BASE_URL` and `OPENCLAW_GATEWAY_TOKEN` are no longer read by
  `document_intel.py` at all (removed). If `PROXY_OPENCLAW_URL` or
  `GCOR_API_KEY` are ever misconfigured, DocInt's `_chat()`/`_vision()`
  openclaw branches fail closed to their respective fallbacks rather than
  reaching openclaw directly under any circumstance — there is no longer a
  direct code path to openclaw from `document_intel.py`.
