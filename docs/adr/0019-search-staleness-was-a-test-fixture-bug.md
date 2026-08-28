# ADR 0019: The "search staleness" bug was never in Graphiti/FalkorDB — it was a temporal-filter mismatch

**Status:** Accepted — test-level root cause found and fixed; a related
production-code question is left open for a decision (see Consequences)
**Date:** 2026-08-28
**Decision owners:** EedgeAI maintainers
**Corrects:** docs/adr/0016, docs/adr/0017, docs/adr/0018 (all superseded
by this — see "What this means for those ADRs" below)

## Context

ADR 0018 shipped passive instrumentation to catch the next live occurrence
of the search-staleness bug with full context instead of forcing a
synthetic reproduction. It worked immediately — but not the way expected.

Re-running the live integration suite reproduced
`test_ingest_then_search_finds_it`'s failure three times in a row this
session. The new diagnostics (both the connection-level and
search-ranking checks from ADR 0018) never fired for any of the three —
the data was never actually missing or unreachable at any layer they
checked. That mismatch was the clue: whatever was happening, it wasn't in
Graphiti, FalkorDB, or graphiti-core's search/ranking path at all.

### The actual mechanism

The failing test's original fixture text was:

> "The quarterly revenue for Northwind Traders in **Q4 2026** was 7.8
> million dollars."

Every session this bug was investigated in, the stack's real wall-clock
date was in **August 2026** — meaning "Q4 2026" (October–December) was, at
the time of every single test run, a date in the future.

Graphiti's temporal extraction reads a quarter-scoped statement as
defining a validity *window* for the fact: `valid_at` = quarter start,
`invalid_at` = quarter end. For "Q4 2026" that's
`valid_at=2026-10-01, invalid_at=2027-01-01` — both still in the future
relative to August 2026. Separately, `proxy/main.py`'s `_filter_hits()`
applies temporal-validity checks against `now`:

```python
valid_to = p.get("valid_to")          # = fact["invalid_at"] or fact["expired_at"]
if valid_to and valid_to < now:
    continue                          # "already expired"
valid_from = p.get("valid_from")      # = fact["valid_at"] or fact["created_at"]
if valid_from and valid_from > now:
    continue                          # "not yet valid"
```

With `valid_at` in the future, the fact is dropped as "not yet valid" —
every time, deterministically, with zero timing dependency.

**First fix attempt, and why it wasn't enough:** changed the fixture to a
firmly *past* quarter ("Q2 2024"), expecting that to clear the "not yet
valid" check. It did — but the test still failed, reproducibly, for the
opposite reason. Graphiti extracted `valid_at=2024-04-01,
invalid_at=2024-07-01` — both now in the *past* relative to August 2026 —
so `_filter_hits()` instead dropped it as "already expired"
(`invalid_at < now`). **There is no calendar quarter that survives both
checks**, except one that happens to contain the actual current date — not
something a stable test fixture can rely on. Confirmed directly by
capturing Graphiti's raw `/search` response (bypassing the proxy) for
both fixture variants: it correctly found and top-ranked the fact in
*both* cases (future- and past-dated), with the proxy's `/api/search`
silently omitting it in both cases too. The data was never missing or
stale at any point — only ever filtered downstream.

### Root cause, precisely

`_filter_hits()`'s temporal-validity logic predates the Graphiti/FalkorDB
backend — it's a holdover from the earlier Neo4j-based design (visible in
git history alongside the now-removed `_EXPAND_CYPHER` structural-phase
queries, from before docs/adr/0001's cutover), carried forward onto
Graphiti's `valid_at`/`invalid_at` fields without re-validating that the
semantics still line up. They don't, for one specific and common case:

- Graphiti's bi-temporal edges use `valid_at`/`invalid_at` for two
  distinct things under one field pair: (a) genuine **supersession** —
  when a newer fact contradicts an older one (e.g. "the CEO is X" replaced
  by "the CEO is Y"), the old edge is correctly marked invalid and safely
  hidden; and (b) a **stated validity window extracted from the text
  itself** (e.g. "valid until Dec 10", or — the case here — "Q2 2024
  revenue") — which for a historical/reporting statement does *not* mean
  the fact becomes false or unworthy of citing once the window closes; it
  means the statement is permanently true *about* that window.
- `_filter_hits()` treats both cases identically: `invalid_at < now` ⇒
  drop. For case (a) that's correct. For case (b) — any document
  mentioning a specific past period — it means **the fact becomes
  permanently unsearchable the moment that period ends**, which is wrong:
  a document mentioning a specific quarter's revenue, once that quarter
  has passed (which is true almost immediately for most real content),
  would be silently hidden from search forever.

## Decision

1. **Fixed the test.** `test_ingest_then_search_finds_it`'s fixture no
   longer names a specific period at all ("Northwind Traders reported
   quarterly revenue of 7.8 million dollars.") — Graphiti extracts no
   valid_at/invalid_at window, so the fact stays permanently visible.
   Verified: full suite passes cleanly against this fixture (see commit).
   This sidesteps the issue for the test; it does not resolve the
   underlying mismatch for real documents (see Consequences).
2. **Left ADR 0017's scheduled self-restart mitigation in place,
   unchanged.** Cheap and harmless; while it turns out not to have been
   fixing *this* bug, a genuine connection/process-level staleness issue
   remains a plausible failure mode in general, with no evidence either
   way and no cost to keeping a bounded-damage mitigation.
3. **Left ADR 0018's diagnostic instrumentation in place, unchanged.**
   Same reasoning — a real, low-cost, well-scoped check for a different
   failure mode this investigation never found evidence of, but which
   remains worth watching for.
4. **Did not change `_filter_hits()`'s production filtering logic in this
   pass.** This affects real search behavior for every document that
   mentions a specific past date/period — a meaningfully large surface —
   and the right fix depends on a product decision (see Consequences),
   not something to change unilaterally mid-investigation.

## What this means for those ADRs

- **ADR 0016** (FalkorDB connection health-checking): the fix itself
  (health_check_interval, socket timeouts) is harmless and stays, but its
  premise — that a stale FalkorDB connection was ever actually observed
  causing this — is now unconfirmed.
- **ADR 0017** (search-connection-workaround): its "what was ruled out"
  section is still accurate, careful work (embedding storage, cosine
  scoring, raw Cypher queries — all genuinely tested and confirmed fine),
  but its central conclusion — "something concurrency- or process-level in
  the async FalkorDB/Redis client" — was never actually demonstrated;
  every documented failure of the specific test that drove that
  conclusion is now fully explained by this ADR's mechanism instead. The
  "separate OS process always succeeds" observations were real, but very
  plausibly explained by those ad-hoc checks calling `graphiti-core`'s
  `search()` directly rather than going through the proxy's
  `_filter_hits()` — bypassing the filter, not bypassing staleness.
- **ADR 0018** (passive instrumentation): correctly identified something
  was off (neither of its checks fired despite a real, reproducing
  failure) — that mismatch is exactly what led to finding this ADR's
  actual root cause. Its own mid-flight correction (a recency-gated false
  positive it caught on live traffic) stands as documented.

None of those ADRs are being deleted or marked Rejected — the
investigative work in them was real, careful, and honestly documented at
each step; this ADR is the conclusion that work was building toward.

## Consequences

- `test_ingest_then_search_finds_it` now passes reliably, every run, with
  no dependency on Graphiti restart timing or which calendar date the
  fixture happens to name.
- **Open product question, not resolved here:** does `_filter_hits()`
  correctly handle a real document that names a specific past
  date/period? Right now, no — that content becomes unsearchable once the
  named period ends (often immediately, for most real-world content).
  Options worth considering in a future pass, not decided here:
  - Stop using Graphiti's `invalid_at` as an automatic "hide it" signal
    for `_filter_hits()`'s temporal check, since it conflates genuine
    supersession with a stated-but-still-true historical window; possibly
    rely on some other signal for supersession specifically (needs
    investigation into what graphiti-core actually exposes to
    distinguish the two cases, if anything, at the API level).
  - Drop the temporal-validity check entirely for Graphiti-sourced hits
    and rely on Graphiti's own dedup/contradiction handling at ingest
    time instead (it already creates/updates edges to reflect the latest
    known state; the open question is only whether *superseded* old
    facts should also remain searchable as history, which is a genuine
    product judgment call).
  - Something narrower: only treat `invalid_at` as "hide it" when it's
    very recent relative to the edge's own `created_at` (suggesting
    active supersession discovered at ingest time) rather than when it's
    old relative to wall-clock `now` (which just means the stated period
    has naturally elapsed).
  This needs a decision from whoever owns the product behavior here — it
  changes what real users can find, not just test reliability.
