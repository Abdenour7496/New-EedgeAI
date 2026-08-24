"""
Data-governance schema for everything the GCOR proxy writes into Graphiti.

Formalizes fields that used to be informal string literals scattered through
main.py (access_level="public", confidence=1.0, valid_to=None, ...) into one
documented, validated place. See
docs/adr/0004-data-governance-schema.md for the full rationale, the
retrieval-time enforcement (main._filter_hits), and known gaps.

Every episode/fact this stack writes to Graphiti carries this classification:

  access_level   "public" | "restricted" | "agent:<agent_id>"
                 Set at ingestion (api_ingest, api_ingest_session), enforced
                 at retrieval (main._filter_hits). "restricted" hides a hit
                 from every retrieval path unconditionally; "agent:<id>"
                 scopes it to the proxy instance whose AGENT_ID matches — see
                 the ADR for what happens when AGENT_ID is unset.
  confidence     float 0.0-1.0. Directly-ingested documents and Graphiti
                 facts are stamped 1.0 (see graphiti_search); nothing in this
                 stack currently ingests below that. Filtered against
                 CONFIDENCE_THRESHOLD at retrieval.
  valid_from /   ISO-8601 UTC timestamps bounding temporal validity. Facts
  valid_to       outside this window are dropped at retrieval regardless of
                 access_level or confidence.
  agent_id       Free-text label for the owning agent. Not itself an access
                 boundary — access_level's "agent:<id>" form is; agent_id is
                 informational provenance stored alongside each episode.

Only access_level is validated at ingestion today (validate_access_level
below) — confidence and the temporal fields are proxy-computed, never taken
directly from a caller, so they don't need input validation the way a
free-text access_level does.
"""

ACCESS_LEVEL_PUBLIC = "public"
ACCESS_LEVEL_RESTRICTED = "restricted"
ACCESS_LEVEL_AGENT_PREFIX = "agent:"
DEFAULT_ACCESS_LEVEL = ACCESS_LEVEL_PUBLIC

# Generous but bounded — this is a partition label, not free text content.
_MAX_AGENT_ID_LENGTH = 128


def validate_access_level(value: str | None) -> str:
    """Normalize and validate an access_level string at ingestion time.

    Returns the canonical value (trimmed; blank/None becomes the default
    "public") or raises ValueError with a message safe to surface directly to
    a caller, e.g. `raise HTTPException(400, detail=str(exc))`.
    """
    level = (value or "").strip() or DEFAULT_ACCESS_LEVEL
    if level in (ACCESS_LEVEL_PUBLIC, ACCESS_LEVEL_RESTRICTED):
        return level
    if level.startswith(ACCESS_LEVEL_AGENT_PREFIX):
        owner = level[len(ACCESS_LEVEL_AGENT_PREFIX):]
        if not owner or len(owner) > _MAX_AGENT_ID_LENGTH:
            raise ValueError(
                f"Invalid access_level '{level}': the agent id after "
                f"'{ACCESS_LEVEL_AGENT_PREFIX}' must be 1-{_MAX_AGENT_ID_LENGTH} characters"
            )
        return level
    raise ValueError(
        f"Invalid access_level '{level}': must be '{ACCESS_LEVEL_PUBLIC}', "
        f"'{ACCESS_LEVEL_RESTRICTED}', or '{ACCESS_LEVEL_AGENT_PREFIX}<agent_id>'"
    )
