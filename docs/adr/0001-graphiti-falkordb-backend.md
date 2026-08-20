# ADR 0001: Replace Neo4j and Qdrant with Graphiti and FalkorDB

**Status:** Accepted  
**Date:** 2026-08-20  
**Decision owners:** EedgeAI maintainers

## Context

The previous retrieval path wrote every document to both Qdrant and Neo4j, then joined vector hits back to graph identifiers at query time. That duplicated storage and schema logic, exposed the system to partial writes and identifier drift, and required two databases, two MCP servers, and a Neo4j metrics exporter. The optional Buzz stack added unrelated operational services.

The replacement must retain semantic and structural retrieval, document provenance, agent access through MCP, local operation, and recoverability of existing data.

## Decision drivers

- One authoritative knowledge backend
- Hybrid semantic, keyword, graph, and temporal retrieval
- OpenClaw/MCP compatibility
- Local deployment without a mandatory paid API
- Fewer services and cross-database consistency paths
- Rollback safety for existing volumes

## Options considered

### Graphiti with FalkorDB

Graphiti supports FalkorDB as a first-class driver and supplies temporal episodes, entity/fact extraction, hybrid retrieval, and an HTTP MCP server. FalkorDB stores both graph structure and vector indexes. The trade-off is that ingestion needs a model that reliably emits structured JSON and is more compute-intensive than plain embedding generation.

### Keep Neo4j and Qdrant

This has the lowest migration effort and preserves mature direct APIs, but keeps dual writes, duplicated metadata, two MCP integrations, and operational drift.

### FalkorDB with custom extraction and retrieval

This removes the dual database while preserving full application control, but would require EedgeAI to build and maintain entity extraction, temporal semantics, deduplication, reranking, provenance, and MCP tooling that Graphiti already provides.

## Decision

Use Graphiti 0.22 with FalkorDB as the only active knowledge database. Use Graphiti's REST facade for proxy and ingestion traffic and its HTTP MCP service for OpenClaw. Enable FalkorDB append-only persistence with one-second fsync. Default extraction to local `qwen2.5:7b` and embeddings to `nomic-embed-text` through Ollama. Remove Neo4j, Qdrant, their MCP/exporter services, their runtime client packages, and all Buzz services from the active Compose topology.

## Consequences

Positive consequences are a single source of truth, native temporal knowledge, simpler retrieval, fewer active containers, and no graph/vector join key. Negative consequences are higher ingestion latency, a larger local model requirement, and API behavior changes: collections become Graphiti groups and group rename/archive semantics are not available. Existing Neo4j/Qdrant contents are not automatically transformed.

## Risks and mitigations

- Structured extraction may fail on small models. Default to Qwen 2.5 7B, validate a real write/search during deployment, and permit a hosted OpenAI-compatible override.
- Re-ingestion can be slow. Keep MinIO originals and ingest asynchronously at the workflow boundary.
- Legacy data could be lost during cutover. Preserve the old named volumes and do not prune them until migration acceptance.
- Graphiti's published REST image is Neo4j-specific. Pin its image digest and add a thin tested FalkorDB REST facade over `graphiti-core[falkordb]`.

## Rollback plan

Revert this commit and run the former Compose definition. The retained `neo4j_data`, `qdrant_data`, and Buzz volumes allow the old services to mount their existing data. Do not delete those volumes until the new backend has passed acceptance and any required source documents have been re-ingested.

## Validation plan

1. Validate Compose configuration and build all changed images.
2. Require healthy FalkorDB, Graphiti REST, and Graphiti MCP containers.
3. Add a unique episode through Graphiti and retrieve its fact through hybrid search.
4. Query FalkorDB directly to prove graph nodes were persisted.
5. Exercise proxy health/search and confirm OpenClaw loads the Graphiti MCP configuration.
6. Remove orphaned legacy containers while retaining their volumes.
