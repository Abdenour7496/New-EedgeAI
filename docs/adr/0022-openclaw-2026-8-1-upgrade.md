# ADR 0022: Upgrade openclaw base image 2026.7.1-2 → 2026.8.1

**Status:** Accepted — upgrade complete; the intermittent model-routing
issue flagged when this was first written is now root-caused and fixed
(see "Update: the intermittent routing issue, root-caused" at the end)
**Date:** 2026-08-31/09-01
**Decision owners:** EedgeAI maintainers

## Context

Asked to update openclaw to the latest release. Was `2026.7.1-2`
(unpinned by digest); latest available was `2026.8.1`, roughly a month
of releases ahead, with several breaking changes documented upstream:
OpenProse plugin removal, a `codex/*` → `openai/*` model-reference
migration, and a plugin SDK import-path deprecation. Checked our config
against each before touching anything: already on `openai/*` format, no
OpenProse/`/prose` usage anywhere in the workspace — both non-issues.

## Decision

Pinned `openclaw/Dockerfile`'s base image to
`ghcr.io/openclaw/openclaw:2026.8.1@sha256:e7849cb6c1ef1ead39ab4be7d85edb2df89611f486e283284c7cf35ce39a20d4`
(digest verified directly via `docker pull`, not trusted from a fetched
summary). Extends ADR 0014's pinning practice to this Dockerfile's base
image, which wasn't originally in that ADR's scope.

### hotfix-openclaw-runtime.js needed a real update, not just a rebuild

The build's own hotfix-application step is defensively written (checks a
regex match before writing, warns rather than crashes on a miss) — this
mattered: rebuilding against 2026.8.1 revealed the fallback-message-guard
patch's three known historical signatures all missed. Investigated
directly rather than assuming irrelevance: the target function
(`resolveAssistantFailoverErrorMessage`) still exists, and the underlying
bug it fixes (a stale assistant error from a *different* provider/model
getting surfaced during fallback) is still present in 2026.8.1's version
of it — just with a changed signature (`params.timedOut`/`idleTimedOut`
replaced by `params.terminal.kind`/`source`, plus a new `providerOwner`
field). Added a fourth pattern variant for this signature; verified the
patched output byte-for-byte matches the intended fix.

Also fixed a real bug in the hotfix script itself, found while
investigating: it unconditionally wrote the file and logged "Patched
OpenClaw runtime bundle" regardless of whether any of the three
attempted patterns actually matched — meaning a fully-failed patch
(all three misses) still printed a misleading success line. Now only
writes/logs on an actual match, and warns explicitly (naming the file)
when none apply. This is what made the miss visible in the first place
once it was fixed, rather than continuing to silently mask it on future
version bumps.

### State migration, done properly rather than force-started

The new version refuses to start the gateway against un-migrated state
(by design — several of its startup checks explicitly require running
`openclaw doctor --fix` first, one requiring the gateway itself to be
*stopped* since the migration needs exclusive access to the state it's
rewriting). Iterated through this properly rather than forcing a start:

1. `openclaw doctor --fix` (run via `docker compose run --rm --no-deps
   openclaw ...`, reusing the same persisted volumes without competing
   with the actual service) — migrated `openclaw.json`'s schema (legacy
   `agents.defaults.memorySearch` → `memory.search`;
   `mcp.servers.*.type` → `.transport`; legacy `agents.defaults.models`
   copied forward into the new `modelPolicy.allow`), upgraded the agent
   SQLite schema v1→v19, and migrated several other legacy JSON stores
   (auth profiles, device identity, Telegram pairing/allowlist state)
   into shared SQLite. All of our custom config (primary/fallback
   models, Telegram, the `graphiti-memory` MCP server) survived intact —
   confirmed by reading the resulting `openclaw.json` directly, not
   assumed.
2. A second blocker (`Legacy session store requires migration`) needed
   a specific mode `doctor` itself doesn't run by default:
   `openclaw doctor --session-sqlite dry-run --session-sqlite-all-agents`
   first (515 legacy entries, 0 issues), then the same with `import`
   instead of `dry-run` (515→515 entries migrated cleanly, ~1MB
   reclaimed via compaction).
3. A third blocker (`Legacy workspace setup state requires migration`)
   needed a further `doctor --fix` pass — this one also merged
   `TOOLS.md` into `AGENTS.md` (a 2026.8.1 workspace-file consolidation)
   and migrated `HEARTBEAT.md` into cron scratch state. Verified the
   merge preserved the corrections made in the same session (removed
   stale Neo4j/Qdrant references, documented `ingest-cli`) — see
   Consequences.
4. Codex's OAuth session specifically needed its own explicit migration:
   `agent-scoped Codex runs use OpenClaw's auth store and do not read
   [the raw ~/.codex/auth.json] file` — ran the tool's own suggested
   `openclaw migrate plan codex --from <codex-home> --agent main
   --include-secrets --item auth:openai` (preview, 0 conflicts), then
   the equivalent `migrate apply ... --yes` (imported cleanly, auto-
   archived `config.toml` for manual review rather than activating it
   automatically).
5. Ollama's provider API key, previously read directly from
   `openclaw.json`'s `models.providers.ollama.apiKey`, also needed
   moving into the new auth store — `openclaw models auth
   paste-api-key --provider ollama` (piped via stdin; the flag name
   suggested but doesn't exist: `--api-key`).
6. The several unclean boots this took along the way tripped 2026.8.1's
   new crash-loop breaker, which suppresses channel auto-start
   (Telegram) after repeated failures within a rolling window. Not a
   real error — cleared on its own once a clean boot cycle completed
   without a new failure; started Telegram manually in the meantime via
   `openclaw gateway call channels.start --params
   '{"channel":"telegram"}'` rather than waiting.

A separate, minor, genuinely unrelated bug was found and worked around
while cleaning up test data afterward, not caused by this upgrade:
`DELETE /episode/{uuid}` 500s with `GroupsEdgesNotFoundError` when the
episode's group has zero extracted edges (`EntityEdge.get_by_group_ids`
raises instead of returning empty) — `delete_episode()` in
`graphiti/app.py` doesn't catch it. Worked around by deleting the one
affected test episode directly via Cypher; the underlying handler bug is
still open, worth a small follow-up fix (catch
`GroupsEdgesNotFoundError` and treat as "0 edges" rather than propagating
as a 500).

## Consequences

- Verified working: gateway starts cleanly and reports ready, Telegram
  channel operational, `openclaw doctor --fix` reports no more blocking
  migrations, a direct chat-completion request round-tripped in 10s
  using the correct subscription-based Codex transport
  (`chatgpt.com/backend-api/codex/responses`, not metered billing), and
  `ingest-cli` (with docs/adr/0021's idempotency check) still functions
  correctly end-to-end when the completions path is healthy.
- Corrected the `TOOLS.md` (now merged into `AGENTS.md`)/`IDENTITY.md`/
  `USER.md` staleness fixed earlier the same session — confirmed those
  corrections survived 2026.8.1's own TOOLS.md→AGENTS.md merge migration
  rather than being silently lost.
- **Open issue, not resolved:** model routing for the primary
  `openai/gpt-5.6-sol` model is intermittent — some requests correctly
  use the Codex CLI subscription transport (fast, ~10s, confirmed
  working); others instead route through an "embedded" execution path
  that hits the already-known-out-of-credit metered OpenAI API key
  (same account confirmed unfunded well before this upgrade — not a new
  billing problem, but a *newly inconsistent* routing decision that
  didn't reach that failing path before). When it does, the full
  fallback chain (Codex → Claude-CLI → Ollama) is also unreliable right
  after this each fresh boot: Claude-CLI aborted near-instantly on
  several attempts, and Ollama's own auth needed the explicit
  `paste-api-key` step above. Observed this failure specifically on a
  cron-triggered background task and once on a `ingest-cli` extraction
  call; direct interactive-style API calls have succeeded every time
  tested. Not yet root-caused which trigger path or timing condition
  picks "embedded" over the configured `agentRuntime: codex` CLI
  backend — worth a focused follow-up if it recurs visibly (e.g. slow or
  silently-stuck ingests), rather than something to chase further
  blind in this pass.
- `delete_episode()`'s `GroupsEdgesNotFoundError` handling gap noted
  above is real but out of scope for this ADR — tracked here for a
  future small fix, not fixed in this pass.

## Update: the intermittent routing issue, root-caused

Asked directly to investigate and fix the open routing issue rather than
leave it as a documented gap. Root cause: `openclaw models auth list`
showed `openai:codex-import (Codex import) [openai/api_key]` — the
`migrate apply codex` step from the original upgrade (above) had imported
the Codex credential and classified it as a plain **API key**, not an
OAuth/session credential. `openai/gpt-5.6-sol` is tagged `alias:codex` in
the model catalog specifically so it routes through the external `codex`
CLI process (which has its own, completely valid, untouched OAuth session
at `~/.codex/auth.json` — confirmed directly: `auth_mode: "chatgpt"`,
`OPENAI_API_KEY: null`). Instead, every observed failure was via
`[agent/embedded]` execution, never `[agent/cli-backend]` — meaning the
badly-typed imported profile gave the *embedded* path just enough
credential to attempt a request at all, and it used it as a metered API
key, hitting the same already-known-out-of-credit OpenAI account. Before
the import, embedded mode had no credential and (per the original error
this session fixed) errored outright — this import went one step too far
and gave it a *wrong* one instead of the intended right one.

Fix: `openclaw models auth logout openai:codex-import --yes`, then
restart the gateway. No re-import needed — removing the bad profile is
sufficient; the correct path (the external `codex` CLI's own OAuth
session) was never broken and picks up automatically once the bad
embedded-mode credential is gone.

Verified: 5 consecutive direct chat completions, all clean, all fast
(7-12s), zero fallback/error log lines across any of them — a real
change from before the fix, where every failing attempt fell through
the entire broken chain (Codex "no credits" → Claude-CLI instant abort →
Ollama auth error) rather than completing on the first try.

### A second bug found while verifying the fix

Testing the full `ingest-cli` pipeline (not just direct chat) with the
auth fix live surfaced a *different*, real bug: two consecutive
`ingest-cli` runs both failed with a generic `{"error":"fetch failed"}`
at almost exactly 300 seconds, with nothing landing in Graphiti either
time. Root cause: Node's global `fetch()` has no effective timeout
override via a plain `signal: AbortSignal.timeout(...)` option — that
only bounds how long the calling code waits, not undici's own
dispatcher-level `headersTimeout`/`bodyTimeout`, which default to 300s
and close the connection independently of any `AbortSignal`. Confirmed
by setting `AbortSignal.timeout(1800000)` first — no change, same ~300s
failure. Graphiti's own extraction genuinely needs longer than that under
real load (confirmed via its `graphiti_hotfix` "LLM response...
defaulting" log appearing mid-request, with no completion log — the
request was still legitimately in progress server-side when the client's
socket died).

Fixed properly: added `undici` as an explicit dependency
(`openclaw/package.json`) and gave `ingest.js`'s Graphiti call its own
dedicated `undici.Agent` (not the global default, so only this call gets
the extended patience) with `headersTimeout`/`bodyTimeout` both set to
`GRAPHITI_INGEST_TIMEOUT_SECONDS` (env var, default 1800s — same name
proxy/main.py already uses for the equivalent `/api/ingest` timeout).
The `AbortSignal.timeout()` stays too, as an outer backstop. Verified: a
document that previously failed twice at ~300s now completes
successfully — took 26 minutes end-to-end under current load, confirming
the *connection* was never the problem, only the client's patience for
it. That 26-minute figure is itself a separate, real throughput
characteristic (Graphiti's multi-step extraction serialized behind the
shared openclaw concurrency lane, docs/adr/0012) worth keeping an eye on
if it recurs for real documents, but is not a bug introduced by this fix
or this upgrade — this ADR's fixes make that duration *survivable*
rather than *shorter*.

### A third bug, found and fixed along the way (unrelated to routing)

Hit the pre-existing `delete_episode()`/`gc_orphaned_edges()`/
`delete_group()` bug (flagged but not fixed earlier in this ADR) for
real, repeatedly, while cleaning up test episodes during this
investigation — an episode/group with zero extracted edges 500s instead
of treating "no edges" as "nothing to do"
(`EntityEdge.get_by_group_ids()` raises `GroupsEdgesNotFoundError` rather
than returning `[]`). Fixed this pass since it kept costing real time:
added `_get_edges_by_group_ids_safe()` in `graphiti/app.py`, catching
`GroupsEdgesNotFoundError` and returning `[]`, used at all three call
sites. Verified directly: `POST /admin/gc-orphaned-edges/{group}` against
a group with zero edges previously 500'd; now returns
`{"success":true,"edges_scanned":0,...}` cleanly.

### Verified after all three fixes

Full stack healthy (all 16 services), `openclaw`'s own `ingest.test.js`
suite still passes (26/26, unaffected by the fetch/dispatcher change —
those are pure-logic tests with no network calls), and the properly
rebuilt image (via the normal Dockerfile flow, not the live
`npm install` used to verify quickly) resolves `undici` correctly and
reproduces every fix cleanly from a fresh container.
