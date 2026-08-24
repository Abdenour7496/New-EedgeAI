# ADR 0004: Data-governance schema for ingested knowledge

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

Every document chunk and chat-session transcript ingested into Graphiti
already carried a classification — `access_level`, `confidence`,
`valid_from`/`valid_to`, `agent_id` — but it existed only as informal string
literals scattered through `proxy/main.py`: `"public"` typed in five
different places, `_filter_hits()`'s enforcement logic documented only in a
docstring, and `access_level` accepted from a caller (`api_ingest`,
`api_ingest_session`) as an unvalidated free-text `str`. Nothing stopped a
typo (`"pubic"`, `"Public"`, `"restrcited"`) from silently creating an
access level that `_filter_hits()` would never recognize as restricted —
because anything that isn't exactly `"restricted"` or `"agent:<id>"` falls
through as effectively public.

This had already caused one real bug (ADR 0002): `/api/search` skipped
`_filter_hits()` entirely, so `access_level="restricted"` hits were hidden
from chat but fully readable via search. Fixing that call site closed one
leak; it didn't stop the next feature from doing the same thing, because
there was no single place documenting what the fields mean or how they're
supposed to be enforced.

## Decision

Formalize the existing fields as a schema, in `proxy/governance.py`, rather
than inventing new classification machinery:

| Field | Type | Values | Set at | Enforced at |
|---|---|---|---|---|
| `access_level` | str | `"public"` \| `"restricted"` \| `"agent:<agent_id>"` | ingestion (`api_ingest`, `api_ingest_session`) | retrieval (`_filter_hits`) |
| `confidence` | float 0.0–1.0 | proxy-computed, currently always `1.0` | `graphiti_search` | retrieval, against `CONFIDENCE_THRESHOLD` |
| `valid_from` / `valid_to` | ISO-8601 UTC str \| null | proxy-computed from `valid_hours` or Graphiti's own temporal extraction | ingestion / Graphiti | retrieval (`_filter_hits`) |
| `agent_id` | str | free text, provenance only | ingestion | not itself an access boundary — see below |

`governance.validate_access_level()` is the single source of truth for what
counts as a valid `access_level`: exactly `"public"`, exactly
`"restricted"`, or `"agent:"` followed by 1–128 characters. Both ingestion
endpoints call it and reject an invalid value with `400` before anything
reaches Graphiti, instead of silently accepting a typo that `_filter_hits()`
would then treat as public. `confidence` and the temporal fields stay
proxy-computed (never taken directly from a caller), so they don't need the
same input validation a free-text field does.

## Known enforcement gap (documented, not fixed by this ADR)

`_filter_hits()`'s `"agent:<id>"` branch only excludes a hit when this
proxy's own `AGENT_ID` is set and doesn't match:

```python
if access_level.startswith("agent:") and AGENT_ID:
    owner = access_level.split(":", 1)[1]
    if owner != AGENT_ID:
        continue  # excluded
```

If `AGENT_ID` is unset on a given proxy instance (the default), agent-scoped
hits are **not excluded** — they pass through to every caller of that
instance, same as `"public"`. This is a real access-control gap on any
deployment that ingests `agent:<id>`-scoped content but doesn't run a
dedicated per-agent proxy instance to enforce the partition. It is being
documented here rather than silently changed, because the fix (deny by
default when `AGENT_ID` is unset, rather than allow) is a behavior change
that needs its own sign-off — a deployment currently relying on the
allow-when-unset behavior would break. Track any change to this default
separately from this ADR.

## What this does not change

- No new storage, no schema migration — these fields already lived in every
  Graphiti episode's metadata string and in `_filter_hits()`'s payload
  dict. This ADR documents and validates them; it doesn't restructure them.
- `_filter_hits()`'s enforcement order (confidence → temporal → access) is
  unchanged, aside from hardening the `access_level` default to treat an
  explicit `None` the same as a missing key (`p.get("access_level") or
  "public"` instead of `p.get("access_level", "public")`, which only applies
  a default when the key is absent, not when its value is `None`).
