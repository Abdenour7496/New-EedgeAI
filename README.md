# EedgeAI — Cognitive AI Stack

A production-ready AI agent stack built on **GCOR** (Graph-Centric Orchestrated Retrieval): Neo4j as the cognitive backbone, Qdrant as the semantic perception layer, OpenClaw as the agentic interface, OpenWebUI as the chat frontend, and Edge TTS for voice output — all wired together with a cognitive RAG proxy and full observability stack.

---

## Architecture

```
User
 ├─► Knowledge UI  (localhost:5001)     ← document ingest, search, browse, collection management
 ├─► Open WebUI    (localhost:8080)     ← chat interface + voice (TTS)
 │     └─► GCOR Proxy (port 5001)
 │           ├─► Qdrant  (port 6333)   ← semantic search  (vector perception)
 │           ├─► Neo4j   (port 7687)   ← knowledge graph  (cognitive backbone)
 │           └─► LLM API (GitHub Copilot / OpenAI / Anthropic / Ollama)
 │
 ├─► OpenClaw Agent (localhost:18799)  ← agentic interface with tools + Telegram bot
 │     ├─► relay (embedded, :18799→:18789, :18801→:18790, metrics :9091)
 │     ├─► mcp-qdrant (port 8765)  ──► Qdrant
 │     └─► mcp-neo4j  (port 8766)  ──► Neo4j
 │
 └─► Edge TTS      (localhost:5050)    ← OpenAI-compatible text-to-speech (Microsoft Edge voices)

Buzz (optional: `--profile buzz`)
 └─► Buzz Relay     (localhost:3010)    ← human + agent collaboration workspace
   ├─► Postgres                         ← Buzz event store
   ├─► Redis                            ← presence and pub/sub
   ├─► MinIO                            ← media and Git object storage
   └─► Buzz Agent Bridge                ← joins a channel as its own identity; on
                                            @mention, answers via GCOR Proxy
                                            /v1/buzz/chat/completions (bearer-auth'd)

Monitoring
 ├─► Prometheus (localhost:9090)   ← scrapes proxy + qdrant + openclaw metrics every 15s
 └─► Grafana    (localhost:3000)   ← auto-provisioned GCOR observability dashboard
```

### GCOR Retrieval Pipeline (every chat message via OpenWebUI)

1. **Intent classification** — keyword-based: `factual | planning | dependency | memory | semantic | inference | belief`
2. **Semantic phase** — embed query → top-K Qdrant hits, filtered by confidence, temporal validity, and access level
3. **Structural phase** — intent-specific Neo4j Cypher expansion using the `neo4j_element_id` from each Qdrant hit
4. **Reflection check** — fallback to chunk text when graph is empty; pure LLM when both are empty
5. **Context injection** — structured system message with confidence scores, temporal badges, reasoning traces
6. **LLM call** — forwarded to the configured backend with enriched context

### Cognitive Infrastructure

Every node in Neo4j and every vector point in Qdrant carries:

| Property | Purpose |
|---|---|
| `confidence` | Float 0.0–1.0 — certainty score, filterable at query time |
| `valid_from` / `valid_to` | ISO-8601 temporal validity window — expired knowledge is excluded |
| `agent_id` | Agent partition — scopes memory/belief/inference to one agent |
| `access_level` | ACL: `public` \| `restricted` \| `agent:<id>` |

Cognitive node types supported in Neo4j:

| Label | Description |
|---|---|
| `:Document` | Ingested source document |
| `:Chunk` | Text segment of a Document, linked via `CONTAINS` |
| `:Memory` | Agent observations, reflections, plans, facts |
| `:Inference` | Reasoned conclusion with `reasoning_trace` and `DERIVED_FROM` sources |
| `:Belief` | Agent epistemic state with `HOLDS`, `ABOUT`, and `CONTRADICTS` relationships |
| `:Goal` | Agent objectives |
| `:Event` | Timestamped occurrences |
| `:Concept` | Named concepts linked to memories and beliefs |
| `:ArchivedCollection` | Archive metadata for deleted Qdrant collections (used by restore) |

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- At least **8 GB RAM** allocated to Docker
- Ports `3000, 5001, 5050, 6333, 7474, 7687, 8080, 9090, 18799, 18801` free
- Port `3010` free when enabling the optional Buzz profile

---

## Step 1 — Configure Environment Variables

Copy `.env.example` and fill in your credentials:

```bash
cp .env.example .env
```

### LLM & API Keys

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key — used for LLM calls and embeddings |
| `ANTHROPIC_API_KEY` | Anthropic API key — used by the GCOR proxy and OpenClaw |
| `COPILOT_API_KEY` | GitHub classic PAT — used for GitHub Copilot model access in OpenClaw |

> **GitHub Copilot PAT:** Must be a **classic** token (starts with `ghp_`). Fine-grained tokens (`github_pat_`) are not supported by the Copilot token exchange API. Generate one at [github.com/settings/tokens](https://github.com/settings/tokens) — no scopes required, just an active Copilot Individual/Business subscription on the account.

### OpenClaw Gateway

| Variable | Description |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | Token for OpenClaw gateway authentication |
| `OPENCLAW_GATEWAY_PASSWORD` | Password for OpenClaw gateway |

Optional overrides:

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_GATEWAY_PORT` | `18789` | Internal gateway port |
| `OPENCLAW_BRIDGE_PORT` | `18790` | Internal bridge port |
| `OPENCLAW_GATEWAY_BIND` | `lan` | Network bind mode |
| `OPENCLAW_CONFIG_DIR` | `openclaw_config` | Config volume path |
| `OPENCLAW_DOCKER_APT_PACKAGES` | _(empty)_ | Extra apt packages installed at container start |

### LLM & Embeddings

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `openclaw` | GCOR proxy backend: `openclaw`, `openai`, `anthropic`, or `ollama` |
| `OPENAI_CHAT_MODEL` | `gpt-4o` | OpenAI model for chat completions |
| `ANTHROPIC_CHAT_MODEL` | `claude-haiku-4-5-20251001` | Anthropic model for chat completions |
| `COPILOT_CHAT_MODEL` | `gpt-4.1` | GitHub Copilot model used by the proxy fallback |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model for local inference |
| `EMBEDDING_BACKEND` | `ollama` | Embedding provider: `openai` or `ollama` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama embedding model (when `EMBEDDING_BACKEND=ollama`) |

### Cognitive Knobs

| Variable | Default | Description |
|---|---|---|
| `QDRANT_COLLECTION` | `documents` | Default Qdrant collection name |
| `QDRANT_TOP_K` | `8` | Number of semantic search results |
| `ENABLE_RAG` | `true` | Set `false` to disable GCOR and use plain LLM |
| `CONFIDENCE_THRESHOLD` | `0.0` | Drop knowledge below this confidence (0.0–1.0) |
| `AGENT_ID` | _(empty)_ | Scope retrieval to a specific agent partition |
| `DEFAULT_ACCESS_LEVEL` | `public` | Default ACL for new nodes |

### Text-to-Speech (TTS)

| Variable | Default | Description |
|---|---|---|
| `TTS_API_KEY` | `tts-local` | Bearer token for the Edge TTS service |
| `TTS_DEFAULT_VOICE` | `en-US-AriaNeural` | Default Microsoft Edge TTS voice |
| `TTS_DEFAULT_FORMAT` | `mp3` | Audio output format: `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` |
| `TTS_DEFAULT_SPEED` | `1.0` | Playback speed (0.25–4.0) |

### Telegram

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token for the OpenClaw Telegram channel |

> **Security:** Never commit `.env` to version control.

---

## Step 2 — Project Structure

```
eedgeai/
├── docker-compose.unified.yml
├── .env.example
├── openclaw/
│   ├── Dockerfile                 ← extends openclaw base image (build context: project root)
│   ├── entrypoint.sh              ← seeds config from /opt/openclaw-seed, starts relay, execs openclaw
│   ├── hotfix-openclaw-runtime.js ← patches the bundled runtime bundle at build time
│   ├── package.json               ← neo4j-driver, qdrant client, pdf-parse, mammoth
│   ├── package-lock.json          ← lockfile (committed for reproducible builds)
│   ├── neo4j.js                   ← neo4j-cli  (shell tool for OpenClaw agent)
│   ├── qdrant.js                  ← qdrant-cli (shell tool for OpenClaw agent)
│   ├── ingest.js                  ← ingest-cli (document ingestion tool)
│   └── image-extractor.js         ← image extraction helper
├── openclaw-config/
│   └── openclaw.json              ← seed config: models, fallbacks, MCP servers, Telegram, auth profiles
├── openclaw-relay/
│   └── relay.js                   ← TCP relay + Prometheus metrics :9091 (embedded in openclaw container)
├── neo4j-exporter/                ← Prometheus exporter for Neo4j Community Edition
├── mcp-servers/
│   ├── qdrant/
│   │   ├── Dockerfile
│   │   └── server.py              ← GCOR-aware MCP server for Qdrant (SSE :8765)
│   └── neo4j/
│       ├── Dockerfile
│       └── server.py              ← Cognitive MCP server for Neo4j (SSE :8766)
├── proxy/
│   ├── Dockerfile
│   ├── main.py                    ← GCOR proxy + Knowledge UI + ingest API + metrics
│   ├── requirements.txt
│   └── templates/
│       └── knowledge.html         ← Knowledge management UI
├── ingest-watcher/
│   ├── Dockerfile
│   ├── watcher.py                 ← polls MinIO documents/inbox/ → POST /api/ingest
│   └── requirements.txt
├── monitoring/
│   ├── prometheus.yml             ← scrape config (proxy + qdrant + openclaw, 15s interval)
│   ├── grafana-dashboard.json     ← auto-provisioned GCOR observability dashboard
│   └── grafana-provisioning/
│       ├── datasources/
│       │   └── prometheus.yaml    ← Prometheus datasource (uid: prometheus)
│       └── dashboards/
│           └── default.yaml       ← dashboard file provider config
└── agentic-stack/
    └── agentic_stack/
        └── graph/
            └── schema.py          ← Neo4j constraints and indexes
```

---

## Step 3 — Start the Stack

```bash
docker compose -f docker-compose.unified.yml up -d
```

First run pulls all images and builds custom containers (~5–10 min). Services start in dependency order: Neo4j and Qdrant → MCP servers → OpenClaw → Proxy → TTS → OpenWebUI.

### Optional: Start Buzz

Buzz is isolated behind a Compose profile so it does not affect the default stack. It runs a self-hosted collaboration relay with its own Postgres, Redis, MinIO, migration job, and persistent named volumes:

```bash
docker compose -f docker-compose.unified.yml --profile buzz up -d buzz
```

Open `http://localhost:3010` for the bundled web UI, or configure a Buzz desktop client with `ws://localhost:3010`. Before exposing the relay beyond a trusted local network, replace the development Buzz passwords in `.env` and set `BUZZ_RELAY_URL` to its public `wss://` URL.

#### Optional: Knowledge Bridge (Buzz ↔ GCOR)

Lets both humans and agents in a Buzz channel retrieve GCOR collections/documents by @mentioning the bridge — it's a two-way integration: the bridge reads the channel, runs the same GCOR retrieval pipeline as the chat UI (Qdrant + Neo4j), and posts the answer back into the thread. Verified working end-to-end against a real Buzz instance, not just wired up.

**How it works:** `buzz-agent-bridge` runs upstream `buzz-acp` (built from [block/buzz](https://github.com/block/buzz), pinned tag) — a first-party harness that holds its own Nostr keypair, authenticates to the Buzz relay, and watches only the channel(s) it's configured for. On a mention it spawns `buzz-bridge/reply_adapter.py` for that turn — a small deterministic agent (not an LLM tool-calling loop): it calls the GCOR proxy's `POST /v1/buzz/chat/completions` and *always* posts whatever text comes back via `buzz messages send`, threaded as a reply. (We initially tried the upstream `buzz-agent` + a `buzz-dev-mcp` shell tool, matching how Buzz's own agents work — the model would answer the question correctly but frequently never chose to invoke the "post it" tool call, so replies silently vanished. The deterministic adapter removes that judgment call: the model never decides whether or what to execute, it can only answer the question that reached it.) `/v1/buzz/chat/completions` is a route separate from the normal chat endpoint, gated behind a bearer token (`BUZZ_BRIDGE_API_KEY`) since Buzz channel content is a less-trusted source than the rest of the internal docker network.

A `buzz-relay-proxy` sidecar (nginx) also sits in front of the relay for the bridge's own traffic. The relay resolves which "community"/workspace a request belongs to strictly by the exact `Host` header it arrives on (`communities.host` in Postgres, unique — no aliasing), set to whatever host the Buzz desktop/web client first connected through (typically `localhost:3010`). Traffic from inside the docker network naturally arrives as `buzz:3000` and gets rejected outright (`404: no community configured for this host`) — including the NIP-42 auth handshake itself, since `buzz-acp` signs its auth event claiming the exact relay URL it was given, and the server requires that to match its own self-identity, not just be reachable. `buzz-relay-proxy` rewrites the Host header for the bridge's traffic; a static IP on a dedicated `buzz_bridge_net` network plus an `extra_hosts` override make `ws://localhost:3010` (what the bridge is configured to claim) actually route to that proxy instead of the container's own loopback. None of this touches how the desktop/web client itself connects.

Security is layered:
- **Channel membership** (Buzz-side ACL) — invite the bridge identity into a channel exactly like inviting a person; it never sees channels it isn't a member of. Membership itself requires an existing owner/admin of that channel to grant it — neither the bridge nor the relay's own admin key can self-escalate into a channel.
- **`BUZZ_KNOWLEDGE_CHANNELS`** — an explicit allow-list the bridge itself enforces, independent of relay-side membership.
- **`BUZZ_ACP_SUBSCRIBE=mentions`** — only triggers on an explicit `@mention`, not every message in the channel.
- **No LLM-driven tool/shell access** — `reply_adapter.py` deterministically does exactly one thing (ask the proxy, post the answer); the model has no ability to choose or execute any other action.
- **`BUZZ_BRIDGE_API_KEY`** — bearer token required by the proxy; `/v1/buzz/chat/completions` and `/v1/buzz/ingest` both return `503` if it isn't set, and `401` on a bad/missing token.
- **Rate limiting + input caps** on the proxy side (`BUZZ_BRIDGE_RATE_LIMIT_PER_MIN`, message count/size limits, and `BUZZ_BRIDGE_MAX_UPLOAD_BYTES` for `/v1/buzz/ingest`) bound cost if the token ever leaks or the bridge misbehaves.
- **`BUZZ_ALLOWED_COLLECTIONS`** (optional) — restrict which Qdrant collections Buzz-originated queries can target.

Setup:

```bash
# 1. The relay needs a *stable* signing key before any buzz-admin add-member/
#    remove-member call will work (it signs the membership roster with it; without
#    it the relay re-generates a throwaway key every restart). Generate one, put it
#    in .env as BUZZ_RELAY_PRIVATE_KEY, then (re)start the relay so it picks it up:
docker compose -f docker-compose.unified.yml --profile buzz run --rm buzz-migrate generate-key
# → paste the printed secret key into BUZZ_RELAY_PRIVATE_KEY in .env, then:
docker compose -f docker-compose.unified.yml --profile buzz up -d buzz

# 2. Generate a *separate* keypair for the bridge's own agent identity
docker compose -f docker-compose.unified.yml --profile buzz run --rm buzz-migrate generate-key

# 3. Register the printed pubkey as a relay member
docker compose -f docker-compose.unified.yml --profile buzz run --rm \
  buzz-migrate add-member --pubkey <public-key-printed-in-step-2> --role member
```

Then, in `.env`:
- Paste the secret key from step 2 into `BUZZ_AGENT_PRIVATE_KEY`
- Generate a secret with `openssl rand -hex 32` and set both `BUZZ_BRIDGE_API_KEY` (this value is read by both the proxy and the bridge)
- Find (or create) the target channel's UUID — easiest via `SELECT id, name FROM channels;` in `buzz-postgres` if you don't want to dig through the client UI — and set `BUZZ_KNOWLEDGE_CHANNELS` to it (comma-separated for multiple)
- Get the bridge's pubkey into that channel. If the channel has an open add-policy, the bridge can self-join:
  ```bash
  docker compose -f docker-compose.unified.yml --profile buzz run --rm \
    --entrypoint /usr/local/bin/buzz-cli -e BUZZ_RELAY_URL=ws://localhost:3010 \
    -e BUZZ_PRIVATE_KEY=<bridge-secret-key-from-step-2> \
    buzz-agent-bridge channels join --channel <channel-uuid>
  ```
  Private channels need an existing owner/admin to invite the bridge's pubkey from the Buzz client instead — the bridge (and even the relay's own admin key) can't add itself to those.

```bash
docker compose -f docker-compose.unified.yml --profile buzz up -d buzz-relay-proxy buzz-agent-bridge
```

In the channel, @mention the bridge with a question — e.g. `@gcor-bot what does the Q4 report say about churn?` — and it replies with the GCOR-retrieved answer, threaded to your message.

**Ingesting documents from a channel.** Buzz's own upload/media pipeline is images-only by design (`image/jpeg`, `image/png`, `image/gif`, `image/webp` — confirmed against `crates/buzz-media/src/validation.rs` in the block/buzz source; there's no path for attaching a PDF/DOCX/TXT to a Buzz message at all). Two ways around that, both via a new bearer-authenticated `POST /v1/buzz/ingest` route on the proxy (same trust boundary as `/v1/buzz/chat/completions`: requires `BUZZ_BRIDGE_API_KEY`, shares its rate limit, and separately caps upload size via `BUZZ_BRIDGE_MAX_UPLOAD_BYTES`, default 20 MB):

- **Attach an image and @mention the bridge** — e.g. a photo of a receipt or whiteboard, or a screenshot. `reply_adapter.py` reads the `imeta` tag Buzz puts in the triggering event's `Tags:` (see `crates/buzz-acp/src/queue.rs::format_event_block` — the ACP prompt always includes the full raw tag JSON), downloads the attachment with `buzz media get` (handles Blossom auth), and ingests it — OCR/vision extraction, same as any other image via the Knowledge UI. The bridge replies with a confirmation (`document_id`, chunk count) instead of an answer.
- **`@gcor-bot ingest <name>`** — ingest (or re-confirm) a file that's already sitting in MinIO, e.g. dropped into `documents/inbox/` (see "Via MinIO" under Step 7 — Ingest Documents), without needing a Buzz-native upload at all. The proxy searches the bucket root, `inbox/`, `processed/`, `failed/`, and `originals/*/` for a matching name.

If a triggering message has both an image attachment and other text, the attachment takes priority — it's treated as an ingest request, not a question.

**Giving it a display name:** worth doing, not just cosmetic — Buzz's `@name` mention parsing resolves display names against channel members' `kind:0` profiles, so an unnamed identity can't reliably be `@mentioned` by typing at all (only via a raw `nostr:` URI). `buzz-relay-proxy` (started above) makes this work from inside the docker network:
```bash
docker compose -f docker-compose.unified.yml --profile buzz run --rm \
  --entrypoint /usr/local/bin/buzz-cli -e BUZZ_RELAY_URL=ws://localhost:3010 \
  -e BUZZ_PRIVATE_KEY=<bridge-secret-key-from-step-2> \
  buzz-agent-bridge users set-profile --name "gcor-bot" --about "Mention me with a question and I'll answer from the GCOR document collections."
```

Watch logs:
```bash
docker compose -f docker-compose.unified.yml logs -f
```

---

## Step 4 — Verify All Services

```bash
docker compose -f docker-compose.unified.yml ps
```

| Service | Port(s) | Description |
|---|---|---|
| `openwebui` | 8080 | Chat UI — select **openclaw** model |
| `proxy` | 5001 | GCOR pipeline + Knowledge UI + `/metrics` |
| `tts` | 5050 | OpenAI-compatible Edge TTS (`/v1/audio/speech`) |
| `openclaw` | 18799, 18801, 9091 | OpenClaw agent UI + embedded relay + relay metrics |
| `ollama` | 11434 | Local LLM server |
| `mcp-qdrant` | 8765 (internal) | MCP server — Qdrant tools for OpenClaw |
| `mcp-neo4j` | 8766 (internal) | MCP server — Neo4j tools for OpenClaw |
| `neo4j` | 7474, 7687 | Graph database |
| `neo4j-exporter` | (internal) | Prometheus exporter for Neo4j Community Edition |
| `qdrant` | 6333 | Vector database + `/metrics` |
| `minio` | 9000 (S3 API), 9001 (console) | Document storage — originals + `inbox/` drop folder |
| `ingest-watcher` | (internal) | Polls `documents/inbox/` and pushes new files into `/api/ingest` |
| `prometheus` | 9090 | Metrics collection |
| `grafana` | 3000 | Observability dashboard |
| `buzz` (optional profile) | 3010 | Buzz human + agent collaboration relay |
| `buzz-relay-proxy` (optional profile) | (internal) | Host-header rewrite so server-side clients resolve the same community as the desktop/web client |
| `buzz-agent-bridge` (optional profile) | (internal) | Buzz ↔ GCOR knowledge bridge — no exposed port |

---

## Step 5 — Configure OpenClaw Authentication

OpenClaw uses **GitHub Copilot** as its primary LLM backend. On first run, authenticate via the GitHub device flow:

### Option A — Device flow (recommended, no PAT needed)

```bash
# Trigger the device flow from inside the container
docker exec -it eedgeai-openclaw-1 openclaw models auth login-github-copilot
```

This prints a URL and a short code. Open the URL in your browser, sign in as your GitHub account, and enter the code. The OAuth token is stored in `/home/node/.openclaw/agents/main/agent/auth-profiles.json` inside the container volume (persists across restarts).

### Option B — Classic PAT via environment variable

Set `COPILOT_API_KEY` in `.env` to a GitHub **classic** PAT (`ghp_...`). The compose file maps it to `COPILOT_GITHUB_TOKEN` automatically, which the GitHub Copilot plugin reads on startup.

### Model fallback chain

OpenClaw is configured with a three-tier fallback (see `openclaw-config/openclaw.json`):

| Priority | Model | Notes |
|---|---|---|
| 1 (primary) | `github-copilot/gpt-4.1` | Uses Chat quota — reliable on Individual plan |
| 2 (fallback) | `github-copilot/gpt-5.4` | Uses Premium quota — may hit limits on Individual plan |
| 3 (last resort) | `ollama/llama3.2:latest` | Fully local, no quota |

> **Premium quota:** On the GitHub Copilot Individual monthly plan, `gpt-5.4` draws from a limited Premium quota. Once exhausted it returns HTTP 429 and OpenClaw falls back to `gpt-4.1` automatically. Swap the primary/fallback order once your quota resets if you prefer `gpt-5.4` first.

To change the primary model at runtime:
```bash
docker exec eedgeai-openclaw-1 openclaw models set github-copilot/gpt-4.1
docker exec eedgeai-openclaw-1 openclaw models status
```

---

## Step 6 — Configure Telegram (optional)

OpenClaw includes a Telegram channel. Set `TELEGRAM_BOT_TOKEN` in `.env` (create a bot via [@BotFather](https://t.me/BotFather)), then start the stack. The bot starts automatically.

**First-time pairing:** When you message the bot for the first time, OpenClaw holds the message as a pending pairing request (security feature). Approve it:

```bash
# List pending pairing requests
docker exec eedgeai-openclaw-1 openclaw pairing list

# Approve by code (shown in the list output)
docker exec eedgeai-openclaw-1 openclaw pairing approve <CODE>
```

After approval, all messages to the bot are routed to the OpenClaw agent.

---

## Step 7 — Ingest Documents

### Via the Knowledge UI (recommended)

Open **http://localhost:5001** in your browser. It redirects to the Knowledge page:

- **Cards** show each Qdrant collection with document count, chunk count, and recent files
- **New Collection** button — create an empty Qdrant collection with custom name and vector size
- **Archives** button — browse and restore previously deleted collections
- **Ingest button** (per card) — drag-and-drop or click to upload; supports `.txt` `.md` `.pdf` `.docx` `.json` `.csv`
- **Test button** — run a semantic search query against the collection
- **View button** — browse all ingested documents with chunk counts and timestamps
- **Rename** (✏ icon) — rename a collection (scroll-copies all vectors to the new name)
- **Delete** (🗑 icon) — archive a collection: copies all Qdrant vectors to `_archived_<name>_<ts>` and marks Neo4j `Document` nodes as `archived=true`; the data is never destroyed and can be restored

Each upload:
1. Extracts text from the file
2. Chunks it into ~2000-character segments with overlap
3. Creates a `:Document` node → `:Chunk` nodes in Neo4j (linked via `CONTAINS`)
4. Embeds each chunk and upserts it to Qdrant with the Neo4j `elementId` as `neo4j_element_id`

### Collection Management API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/collections` | List all collections with doc/vector stats |
| `POST` | `/api/collections` | Create a new empty collection |
| `PATCH` | `/api/collections/{name}` | Rename a collection (scroll-copy + delete) |
| `DELETE` | `/api/collections/{name}` | Archive a collection (preserves all data) |
| `GET` | `/api/archives` | List all archived collections |
| `POST` | `/api/archives/{archive_name}/restore` | Restore an archive (optionally under a new name) |

### Via OpenClaw agent (ingest-cli)

Inside the OpenClaw chat, ask the agent to ingest a file using the built-in `ingest-cli` tool:

```bash
ingest-cli /path/to/document.pdf --title "Q4 Report"
ingest-cli /path/to/spec.docx --title "Technical Spec"
ingest-cli /path/to/doc.txt --agent-id "my-agent" --access-level restricted
echo "content here" | ingest-cli --stdin --title "Quick Note"
```

### Via API

```bash
curl -X POST http://localhost:5001/api/ingest \
  -F "file=@report.pdf" \
  -F "title=Q4 Report" \
  -F "access_level=public"
```

### Via MinIO (document storage + drop-folder auto-ingest)

MinIO is part of the default stack (`minio` + `minio-init` + `ingest-watcher`, no profile flag needed) and serves two roles:

**1. Durable storage for every ingested original.** Every path above (`Knowledge UI`, `ingest-cli`, direct API) now also persists the raw uploaded file to MinIO — bucket `documents` (configurable via `DOCS_S3_BUCKET`), key `originals/<document_id>/<filename>`. The `Document` node in Neo4j gets `storage_bucket`/`storage_key` properties, and the `/api/ingest` response includes a `storage` field, so any ingested document can be traced back to its original file. This is best-effort: if MinIO is unreachable, ingestion still succeeds — you just lose the durable copy for that document.

**2. A watched drop folder for bulk/automated ingestion.** Copy files into `documents/inbox/` (any S3-compatible client works — `mc`, `aws s3`, boto3, rclone, ...) and the `ingest-watcher` container polls that prefix every `INGEST_WATCH_INTERVAL_SECONDS` (default 10s), pushes each new file through `POST /api/ingest`, and moves it to `processed/` on success or `failed/` (with a `<file>.error.txt` explaining why) on failure. Nothing is reprocessed — objects only live in `inbox/` until picked up.

```bash
# Point mc at the local MinIO and drop a file into the inbox
mc alias set local http://localhost:9000 gcor_docs <DOCS_S3_SECRET_KEY>
mc cp report.pdf local/documents/inbox/report.pdf
# ...within ~10s it's ingested into Qdrant/Neo4j and moved to processed/report.pdf
```

MinIO console: **http://localhost:9001** (login with `DOCS_S3_ACCESS_KEY` / `DOCS_S3_SECRET_KEY` from `.env`). S3 API: **http://localhost:9000**.

Tune ingestion behavior for watched files via env vars: `INGEST_COLLECTION` (defaults to `QDRANT_COLLECTION`), `INGEST_ENABLE_DOCINT` (run full Document Intelligence — OCR/tables/entities — on drop-folder files; default `false`), `INGEST_ACCESS_LEVEL` (default `public`).

---

## Step 8 — Chat via OpenWebUI

Open **http://localhost:8080**:

1. Create an account on first visit
2. Select **Openclaw** from the model dropdown
3. Every message is automatically enriched via the GCOR pipeline before reaching the LLM
4. Use the **speaker icon** on any response to have it read aloud via Edge TTS

### Available TTS voices

The default voice is `en-US-AriaNeural`. To use a different voice, set `TTS_DEFAULT_VOICE` in `.env` and restart the `tts` container. Full list: [github.com/travisvn/openai-edge-tts#available-voices](https://github.com/travisvn/openai-edge-tts#available-voices).

---

## Step 9 — Chat via OpenClaw

Open **http://localhost:18799**:

- Full agentic interface with tool use
- Native access to `neo4j-cli`, `qdrant-cli`, and `ingest-cli` as shell tools
- Connects to Neo4j and Qdrant via MCP sidecar servers (SSE)
- Primary model: `github-copilot/gpt-4.1` (configurable)

To override the active model at runtime:
```bash
docker exec eedgeai-openclaw-1 openclaw models set github-copilot/gpt-4.1
# List all available (authenticated) models:
docker exec eedgeai-openclaw-1 openclaw models list --all
# Check model + auth status:
docker exec eedgeai-openclaw-1 openclaw models status
```

### MCP tools available to the OpenClaw agent

Both MCP servers are registered at startup via `openclaw-config/openclaw.json`:

| Server | URL | Tools exposed |
|---|---|---|
| `qdrant-memory` | `http://mcp-qdrant:8765/sse` | Semantic search, temporal search, vector upsert/delete |
| `neo4j-graph` | `http://mcp-neo4j:8766/sse` | Graph queries, memory/belief/inference CRUD, confidence updates |

---

## Step 10 — Browse the Knowledge Graph

Open **http://localhost:7474** (Neo4j Browser):
- **Username:** `neo4j` · **Password:** `test1234`

Useful Cypher queries:

```cypher
// All documents
MATCH (d:Document) RETURN d ORDER BY d.created_at DESC

// Document with its chunks
MATCH (d:Document)-[:CONTAINS]->(c:Chunk)
WHERE d.title CONTAINS "report"
RETURN d, c

// All cognitive nodes for an agent
MATCH (n) WHERE n.agent_id = "my-agent"
RETURN labels(n), n.confidence, n.created_at LIMIT 50

// Archived collections
MATCH (a:ArchivedCollection) RETURN a ORDER BY a.archived_at DESC
```

---

## Step 11 — Monitor (Grafana)

Open **http://localhost:3000** · `admin / admin`

The **EedgeAI — GCOR Full Observability** dashboard is auto-provisioned with 39 panels across 7 rows:

| Row | What you see |
|---|---|
| **Service Health** | Proxy / Neo4j / Qdrant / OpenClaw up/down, RAG requests (24h), ingests (24h) |
| **GCOR RAG Pipeline** | Request rate by intent, latency p50/p95/p99, intent distribution pie, fallback mode donut, avg Qdrant hits + Neo4j records per request |
| **LLM & Embeddings** | LLM call rate by backend, latency p50/p95, embedding API latency, error and exception counters |
| **Ingest Operations** | Ingest rate (success / error), avg chunks per document, total documents ingested, collection ops (1h) |
| **HTTP API (Proxy)** | Request rate by handler, 4xx / 5xx error rate, latency p50/p95/p99 |
| **Qdrant Vector DB** | REST request rate by endpoint, REST latency p95, gRPC request rate, collections total, vectors total |
| **Prometheus Health** | Scrape target availability over time, scrape duration per job |

### Prometheus Scrape Targets

| Target | Job | Metrics path |
|---|---|---|
| `proxy:5001` | `proxy` | `/metrics` |
| `qdrant:6333` | `qdrant` | `/metrics` |
| `openclaw:9091` | `openclaw` | `/metrics` — gateway up/down, connection counters |

### Proxy Metrics (`/metrics`)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `gcor_rag_requests_total` | Counter | `intent`, `fallback_mode` | Every GCOR RAG pipeline invocation |
| `gcor_rag_duration_seconds` | Histogram | — | End-to-end pipeline latency |
| `gcor_qdrant_hits` | Histogram | — | Qdrant hits after cognitive filters |
| `gcor_neo4j_records` | Histogram | — | Neo4j graph records expanded |
| `gcor_embed_duration_seconds` | Histogram | — | Embedding API call latency |
| `gcor_llm_requests_total` | Counter | `backend`, `status` | LLM API calls |
| `gcor_llm_duration_seconds` | Histogram | `backend` | LLM call latency (non-streaming) |
| `gcor_ingest_total` | Counter | `status` | Document ingest operations |
| `gcor_ingest_chunks` | Histogram | — | Chunks produced per ingested document |
| `gcor_collection_ops_total` | Counter | `operation` | Collection create / rename / archive / restore |

---

## Stopping the Stack

```bash
docker compose -f docker-compose.unified.yml down

# Also remove all stored data
docker compose -f docker-compose.unified.yml down -v
```

---

## Rebuilding After Code Changes

```bash
docker compose -f docker-compose.unified.yml build <service>
docker compose -f docker-compose.unified.yml up -d <service>
```

### Upgrading OpenClaw

`openclaw/Dockerfile` pins an exact base image tag (`FROM ghcr.io/openclaw/openclaw:<version>`), not `:latest` — `docker pull ...:latest` alone does not change what gets built. To actually upgrade, bump the tag in the Dockerfile to the version you want (see [releases](https://github.com/openclaw/openclaw/releases)), then:

```bash
docker pull ghcr.io/openclaw/openclaw:<new-version>
docker compose -f docker-compose.unified.yml build --no-cache openclaw
docker compose -f docker-compose.unified.yml up -d openclaw
```

The Dockerfile applies a runtime hotfix patch on top of the new base image. If the patch target is no longer found (warning during build), it means the upstream version already includes the fix — this is safe to ignore.

**If the container starts crash-looping with `OpenClaw startup migrations did not complete cleanly` / `Skipped Memory Core legacy memory index import ... legacy rows could not be imported`:** this happened going from `2026.6.11` → `2026.7.1` against a long-lived `openclaw_config` volume. The error message tells you to run `openclaw doctor --fix`, but as of `2026.7.1` that does **not** actually resolve this specific warning — it fixes other things (config key migrations, corrupted recall-store entries) but the gateway keeps refusing to start with the identical error on every restart. Verified via `docker compose run --rm --no-deps openclaw openclaw doctor --fix`, `openclaw memory status --deep`, and `openclaw memory status --index --agent main` — all ran clean, none cleared the warning. Until this is fixed upstream, pin back to the last known-good tag (`2026.6.11`) and rebuild rather than digging into the raw state SQLite files by hand.

---

## Troubleshooting

**OpenClaw shows `ERR_EMPTY_RESPONSE` on port 18799:**
```bash
docker compose -f docker-compose.unified.yml up -d --no-deps openclaw
```

**OpenClaw Copilot auth failing (HTTP 403 or 404):**
- HTTP 403 → fine-grained PAT used; switch to a classic PAT (`ghp_...`)
- HTTP 404 → account has no active Copilot subscription

Re-run the device flow to refresh the token:
```bash
docker exec -it eedgeai-openclaw-1 openclaw models auth login-github-copilot --yes
```

**"Lost connection to the LLM" / chat requests hang for exactly ~120s then fail:**
This is almost always Copilot premium quota exhaustion (`429 quota exceeded`), not a broken connection or changed credentials — openclaw's provider plugin doesn't surface that 429 cleanly, it just hangs until its idle timeout (~120s) and reports `LLM idle timeout (120s): no response from model`. Confirm directly (bypasses openclaw's own retry/timeout handling, gives you the real status fast):
```bash
docker exec eedgeai-openclaw-1 node -e "
const t = require('/home/node/.openclaw/credentials/github-copilot.token.json');
fetch('https://api.githubcopilot.com/chat/completions', {
  method: 'POST',
  headers: {'Authorization': 'Bearer ' + t.token, 'Content-Type': 'application/json',
            'Copilot-Integration-Id': 'vscode-chat', 'Editor-Version': 'vscode/1.0.0'},
  body: JSON.stringify({model: 'gpt-4o', messages: [{role:'user', content:'ping'}], max_tokens: 5}),
}).then(async r => console.log(r.status, await r.text()));
"
```
`429` → quota exhausted, wait for it to reset (check GitHub Copilot billing) — this deployment's model list has no secondary Copilot model configured to fail over to, only the local Ollama fallback. If that fallback *also* times out even though `docker exec <ollama-container> ollama list` shows the model and direct Ollama requests are fast, that's a separate, known issue: a full agent turn bundles the system prompt plus every MCP tool's schema (`qdrant-memory`, `neo4j-graph`, etc.), and small local models can take longer than openclaw's idle timeout to churn through that on CPU-only inference — not a wiring bug.

**Ollama fallback fails instantly with `"<model>" does not support thinking`:**
The model's entry in `models.providers.ollama.models[]` has `"reasoning": true` set for a model that doesn't support Ollama's thinking-mode parameter. Set it to `false` in `openclaw-config/openclaw.json` — it hot-reloads without a container restart (`[reload] config hot reload applied` in the logs confirms it took).

**OpenClaw MCP tools not available:**
```bash
docker exec eedgeai-openclaw-1 openclaw mcp list
# Should show qdrant-memory and neo4j-graph
docker compose -f docker-compose.unified.yml restart mcp-qdrant mcp-neo4j
```

**Telegram bot not responding after first message:**
The first message triggers a pairing request. Approve it:
```bash
docker exec eedgeai-openclaw-1 openclaw pairing list
docker exec eedgeai-openclaw-1 openclaw pairing approve <CODE>
```

**TTS not working in OpenWebUI:**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:5050/v1/audio/speech \
  -H "Authorization: Bearer tts-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"test","voice":"en-US-AriaNeural"}'
# Expected: 200
docker compose -f docker-compose.unified.yml logs tts
```

**OpenWebUI shows no models / only one model:**
```bash
curl http://localhost:5001/v1/models | grep '"id"'
# First model should be "openclaw"
curl http://localhost:5001/health
```

**Knowledge UI shows no collections:**
The `documents` Qdrant collection is created automatically on first ingest. If Qdrant was wiped, ingest any document via the Knowledge UI to recreate it.

**Files dropped into `documents/inbox/` never get ingested:**
```bash
docker compose -f docker-compose.unified.yml logs -f ingest-watcher
```
- No log output at all → container isn't running; `docker compose up -d ingest-watcher`
- `poll loop error` with a connection error to `minio:9000` → `minio` isn't healthy yet, or `S3_ACCESS_KEY`/`S3_SECRET_KEY` in `ingest-watcher`'s environment don't match `DOCS_S3_ACCESS_KEY`/`DOCS_S3_SECRET_KEY` in `.env`
- `ingest failed for inbox/<file>: ...` → the file was moved to `documents/failed/<file>` with a sibling `<file>.error.txt` in MinIO explaining the proxy's rejection (usually an unsupported extension or empty file, same as a manual `/api/ingest` call would raise)
- File uploaded fine but nothing shows up in `documents/inbox/` at all → check you're writing to the right bucket/prefix (`mc ls local/documents/inbox/`) and that `mc mb`/`mc anonymous set` succeeded (`docker compose logs minio-init`)

**`/api/ingest` response has no `storage` field / originals aren't showing up in MinIO:**
This is best-effort by design — ingestion isn't blocked by a MinIO outage. Check `docker compose logs proxy | grep "S3 store"` for the warning, then verify `S3_ACCESS_KEY`/`S3_SECRET_KEY`/`S3_BUCKET` match between the `proxy` and `minio` services' environments, and that `minio` is healthy (`docker compose ps minio`).

**Buzz bridge doesn't respond to @mentions:**
```bash
docker compose -f docker-compose.unified.yml --profile buzz logs -f buzz-agent-bridge
```
- Container exits immediately with `missing required env var(s)` → set `BUZZ_AGENT_PRIVATE_KEY`, `BUZZ_BRIDGE_API_KEY`, and `BUZZ_KNOWLEDGE_CHANNELS` in `.env`
- `WebSocket error: HTTP error: 404 Not Found` on relay connect → `buzz-relay-proxy` isn't up, or `buzz-agent-bridge`'s `extra_hosts`/`buzz_bridge_net` wiring is missing (see the architecture note above — this is the community-by-Host-header issue)
- `Auth failed: auth-required: verification failed` → same root cause as the 404 above but caught one layer later (the NIP-42 auth event's claimed relay URL doesn't match the server's self-identity); fix is the same
- Relay connects fine but "discovered 0 channel(s)" → the bridge's pubkey (registered as a *relay* member) is not yet a member of the specific *channel* in `BUZZ_KNOWLEDGE_CHANNELS` — see the join/invite step above
- `[reply_adapter] could not parse channel/content from prompt` in the logs → buzz-acp's prompt formatting changed from what `buzz-bridge/reply_adapter.py`'s regexes expect; check `docker compose --profile buzz logs buzz-agent-bridge` for the raw prompt text it captured (truncated in the log line) and update `CHANNEL_RE`/`EVENT_ID_RE`/`CONTENT_RE` to match
- Proxy-side `401`/`503` in the proxy logs → `BUZZ_BRIDGE_API_KEY` must be identical in both the proxy's and the bridge's environment (it's the same `.env` value, so this normally only drifts if one container wasn't restarted after an edit)

**`buzz-admin add-member`/`remove-member` fails with `BUZZ_RELAY_PRIVATE_KEY is required`:**
Set `BUZZ_RELAY_PRIVATE_KEY` in `.env` (generate with `buzz-migrate generate-key`) and restart `buzz` — see the Knowledge Bridge setup steps above. This is the relay's own stable signing key, separate from the bridge's identity.

**`buzz-admin`/`buzz-cli` fails with `no community is configured for this host` (or `RELAY_URL host '...' is not mapped to a community`):**
The relay routes by the exact `Host` header a request arrives with, matched against the single row in the `communities` table (`SELECT host FROM communities;` in `buzz-postgres` to check what it's actually set to — normally `localhost:3010`). `buzz-admin` commands need `RELAY_URL` set to match that string exactly (already wired into the `buzz-migrate` service's environment, so plain `docker compose run buzz-migrate ...` works). For `buzz-cli`, route it through `buzz-relay-proxy` — run it via `docker compose run --rm --entrypoint /usr/local/bin/buzz-cli ... buzz-agent-bridge ...` with `-e BUZZ_RELAY_URL=ws://localhost:3010` (that service already has the `extra_hosts`/`buzz_bridge_net` wiring that makes that address actually resolve to the proxy).

**`buzz-cli channels add-member`/`channels join` fails with `restricted: not a channel member`:**
Only an existing member with sufficient role can add others to a *channel* (separate from relay membership) — neither the bridge's own identity nor the relay's admin key qualify by default. If the channel has an open add-policy, the target identity can `channels join` itself; otherwise an existing owner/admin has to invite it from the Buzz client.

**Attaching an image + @mentioning the bridge gets a reply, but the reply says the ingest failed:**
```bash
docker compose -f docker-compose.unified.yml --profile buzz logs -f buzz-agent-bridge
```
This means the attachment-detection and download worked (`buzz media get` succeeded) — the failure is in the proxy's own image extraction step, same as it would be for any image uploaded via the Knowledge UI. Check `docker compose logs proxy` for the actual vision-backend error (all three backends — the configured `VISION_BACKEND`, then the other two as fallback — are tried in order and the last error wins): an Anthropic/OpenAI key with no credit/quota left, or an `OLLAMA_VISION_MODEL` (default `llava:7b`) that was never pulled (`docker exec <ollama-container> ollama list` to check; `ollama-init` pulls it automatically on a fresh stack, but not retroactively on one that was already running).

**`@bot ingest <name>` replies "not found in MinIO":**
The proxy only searches the bucket root, `inbox/`, `processed/`, `failed/`, and `originals/*/` for an exact filename match. Confirm the object is actually there and spelled the same way: `mc ls --recursive local/documents/`.

**Qdrant search returns 0 results:**
```bash
curl http://localhost:6333/collections/documents
```
Check `points_count`. If 0, ingest documents first.

**Neo4j connection refused:**
Neo4j takes 30–60 s to initialise. Check health:
```bash
docker compose -f docker-compose.unified.yml logs neo4j | tail -20
```

**RAG returns 0 context:**
```bash
docker compose -f docker-compose.unified.yml logs proxy --tail 20
```
- `Qdrant collection 'documents' not found` → ingest at least one document
- `Neo4j: 0 graph records expanded` → graph is empty, ingest documents first
- To disable RAG temporarily: set `ENABLE_RAG=false` in `.env` and restart the proxy

**Grafana shows no data:**
```bash
docker compose -f docker-compose.unified.yml up -d --force-recreate grafana
```
Then verify Prometheus targets at `http://localhost:9090/targets` — `proxy` and `qdrant` should both show `UP`.
