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

    llm_config = LLMConfig(
        api_key=api_key,
        model=model,
        small_model=model,
        base_url=base_url,
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
    driver = FalkorDriver(
        host=os.getenv("FALKORDB_HOST", "falkordb"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        password=os.getenv("FALKORDB_PASSWORD") or None,
        database=os.getenv("FALKORDB_DATABASE", "eedgeai"),
    )
    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=reranker,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.graphiti = create_graphiti()
    await app.state.graphiti.build_indices_and_constraints()
    yield
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
    try:
        episode = await EpisodicNode.get_by_uuid(app.state.graphiti.driver, episode_uuid)
        await episode.delete(app.state.graphiti.driver)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True}


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
