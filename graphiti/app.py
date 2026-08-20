import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

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
from graphiti_core.nodes import EpisodeType, EpisodicNode


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
    return {"success": True}
