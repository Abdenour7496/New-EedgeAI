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

Neo4j, Qdrant, and the Buzz collaboration stack are no longer active services. Their old Docker volumes are intentionally retained by name for rollback/export; the new services do not mount or modify them. See [docs/adr/0001-graphiti-falkordb-backend.md](docs/adr/0001-graphiti-falkordb-backend.md) for the migration rationale, and [openwebui-functions/README.md](openwebui-functions/README.md) for the chat-capture Functions.

## Feasibility and trade-offs

Graphiti officially supports FalkorDB and combines graph traversal, semantic similarity, keyword search, temporal validity, and episode provenance in one retrieval layer. This simplifies the former dual-write Qdrant/Neo4j path and removes cross-database consistency failures.

The material constraint is extraction quality: Graphiti requires an LLM that reliably returns structured JSON. The default local model is `qwen2.5:7b`; smaller local models were not reliable enough in validation. Allocate at least 12 GB RAM to Docker (16 GB recommended). For better extraction quality, point `GRAPHITI_API_BASE_URL`, `GRAPHITI_API_KEY`, and `GRAPHITI_MODEL` at an OpenAI-compatible hosted model.

CPU-only local extraction is also slow — several minutes per ingest call is normal, since each chunk is a sequential LLM call inside Graphiti. `/api/ingest`, `/api/ingest/session`, and the OpenWebUI ingest Functions all wait synchronously for that to finish; the proxy's `GRAPHITI_INGEST_TIMEOUT_SECONDS` (default 1800s) caps how long they'll wait before returning an error, even though the episode may still land a bit later. A hosted model needs far less margin.

This cutover does not automatically transform legacy Neo4j/Qdrant data. Re-ingest source documents to populate Graphiti. Legacy volumes are retained until migration has been accepted. Graphiti groups also have no rename/archive concept — see [Operations](#operations).

## Start

```bash
cp .env.example .env
docker compose -f docker-compose.unified.yml up -d --build --remove-orphans
```

The first start downloads Qwen 2.5 7B, Nomic Embed Text, Llama 3.2, and LLaVA, so it can take several minutes.

To also capture files and chat sessions from Open WebUI (see [Chat and file capture](#chat-and-file-capture) below), install the two Functions in `openwebui-functions/` from Open WebUI's Admin Panel — they aren't wired in automatically.

## Services

| Service | URL / port | Purpose |
|---|---|---|
| Knowledge UI / proxy | http://localhost:5001/knowledge | ingest, browse, search, RAG API |
| Open WebUI | http://localhost:8080 | chat UI |
| Graphiti REST | http://localhost:8000/docs | knowledge ingestion and retrieval |
| FalkorDB Browser | http://localhost:3001 | graph inspection |
| FalkorDB | localhost:6379 | graph/vector database |
| MinIO API | http://localhost:9000 | S3-compatible object storage |
| MinIO console | http://localhost:9001 | browse documents and chat-session snapshots |
| Ollama | http://localhost:11434 | local LLM/embedding/vision backend |
| OpenClaw | http://localhost:18799 | agent gateway (Codex → Claude Code → Ollama) |
| Edge TTS | http://localhost:5050 | OpenAI-compatible text-to-speech |
| Prometheus | http://localhost:9090 | metrics |
| Grafana | http://localhost:3000 | dashboards |

## Knowledge API

```bash
curl -F "file=@report.pdf" -F "collection=documents" http://localhost:5001/api/ingest
curl "http://localhost:5001/api/search?q=your+question&collection=documents"
```

The MinIO watcher also processes objects placed under `documents/inbox/`.

## Chat and file capture

Files attached during an Open WebUI chat, and the chat session itself, are captured automatically once the corresponding Function is installed from **Admin Panel > Functions** (see [openwebui-functions/README.md](openwebui-functions/README.md) for install steps):

| Function | Captures | Stored at | Indexed into |
|---|---|---|---|
| `gcor_file_ingest.py` | files attached to a chat message | `documents/originals/<doc_id>/<filename>` | Graphiti group `documents` (or the workspace's selected group) |
| `gcor_chat_session_ingest.py` | the conversation itself, after every assistant turn | `documents/chat-sessions/<session_id>/<date-time>_<meaningful-name>.json` | Graphiti group `chat_sessions` |

Chat-session naming: the folder is keyed by the chat's stable `session_id`, so a conversation's snapshots stay together for its whole life even as Open WebUI's own title changes. Each snapshot's filename carries the date, time, and the best human-readable name available at that moment — the real chat title once Open WebUI has generated one, otherwise a snippet of the first user message (Open WebUI's placeholder title, e.g. "New Chat", is never used as the name). Example: `chat-sessions/3f9a2b7c-.../2026-08-22T14-05-30Z_what-is-the-capital-of-france.json`.

Past conversations are searchable the same way documents are:

```bash
curl "http://localhost:5001/api/search?collection=chat_sessions&q=your+question"
```

## Operations

```bash
docker compose -f docker-compose.unified.yml ps
docker compose -f docker-compose.unified.yml logs -f graphiti graphiti-mcp falkordb proxy
curl http://localhost:8000/healthcheck
docker exec eedgeai-falkordb-1 redis-cli PING
```

Graphiti groups replace Qdrant collections. A group is materialized on first ingest. Renaming groups is intentionally unsupported; deleting a group (or a single document within one) permanently removes its Graphiti episodes and facts — there is no archive/restore for either. The original file stays in MinIO either way, so a deleted document or group can be re-ingested from there if needed.

## Configuration

See `.env.example`. The important settings are `GRAPHITI_API_BASE_URL`, `GRAPHITI_MODEL`, `GRAPHITI_EMBEDDING_MODEL`, `GRAPHITI_EMBEDDING_DIM`, `GRAPHITI_GROUP_ID`, and `GRAPHITI_CHAT_SESSIONS_GROUP_ID` (the separate group chat sessions are archived into). `GRAPHITI_INGEST_TIMEOUT_SECONDS` (default 1800) caps how long the proxy waits for Graphiti extraction on a slow local model before returning an error. Do not commit `.env` or credentials.
