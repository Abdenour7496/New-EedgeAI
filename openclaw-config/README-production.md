# Production OpenClaw config

`openclaw.production.json` mirrors `openclaw.json` (dev) but inverts the model
order: **open-source local model first, frontier cloud models as fallback**
only when the local model fails, times out, or is otherwise unavailable.

This is the opposite of `openclaw.json`, which is deliberately cloud-first
(`primary: openai/gpt-5.6-sol`) because the dev box's hardware struggles to
run local models reliably. Do not merge these two files — they encode
different, hardware-driven trade-offs for different environments.

## Before deploying this

- `deepseek-r1:32b` is a **placeholder size**. Right-size it to the actual
  production GPU allocation once cluster specs are final — swap for a
  smaller distill (7B/8B/14B) if VRAM is tighter than expected, or a larger
  one (70B+) if there's headroom. Update both the `id`/`name` fields in
  `models.providers.ollama.models` and `agents.defaults.model.primary`.
- Pull the chosen tag into the cluster's Ollama deployment
  (`ollama pull deepseek-r1:<size>`) before cutover — first-pull is
  several GB and shouldn't happen on a cold path in prod.
- Validate structured JSON reliability for the *Graphiti extraction* model
  separately — this file only controls OpenClaw's chat/agent model, not
  `GRAPHITI_MODEL` (set via `.env`). Both should be validated the same way
  the ADR called out for `qwen2.5:7b` (see
  `docs/adr/0001-graphiti-falkordb-backend.md`) before trusting either in
  production.
- This file isn't wired into `docker-compose.unified.yml` or any Helm chart
  yet — the existing `aks-helm-chart.zip` predates the current
  Graphiti/FalkorDB architecture and needs a rebuild, not a patch. Mount
  this file (e.g. via a ConfigMap) as `openclaw.json` inside the production
  OpenClaw container/pod once that chart exists.
