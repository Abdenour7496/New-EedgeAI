# ADR 0007: Prometheus alert rules (no Alertmanager yet)

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

`prometheus` already scrapes `proxy`, `graphiti`, `falkordb-exporter`, and
`openclaw`, and `grafana` renders dashboards from that data — but nothing
evaluated the data for a problem state. A dead service or a failing
extraction pipeline was only noticed by someone looking at a dashboard.

## Decision

Add `monitoring/alerts.yml`, loaded via `rule_files` in
`monitoring/prometheus.yml`, with rules built directly against metrics this
stack already exports (no new instrumentation):

| Alert | Fires on | Source |
|---|---|---|
| `ServiceDown` | `up == 0` for 2m, any scrape job | Prometheus's own scrape state |
| `FalkorDBDown` | `redis_up == 0` for 2m | `falkordb-exporter` |
| `ProxyLLMErrorRateHigh` | >20% of `gcor_llm_requests_total` are `error`/`exception` over 10m | `proxy/main.py` |
| `ProxyIngestFailing` | `gcor_ingest_total{status="graphiti_error"}` incrementing for 10m+ | `proxy/main.py` |
| `GraphitiIngestFailing` | `graphiti_ingest_requests_total{status="error"}` incrementing for 10m+ | `graphiti/app.py` |
| `GraphitiSearchFailing` | `graphiti_search_requests_total{status="error"}` incrementing for 10m+ | `graphiti/app.py` |

This is **rules only** — no Alertmanager service, no notification channel.
Firing alerts are visible at `http://localhost:9090/alerts` and queryable
in Grafana via the `ALERTS`/`ALERTS_FOR_STATE` metrics Prometheus already
exposes, but nothing pages or messages anyone. This was a deliberate scope
cut: wiring a real notification channel (e.g. Alertmanager -> Telegram,
reusing the bot token already configured for OpenClaw) is meaningfully
more moving parts — a new service, routing config, silence/inhibition
rules — and was explicitly deferred rather than done partially.

`openclaw`'s own `/metrics` (port 9091) has no rules of its own here beyond
`ServiceDown` — its exported metric names weren't inventoried as part of
this pass; extending coverage there is future work.

## Consequences

- An operator has to actively check Prometheus/Grafana to see a firing
  alert; there's no push notification yet. Adding Alertmanager (with a
  Telegram or other receiver) is a natural next step and was scoped out of
  this ADR intentionally, not forgotten.
- All rule expressions were built against metric names and label values
  read directly from `proxy/main.py` and `graphiti/app.py` at the time of
  writing (not guessed) — if those metrics are renamed or their label
  values change, `monitoring/alerts.yml` needs a matching update or the
  rules will silently stop firing.
- `BackupStale` / "no backup in N hours" was considered and deliberately
  left out: the `backup` service (see `backup/README.md`) doesn't export
  Prometheus metrics today, so there's nothing to alert on without adding
  instrumentation there first.
