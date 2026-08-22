# EedgeAI

EedgeAI is a local-first agent and retrieval stack. Graphiti provides temporal knowledge extraction and hybrid retrieval, FalkorDB stores the graph and vectors, OpenClaw provides agent tools, Open WebUI provides chat, and MinIO preserves original documents and chat-session transcripts.

Conversational agents use OpenAI Codex first, Claude Code second, and local Ollama as the final fallback. Graphiti extraction and embeddings remain on Ollama by default because they are background OpenAI-compatible API workloads, separate from the interactive Codex and Claude Code runtimes.

## Current architecture

```text
Open WebUI / Knowledge UI / OpenClaw
                  |
             RAG proxy
                  |
              Graphiti
        (episodes, entities, facts)
                  |
              FalkorDB
          (graph + vectors)

Uploaded files and chat-session transcripts
                  |
                MinIO
      (S3-compatible durable storage)
```

Neo4j, Qdrant, and the Buzz collaboration stack are no longer active services. Their old Docker volumes are intentionally retained by name for rollback/export; the new services do not mount or modify them. See [docs/adr/0001-graphiti-falkordb-backend.md](docs/adr/0001-graphiti-falkordb-backend.md) for the migration rationale, [docs/adr/0002-proxy-api-authentication.md](docs/adr/0002-proxy-api-authentication.md) for the API-key/OpenWebUI-as-front-door decision, and [openwebui-functions/README.md](openwebui-functions/README.md) for the chat-capture Functions.

**OpenWebUI (`:8080`) is the only surface meant for end users.** Every other service — the Knowledge UI, Graphiti's REST docs, Grafana, Prometheus, the MinIO/FalkorDB browsers, OpenClaw's control UI — is an operator/admin tool, bound to `127.0.0.1` by default (reach it via an SSH tunnel or `kubectl port-forward`, not by opening the port). See [Authentication](#authentication) below.

## Feasibility and trade-offs

Graphiti officially supports FalkorDB and combines graph traversal, semantic similarity, keyword search, temporal validity, and episode provenance in one retrieval layer. This simplifies the former dual-write Qdrant/Neo4j path and removes cross-database consistency failures.

The material constraint is extraction quality: Graphiti requires an LLM that reliably returns structured JSON. The default local model is `qwen2.5:7b`; smaller local models were not reliable enough in validation. Allocate at least 12 GB RAM to Docker (16 GB recommended). For better extraction quality, point `GRAPHITI_API_BASE_URL`, `GRAPHITI_API_KEY`, and `GRAPHITI_MODEL` at an OpenAI-compatible hosted model.

CPU-only local extraction is also slow — several minutes per ingest call is normal, since each chunk is a sequential LLM call inside Graphiti. `/api/ingest`, `/api/ingest/session`, and the OpenWebUI ingest Functions all wait synchronously for that to finish; the proxy's `GRAPHITI_INGEST_TIMEOUT_SECONDS` (default 1800s) caps how long they'll wait before returning an error, even though the episode may still land a bit later. A hosted model needs far less margin.

A local model can also return incomplete or malformed structured JSON for one of Graphiti's internal extraction steps (missing required fields, or occasionally echoing the JSON schema itself instead of data), which graphiti-core doesn't tolerate by default. `graphiti/app.py` patches this at startup across every affected response model so a partial response degrades to safe defaults instead of failing the whole ingest — see [docs/adr/0003-graphiti-edgeduplicate-hotfix.md](docs/adr/0003-graphiti-edgeduplicate-hotfix.md). A rising rate of its warning logs (`docker compose logs graphiti | grep graphiti_hotfix`) is a real signal the configured model is unreliable and worth upgrading.

This cutover does not automatically transform legacy Neo4j/Qdrant data. Re-ingest source documents to populate Graphiti. Legacy volumes are retained until migration has been accepted. Graphiti groups also have no rename/archive concept — see [Operations](#operations).

## Start

```bash
cp .env.example .env
# At minimum, set GCOR_API_KEY and WEBUI_SECRET_KEY before starting anything
# beyond an isolated dev box — see Authentication below.
docker compose -f docker-compose.unified.yml up -d --build --remove-orphans
```

The first start downloads Qwen 2.5 7B, Nomic Embed Text, Llama 3.2, and LLaVA, so it can take several minutes.

To also capture files and chat sessions from Open WebUI (see [Chat and file capture](#chat-and-file-capture) below), install the three Functions in `openwebui-functions/` from Open WebUI's Admin Panel — they aren't wired in automatically. Their `gcor_api_key` valve (where they have one) defaults to this container's own `GCOR_API_KEY` env var, so there's normally nothing to configure there beyond installing them.

## Authentication

`GCOR_API_KEY` (set in `.env`) gates every `/api/*` and `/v1/*` route on the proxy — see [docs/adr/0002-proxy-api-authentication.md](docs/adr/0002-proxy-api-authentication.md). Every trusted caller needs the same value:

- `openwebui`'s `OPENAI_API_KEY`/`RAG_OPENAI_API_KEY` (already wired to `${GCOR_API_KEY}` in the compose file)
- `ingest-watcher` (already wired)
- each `gcor_*` Function's `gcor_api_key` valve (auto-fills from the OpenWebUI container's own `GCOR_API_KEY` env var)
- any direct `curl`/admin script (see the updated examples below)

Leaving `GCOR_API_KEY` blank leaves those endpoints open — the proxy logs a startup warning when that's the case. Generate a real one with `openssl rand -hex 32`.

OpenWebUI itself is the authenticated human front door: set `WEBUI_SECRET_KEY` (also in `.env`) to a fixed value so sessions survive a restart, and leave `OPENWEBUI_ENABLE_SIGNUP=false` once the first admin account exists.

## Services

Everything except Open WebUI is bound to `127.0.0.1` by default — these URLs work from the docker host itself; reach them remotely via an SSH tunnel or `kubectl port-forward` (see [Authentication](#authentication)).

| Service | URL / port | Purpose |
|---|---|---|
| Open WebUI | http://localhost:8080 | chat UI — the one surface meant for end users |
| Knowledge UI / proxy | http://localhost:5001/knowledge | ingest, browse, search, RAG API (operator tool) |
| Graphiti REST | http://localhost:8000/docs | knowledge ingestion and retrieval |
| FalkorDB Browser | http://localhost:3001 | graph inspection |
| FalkorDB | localhost:6379 | graph/vector database |
| MinIO API | http://localhost:9000 | S3-compatible object storage |
| MinIO console | http://localhost:9001 | browse documents and chat-session snapshots |
| Ollama | http://localhost:11434 | local LLM/embedding/vision backend |
| OpenClaw | http://localhost:18799 | agent gateway (Codex → Claude Code → Ollama) |
| Edge TTS | http://localhost:5050 | OpenAI-compatible text-to-speech |
| Prometheus | http://localhost:9090 | metrics |
| Grafana | http://localhost:3000 | dashboards (default login `admin` / `$GRAFANA_ADMIN_PASSWORD`) |

## Knowledge API

```bash
curl -H "Authorization: Bearer $GCOR_API_KEY" \
     -F "file=@report.pdf" -F "collection=documents" http://localhost:5001/api/ingest
curl -H "Authorization: Bearer $GCOR_API_KEY" \
     "http://localhost:5001/api/search?q=your+question&collection=documents"
```

The MinIO watcher also processes objects placed under `documents/inbox/` (it authenticates to the proxy with the same `GCOR_API_KEY`).

## Chat and file capture

Files attached during an Open WebUI chat, and the chat session itself, are captured automatically once the corresponding Function is installed from **Admin Panel > Functions** (see [openwebui-functions/README.md](openwebui-functions/README.md) for install steps):

| Function | Captures | Stored at | Indexed into |
|---|---|---|---|
| `gcor_file_ingest.py` | files attached to a chat message | `documents/originals/<doc_id>/<filename>` | Graphiti group `documents` (or the workspace's selected group) |
| `gcor_chat_session_ingest.py` | the conversation itself, after every assistant turn | `documents/chat-sessions/<session_id>/<date-time>_<meaningful-name>.json` | Graphiti group `chat_sessions` |

Chat-session naming: the folder is keyed by the chat's stable `session_id`, so a conversation's snapshots stay together for its whole life even as Open WebUI's own title changes. Each snapshot's filename carries the date, time, and the best human-readable name available at that moment — the real chat title once Open WebUI has generated one, otherwise a snippet of the first user message (Open WebUI's placeholder title, e.g. "New Chat", is never used as the name). Example: `chat-sessions/3f9a2b7c-.../2026-08-22T14-05-30Z_what-is-the-capital-of-france.json`.

Past conversations are searchable the same way documents are:

```bash
curl -H "Authorization: Bearer $GCOR_API_KEY" \
     "http://localhost:5001/api/search?collection=chat_sessions&q=your+question"
```

## Operations

```bash
docker compose -f docker-compose.unified.yml ps
docker compose -f docker-compose.unified.yml logs -f graphiti graphiti-mcp falkordb proxy
curl http://localhost:8000/healthcheck
docker exec eedgeai-falkordb-1 redis-cli PING
```

Graphiti groups replace Qdrant collections. A group is materialized on first ingest. Renaming groups is intentionally unsupported; deleting a group (or a single document within one) permanently removes its Graphiti episodes and facts — there is no archive/restore for either. The original file stays in MinIO either way, so a deleted document or group can be re-ingested from there if needed.

A `backup` service snapshots FalkorDB and mirrors the MinIO `documents` bucket into `./backups` every `BACKUP_INTERVAL_SECONDS` (default 6h) — see [backup/README.md](backup/README.md) for what's covered, what isn't, and the restore procedure.

## Configuration

See `.env.example`. The important settings are `GRAPHITI_API_BASE_URL`, `GRAPHITI_MODEL`, `GRAPHITI_EMBEDDING_MODEL`, `GRAPHITI_EMBEDDING_DIM`, `GRAPHITI_GROUP_ID`, and `GRAPHITI_CHAT_SESSIONS_GROUP_ID` (the separate group chat sessions are archived into). `GRAPHITI_INGEST_TIMEOUT_SECONDS` (default 1800) caps how long the proxy waits for Graphiti extraction on a slow local model before returning an error. `GCOR_API_KEY`, `WEBUI_SECRET_KEY`, `FALKORDB_PASSWORD`, and `GRAFANA_ADMIN_PASSWORD` are the credentials to set before this stack is reachable beyond an isolated dev box — see [Authentication](#authentication). Do not commit `.env` or credentials.
