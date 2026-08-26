# ADR 0003: Tolerate malformed structured-output responses in Graphiti

**Status:** Accepted
**Date:** 2026-08-22
**Decision owners:** EedgeAI maintainers

## Context

While validating the OpenWebUI ingestion path end-to-end (see
[docs/adr/0002-proxy-api-authentication.md](docs/adr/0002-proxy-api-authentication.md)
for the auth work that prompted this pass), a real document ingest failed
with a 500 from Graphiti's `/messages` endpoint:

```
pydantic_core.ValidationError: 3 validation errors for EdgeDuplicate
duplicate_facts     Field required
contradicted_facts  Field required
fact_type           Field required
```

`graphiti-core==0.22.0`'s edge-resolution step
(`edge_operations.resolve_extracted_edge`) asks the local LLM
(`qwen2.5:7b` via Ollama, per this repo's default) for a structured
`EdgeDuplicate` JSON object, then does `EdgeDuplicate(**llm_response)`
directly — all three fields required, no defaults. The local model's
response omitted all three keys, crashing edge resolution and, with it, the
entire episode ingest, instead of degrading gracefully. This is exactly the
risk [ADR 0001](0001-graphiti-falkordb-backend.md) already flagged
("Structured extraction may fail on small models"), now reproduced against a
real document.

A first, narrower patch covering only `EdgeDuplicate` was not sufficient:
retrying the same document after that fix hit a *different* model in an
earlier pipeline stage —

```
pydantic_core.ValidationError: 1 validation error for ExtractedEntities
extracted_entities   Field required
  input_value={'$defs': {'ExtractedEnti...title': 'ExtractedEntities'}, ...}
```

— where the LLM had echoed back the JSON **schema** definition itself
(`$defs`, `title` keys) rather than actual extracted-entity data. This
confirmed the failure mode is systemic, not specific to one prompt: any of
graphiti-core's internal "ask the LLM for structured JSON, then
`Model(**llm_response)`" call sites can hit the same class of problem.

## Decision drivers

- Fix the actual pattern, not just the first instance of it — a
  model-by-model reactive patch as each one surfaces in production is not
  an acceptable fix for a systemic issue.
- Don't fork or vendor `graphiti-core`.
- Match each field's own documented fallback intent where one is stated,
  rather than an arbitrary workaround.
- Follow this repo's existing pattern for patching a third-party
  dependency's behavior at runtime instead of editing installed
  site-packages directly (see `openclaw/hotfix-openclaw-runtime.js`).

## Decision

Enumerated every Pydantic model graphiti-core 0.22.0 uses as a
`response_model=` for LLM structured output (`grep -rn "response_model="`
across the installed package): `EdgeDuplicate`, `ExtractedEntities`,
`EntitySummary`, `NodeResolutions`, `Summary`, `SummaryDescription`,
`InvalidatedEdges`, `ExtractedEdges`, `MissingFacts`. (`EdgeDates`'s fields
are already optional, so it needs no patch.  Custom user-defined entity/edge
types — `response_model=entity_type` / `edge_model` — are runtime-supplied
classes, not statically enumerable, and aren't covered; this repo doesn't
configure custom entity/edge types by default.)

In `graphiti/app.py`, at process startup, patch each of those nine classes'
`__init__`: for any required field missing from the LLM's response, fill in
a default inferred from the field's type annotation (`list[...] → []`,
`str → ""`, `int → 0`, `float → 0.0`, `bool → False`), with a small override
table for fields whose own docstring names a more specific fallback
(`EdgeDuplicate.fact_type → "DEFAULT"`, per its description: *"One of the
provided fact types or DEFAULT"*). A warning is logged naming the model and
the missing field(s) whenever this triggers, so it stays visible rather than
silently swallowed. The original (compiled, unmodified) Pydantic validator
still runs on the completed data — well-formed responses are unaffected.

Verified directly against the installed `graphiti-core==0.22.0` package
before merging: all nine models construct without error given completely
empty input (the worst case actually observed — a full schema echo), and a
normal well-formed `EdgeDuplicate` response round-trips unchanged.

## Consequences

Positive: a document no longer fails to ingest outright because one
internal LLM call in a multi-step pipeline returned incomplete or malformed
JSON — the episode still gets saved, just without that one step's
contribution (no dedup, no summary, etc., for that specific call). The
warning log makes each occurrence visible; a rising rate of these warnings
is a real signal that the configured extraction model is unreliable and
worth upgrading — see the production local-model sizing discussion
(DeepSeek, etc.) already tracked separately in
[[project_production_local_llm_goal]].

Negative: this is a monkeypatch against a pinned third-party version
(`graphiti-core==0.22.0`, digest-pinned base image per
`graphiti/Dockerfile`). If graphiti-core is ever upgraded: re-verify the
patch still applies (class/import paths could move), re-run the
`response_model=` enumeration in case new response models were added, and
check whether upstream has fixed the underlying non-fault-tolerant
construction directly — if so, remove this patch rather than carry it
forward unnecessarily. Custom entity/edge types, if ever configured, would
need their own explicit addition to the patched-classes list.

## Rollback plan

Delete the patch block in `graphiti/app.py` (self-contained, directly under
the `graphiti_core` imports) and rebuild the `graphiti` image. The
underlying crashes return; nothing else in this ADR depends on the patch
being present.

## 2026-08-24 addendum: nested list[BaseModel] fields weren't actually covered

Observed directly, not anticipated: `ExtractedEdges` was already in the
patched-model list above, but a real ingest still crashed with
`ValidationError: edges.0.source_entity_id — Input should be a valid
integer [input_value=None]`. Root cause: patching a model's own
`__init__` only intercepts an *explicit* `Model(**data)` call in Python —
it does not intercept nested `list[SomeModel]` fields, which
pydantic-core validates straight from the raw dicts via its compiled
schema, never calling the nested model's `__init__`. So `ExtractedEdges`
being patched never helped its own `edges: list[Edge]` field when an item
had an explicit `null` for a required int.

Fixed by making `_lenient_init` sanitize nested `list[BaseModel]` fields
recursively *before* handing data to the real pydantic `__init__` — see
`_sanitize_for_model` in `graphiti/app.py`. This also closes the same
latent gap in `NodeResolutions.entity_resolutions: list[NodeDuplicate]`
and `ExtractedEntities.extracted_entities: list[ExtractedEntity]`, which
carry the same risk but hadn't been observed crashing yet. Re-verified all
9 patched models with fully-empty input and a normal well-formed
round-trip after this change — no regressions.
