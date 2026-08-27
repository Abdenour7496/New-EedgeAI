import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, get_args, get_origin

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from falkordb.asyncio import FalkorDB
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.edges import EntityEdge
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate
from graphiti_core.prompts.dedupe_nodes import NodeResolutions
from graphiti_core.prompts.extract_edges import ExtractedEdges, MissingFacts
from graphiti_core.prompts.extract_nodes import EntitySummary, ExtractedEntities
from graphiti_core.prompts.invalidate_edges import InvalidatedEdges
from graphiti_core.prompts.summarize_nodes import Summary, SummaryDescription

# ── Hotfix: tolerate malformed structured-output responses from small/local
#    LLMs across every graphiti-core 0.22.0 response model ───────────────────
# Every one of these Pydantic models is what graphiti-core asks the LLM to
# fill in as structured JSON (edge_operations.py, node_operations.py, etc.),
# then constructs directly via `Model(**llm_response)` — no fault tolerance
# for a partial response. A local CPU-bound model like qwen2.5:7b sometimes
# omits required keys, or — observed directly during validation — even
# echoes back the JSON *schema* itself (`$defs`/`title` keys) instead of
# actual data. Either way this used to raise a pydantic ValidationError and
# crash the entire ingest (500) instead of degrading gracefully for just that
# one step. Each field's own description already documents its intended
# fallback ("If no duplicate facts are found, default to empty list", "One of
# the provided fact types or DEFAULT") — this patch makes those defaults real
# instead of merely documented. A well-formed LLM response is unaffected;
# verified directly against the installed graphiti-core==0.22.0 package
# before merging (defaults for all 9 models below with fully-empty input, and
# a normal case round-tripping correctly). See
# docs/adr/0003-graphiti-edgeduplicate-hotfix.md.
#
# Not covered: custom user-defined entity/edge types (`response_model=
# entity_type` / `edge_model` in node_operations.py / edge_operations.py) —
# those are runtime-supplied classes, not statically enumerable here. This
# repo doesn't configure custom entity/edge types by default.
_graphiti_hotfix_logger = logging.getLogger("graphiti_hotfix")
logger = logging.getLogger(__name__)

# Per-field overrides where the field's own docstring names a specific
# fallback more precise than the generic type-based default below.
_FIELD_DEFAULT_OVERRIDES = {
    (EdgeDuplicate, "fact_type"): "DEFAULT",
}


def _default_for_annotation(annotation):
    origin = get_origin(annotation)
    if origin is list:
        return []
    if origin is dict:
        return {}
    args = get_args(annotation)
    if origin is not None and type(None) in args:
        return None
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if annotation is str:
        return ""
    return None


def _sanitize_for_model(model_cls, data: dict, path: str, missing: list[str]) -> dict:
    """Recursively default missing/None required fields for one response
    model's raw (pre-validation) data, including nested list[BaseModel]
    fields (e.g. ExtractedEdges.edges: list[Edge]).

    Why recursive: patching a nested model's own __init__ (e.g. Edge)
    does *not* help here — pydantic-core validates list[Edge] items
    straight from the raw dicts via its compiled schema, never calling
    Edge.__init__ for items nested inside an ExtractedEdges(**data) call.
    The only place that can intervene is here, before the outer model's
    own __init__ hands the (now-sanitized) data to pydantic. Verified
    directly against graphiti-core==0.22.0: patching only the 9 top-level
    models below, with no separate patch on Edge/NodeDuplicate/
    ExtractedEntity, is sufficient — this function reaches into them.

    Observed directly during validation, not just anticipated: a local
    model returning `"source_entity_id": null` inside ExtractedEdges.edges
    (present key, explicit None) crashed ingestion even though
    ExtractedEdges was already in the patched-model list — the old
    field-presence check (`if name in data: continue`) didn't treat an
    explicit None on a required field as needing a default. Fixed here by
    checking `data.get(name) is None` instead of just `name in data`.
    """
    data = dict(data)
    for name, field in model_cls.model_fields.items():
        annotation = field.annotation
        args = get_args(annotation)
        nested_model = (
            args[0] if get_origin(annotation) is list and args
            and isinstance(args[0], type) and issubclass(args[0], BaseModel)
            else None
        )
        if nested_model is not None and isinstance(data.get(name), list):
            data[name] = [
                _sanitize_for_model(nested_model, item, f"{path}.{name}[{i}]", missing)
                if isinstance(item, dict) else item
                for i, item in enumerate(data[name])
            ]
        if field.is_required() and data.get(name) is None:
            reason = "null" if name in data else "missing"
            missing.append(f"{path}.{name} ({reason})")
            data[name] = _FIELD_DEFAULT_OVERRIDES.get(
                (model_cls, name), _default_for_annotation(annotation)
            )
    return data


def _make_lenient(model_cls):
    """Patch one graphiti-core response model in place so missing (or
    explicitly null) required fields — including inside nested
    list[BaseModel] fields — get a safe default instead of raising
    ValidationError."""
    original_init = model_cls.__init__

    def _lenient_init(self, **data):
        missing: list[str] = []
        data = _sanitize_for_model(model_cls, data, model_cls.__name__, missing)
        if missing:
            _graphiti_hotfix_logger.warning(
                "LLM response for %s missing/null required field(s) %s — "
                "defaulting instead of failing the ingest.",
                model_cls.__name__, missing,
            )
        original_init(self, **data)

    model_cls.__init__ = _lenient_init


for _model_cls in (
    EdgeDuplicate, ExtractedEntities, EntitySummary, NodeResolutions,
    Summary, SummaryDescription, InvalidatedEdges, ExtractedEdges, MissingFacts,
):
    _make_lenient(_model_cls)


class Message(BaseModel):
    content: str
    uuid: str | None = None
    name: str = ""
    role_type: Literal["user", "assistant", "system"]
    role: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_description: str = ""


class AddMessagesRequest(BaseModel):
    group_id: str
    messages: list[Message]


class SearchQuery(BaseModel):
    group_ids: list[str] | None = None
    query: str
    max_facts: int = 10


def create_graphiti() -> Graphiti:
    api_key = os.getenv("OPENAI_API_KEY", "ollama")
    base_url = os.getenv("OPENAI_BASE_URL", "http://ollama:11434/v1")
    model = os.getenv("MODEL_NAME", "qwen2.5:7b")
    embedding_model = os.getenv("EMBEDDING_MODEL_NAME", "nomic-embed-text")
    embedding_dim = int(os.getenv("EMBEDDING_DIM", "768"))

    # LLM (entity/edge extraction, dedup) can be pointed somewhere different
    # from embeddings — e.g. the openclaw gateway (Codex/Claude Code CLI
    # subscriptions) instead of local Ollama. Verified directly: local
    # qwen2.5:7b failed to finish extracting a real 6-message chat transcript
    # inside a 1800s budget; the same call via openclaw took 8.8s. Embeddings
    # stay on Ollama regardless — fast locally, and openclaw's gateway isn't
    # an embeddings backend. Defaults to the same Ollama config as before if
    # these aren't set, so this is opt-in. See docs/adr/0010-graphiti-openclaw-llm.md.
    llm_api_key = os.getenv("GRAPHITI_LLM_API_KEY", api_key)
    llm_base_url = os.getenv("GRAPHITI_LLM_BASE_URL", base_url)
    llm_model = os.getenv("GRAPHITI_LLM_MODEL", model)

    llm_config = LLMConfig(
        api_key=llm_api_key,
        model=llm_model,
        small_model=llm_model,
        base_url=llm_base_url,
    )
    llm_client = OpenAIGenericClient(config=llm_config)
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=api_key,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            base_url=base_url,
        )
    )
    reranker = OpenAIRerankerClient(client=llm_client, config=llm_config)
    # FalkorDriver constructs its own falkordb.asyncio.FalkorDB client with
    # every connection-pool tuning knob left at library defaults —
    # including health_check_interval=0 (disabled). Found directly: after
    # this container ran for a while under real ingest/search load, fresh
    # content stopped being found by /search (proven correct in isolation —
    # a brand-new Graphiti instance against the same FalkorDB found it
    # immediately), while a container restart fixed it instantly. That
    # points at a connection in the pool going stale and never being
    # detected/replaced, since nothing was ever checking it. Constructing
    # our own client with health checking enabled and reasonable timeouts,
    # then handing it to FalkorDriver via falkor_db= (it uses a
    # caller-provided client as-is instead of building its own) — see
    # docs/adr/0016-falkordb-connection-health.md.
    falkor_client = FalkorDB(
        host=os.getenv("FALKORDB_HOST", "falkordb"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        password=os.getenv("FALKORDB_PASSWORD") or None,
        health_check_interval=int(os.getenv("FALKORDB_HEALTH_CHECK_INTERVAL_SECONDS", "30")),
        socket_timeout=int(os.getenv("FALKORDB_SOCKET_TIMEOUT_SECONDS", "30")),
        socket_keepalive=True,
    )
    driver = FalkorDriver(
        falkor_db=falkor_client,
        database=os.getenv("FALKORDB_DATABASE", "eedgeai"),
    )
    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=reranker,
    )


SEARCH_RESTART_INTERVAL_SECONDS = int(os.getenv("SEARCH_RESTART_INTERVAL_SECONDS", str(15 * 60)))


async def _periodic_self_restart():
    """Mitigation, not a fix, for docs/adr/0017: /search on this app's
    long-lived instance intermittently — and sometimes not so
    intermittently — stopped finding recently-written facts, confirmed
    correct at every other layer (embedding, storage, the raw Cypher
    queries, RRF fusion). Every fix attempted in-process (a fresh Graphiti
    instance per search call, an isolated thread with its own event loop)
    failed to reliably resolve it; only a genuinely separate OS process
    ever did, consistently, in every test. A full container restart is
    the cheapest way to get that same isolation for the app's primary
    instance too — it's fast (health check passes in well under a
    minute) and loses no data (FalkorDB is a separate service on its own
    volume). This trades a small, bounded, and known outage window for an
    unbounded, unpredictable one — the actual bug still needs a proper
    upstream fix or root-cause (likely in graphiti-core's or falkordb-py's
    async client under concurrent load); this just keeps the symptom's
    damage contained in the meantime. Set
    SEARCH_RESTART_INTERVAL_SECONDS=0 to disable.
    """
    if SEARCH_RESTART_INTERVAL_SECONDS <= 0:
        return
    await asyncio.sleep(SEARCH_RESTART_INTERVAL_SECONDS)
    logger.warning(
        "Restarting after %ds uptime (SEARCH_RESTART_INTERVAL_SECONDS) — "
        "see docs/adr/0017-search-connection-workaround.md.",
        SEARCH_RESTART_INTERVAL_SECONDS,
    )
    # os._exit, not sys.exit or a graceful shutdown request: this needs to
    # actually terminate the process (so `restart: unless-stopped` brings
    # up a genuinely fresh one) even if something else is hung.
    os._exit(0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.graphiti = create_graphiti()
    await app.state.graphiti.build_indices_and_constraints()
    restart_task = asyncio.create_task(_periodic_self_restart())
    yield
    restart_task.cancel()
    await app.state.graphiti.close()


app = FastAPI(title="eedgeai Graphiti API", lifespan=lifespan)

INGEST_REQUESTS = Counter(
    "graphiti_ingest_requests_total", "Graphiti ingestion requests", ["status"]
)
INGEST_DURATION = Histogram(
    "graphiti_ingest_duration_seconds", "Graphiti ingestion latency"
)
SEARCH_REQUESTS = Counter(
    "graphiti_search_requests_total", "Graphiti search requests", ["status"]
)
SEARCH_DURATION = Histogram(
    "graphiti_search_duration_seconds", "Graphiti search latency"
)
SEARCH_FACTS = Histogram(
    "graphiti_search_facts", "Facts returned by Graphiti search",
    buckets=[0, 1, 2, 3, 5, 8, 10, 20, 50],
)


@app.get("/healthcheck")
async def healthcheck():
    return {"status": "healthy", "backend": "falkordb"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/messages", status_code=201)
async def add_messages(request: AddMessagesRequest):
    async def ingest(message: Message):
        await app.state.graphiti.add_episode(
            group_id=request.group_id,
            name=message.name,
            episode_body=f"{message.role or ''}({message.role_type}): {message.content}",
            reference_time=message.timestamp,
            source=EpisodeType.message,
            source_description=message.source_description,
        )

    async def ingest_all():
        for message in request.messages:
            await ingest(message)

    started = time.perf_counter()
    try:
        await ingest_all()
        INGEST_REQUESTS.labels(status="success").inc()
    except Exception:
        INGEST_REQUESTS.labels(status="error").inc()
        raise
    finally:
        INGEST_DURATION.observe(time.perf_counter() - started)
    return {"success": True, "message": "Messages ingested"}


@app.post("/search")
async def search(request: SearchQuery):
    # NOTE: tried several in-process fixes for a real, confirmed bug here
    # (fresh Graphiti instance per call; an isolated thread with its own
    # event loop) — neither reliably worked. Only a genuinely separate OS
    # process ever reliably found recently-written facts in testing. See
    # docs/adr/0017-search-connection-workaround.md for the full
    # investigation and why this reverted to the original simple form
    # rather than keep unproven complexity that added overhead for no
    # measured benefit. Mitigated instead via a scheduled self-restart
    # (main() below) that bounds how long any staleness window can last.
    started = time.perf_counter()
    try:
        edges = await app.state.graphiti.search(
            group_ids=request.group_ids,
            query=request.query,
            num_results=request.max_facts,
        )
        SEARCH_REQUESTS.labels(status="success").inc()
        SEARCH_FACTS.observe(len(edges))
    except Exception:
        SEARCH_REQUESTS.labels(status="error").inc()
        raise
    finally:
        SEARCH_DURATION.observe(time.perf_counter() - started)
    return {"facts": [
        {
            "uuid": edge.uuid,
            "name": edge.name,
            "fact": edge.fact,
            "valid_at": edge.valid_at,
            "invalid_at": edge.invalid_at,
            "created_at": edge.created_at,
            "expired_at": edge.expired_at,
            "source_node_uuid": edge.source_node_uuid,
            "target_node_uuid": edge.target_node_uuid,
            "episodes": edge.episodes or [],
        }
        for edge in edges
    ]}


@app.get("/episodes/{group_id}")
async def get_episodes(group_id: str, last_n: int = 100):
    episodes = await app.state.graphiti.retrieve_episodes(
        group_ids=[group_id],
        last_n=min(last_n, 500),
        reference_time=datetime.now(timezone.utc),
    )
    return [episode.model_dump(mode="json") for episode in episodes]


@app.delete("/episode/{episode_uuid}")
async def delete_episode(episode_uuid: str):
    """Delete one episode AND the facts (EntityEdge) it's the sole source
    of. Previously deleted only the episode node — the edges Graphiti
    extracted from it survived untouched and stayed fully searchable
    forever, permanently orphaned from any source. Found directly: a test
    document deleted via this endpoint (200, "success": true) still had
    its extracted facts showing up in unrelated /api/search results with
    real graphiti_edge_uuids, long after its episode was gone from
    /episodes/{group}'s listing. See docs/adr/0015.

    An edge can be corroborated by multiple episodes (EntityEdge.episodes)
    — e.g. the same fact re-confirmed across several ingested documents.
    Deleting the whole edge over one contributing episode being removed
    would destroy evidence still backed by the others still present, so:
    strip just this episode's uuid from an edge's `episodes` list if
    others remain; only delete the edge outright if this was its sole
    source.
    """
    try:
        episode = await EpisodicNode.get_by_uuid(app.state.graphiti.driver, episode_uuid)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    edges = await EntityEdge.get_by_group_ids(app.state.graphiti.driver, [episode.group_id])
    edges_deleted = 0
    edges_updated = 0
    for edge in edges:
        if episode_uuid not in (edge.episodes or []):
            continue
        remaining = [e for e in edge.episodes if e != episode_uuid]
        if remaining:
            edge.episodes = remaining
            await edge.save(app.state.graphiti.driver)
            edges_updated += 1
        else:
            await edge.delete(app.state.graphiti.driver)
            edges_deleted += 1

    await episode.delete(app.state.graphiti.driver)
    return {"success": True, "edges_deleted": edges_deleted, "edges_updated": edges_updated}


@app.post("/admin/gc-orphaned-edges/{group_id}")
async def gc_orphaned_edges(group_id: str):
    """One-time (or periodic) cleanup for edges left orphaned by
    delete_episode()'s bug prior to this fix — it used to delete only the
    episode node, leaving every edge it produced permanently searchable
    with no surviving source. Any deployment that ever called single-doc
    delete before this fix landed has this debt; this endpoint pays it
    off without needing to know which episode_uuids were ever deleted.

    For every edge in the group: keep only the episode uuids that still
    exist; delete the edge if none remain, update it if the list changed
    but isn't empty, leave it untouched otherwise. Safe to run repeatedly
    — a clean group does no writes.
    """
    edges = await EntityEdge.get_by_group_ids(app.state.graphiti.driver, [group_id])
    episodes = await EpisodicNode.get_by_group_ids(app.state.graphiti.driver, [group_id])
    live_episode_uuids = {e.uuid for e in episodes}

    edges_deleted = 0
    edges_updated = 0
    edges_scanned = len(edges)
    for edge in edges:
        current = edge.episodes or []
        remaining = [e for e in current if e in live_episode_uuids]
        if len(remaining) == len(current):
            continue  # nothing orphaned on this edge
        if remaining:
            edge.episodes = remaining
            await edge.save(app.state.graphiti.driver)
            edges_updated += 1
        else:
            await edge.delete(app.state.graphiti.driver)
            edges_deleted += 1

    return {
        "success": True, "group_id": group_id,
        "edges_scanned": edges_scanned,
        "edges_deleted": edges_deleted, "edges_updated": edges_updated,
    }


@app.delete("/group/{group_id}")
async def delete_group(group_id: str):
    episodes = await EpisodicNode.get_by_group_ids(app.state.graphiti.driver, [group_id])
    edges = await EntityEdge.get_by_group_ids(app.state.graphiti.driver, [group_id])
    for edge in edges:
        await edge.delete(app.state.graphiti.driver)
    for episode in episodes:
        await episode.delete(app.state.graphiti.driver)
    # Edges and episodes only capture relationships and source text — the
    # entity nodes they reference are separate records and survive both
    # loops above unless removed explicitly, leaving orphaned nodes behind.
    await EntityNode.delete_by_group_id(app.state.graphiti.driver, group_id)
    return {"success": True}
