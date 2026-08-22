"""
title: GCOR Collection Scope
author: EedgeAI
version: 1.0.0
required_open_webui_version: 0.10.0
"""

import json
from typing import Optional
from urllib.request import urlopen

from pydantic import BaseModel, Field


class Filter:
    """Attach one configured backend collection to OpenWebUI chat requests."""

    class Valves(BaseModel):
        collection: str = Field(
            default="documents",
            description="Graphiti group used for this workspace or model.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": "get_collection_options",
                }
            },
        )
        priority: int = Field(
            default=0,
            description="Filter execution priority.",
        )

        @classmethod
        def get_collection_options(cls) -> list[dict[str, str]]:
            """List active Graphiti groups from the GCOR proxy for the valve dropdown."""
            try:
                with urlopen("http://proxy:5001/api/collections", timeout=3) as response:
                    payload = json.load(response)
                names = sorted(
                    collection["name"]
                    for collection in payload.get("collections", [])
                    if collection.get("name") and not collection["name"].startswith("_")
                )
                return [{"value": name, "label": name} for name in names]
            except Exception:
                return [{"value": "documents", "label": "documents"}]

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self, body: dict, __user__: Optional[dict] = None
    ) -> dict:
        collection = self.valves.collection.strip()
        if collection:
            body["gcor_collection"] = collection
        return body