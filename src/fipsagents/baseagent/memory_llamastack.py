"""LlamaStack memory backend via OpenAI-compatible vector stores API.

Uses LlamaStack's ``/v1/vector_stores`` endpoints for semantic search and
``/v1/files`` for content ingestion. An agent that already talks to
LlamaStack for inference can store and retrieve memories through the same
endpoint with no additional infrastructure.

Limitations:
  - Search is semantic (vector similarity) — exact-match lookups are not supported
  - No version history on updates (old content is deleted, new content inserted)
  - ``report_contradiction`` is a log-only no-op
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient

logger = logging.getLogger(__name__)


class LlamaStackMemoryClient(MemoryClientBase):
    """LlamaStack-backed memory client using OpenAI-compatible vector stores."""

    def __init__(self, client: httpx.AsyncClient, vector_store_id: str) -> None:
        self._client = client
        self._vector_store_id = vector_store_id

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Search memories via semantic vector similarity.

        Args:
            query: Natural language search query.
            **kwargs: Supports ``max_results`` (default 10).

        Returns:
            List of memory dicts with ``id``, ``content``, and ``score``.
        """
        max_results = int(kwargs.get("max_results", 10))
        try:
            resp = await self._client.post(
                f"/v1/vector_stores/{self._vector_store_id}/search",
                json={"query": query, "max_num_results": max_results},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            results = []
            for entry in data:
                parts = entry.get("content", [])
                text = " ".join(
                    p["text"] for p in parts if p.get("type") == "text" and "text" in p
                )
                results.append(
                    {
                        "id": entry.get("file_id"),
                        "content": text,
                        "score": entry.get("score"),
                    }
                )
            return results
        except Exception:
            logger.warning(
                "LlamaStack search failed for query %r", query, exc_info=True
            )
            return []

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        """Upload content as a file and attach it to the vector store.

        Args:
            content: Text content to persist.
            **kwargs: Unused; accepted for interface compatibility.

        Returns:
            Dict with ``id`` and ``content``, or ``None`` on failure.
        """
        filename = f"memory-{uuid.uuid4()}.txt"
        try:
            file_obj = io.BytesIO(content.encode("utf-8"))
            upload_resp = await self._client.post(
                "/v1/files",
                files={"file": (filename, file_obj, "text/plain")},
                data={"purpose": "assistants"},
            )
            upload_resp.raise_for_status()
            file_id = upload_resp.json()["id"]

            attach_resp = await self._client.post(
                f"/v1/vector_stores/{self._vector_store_id}/files",
                json={"file_id": file_id},
            )
            attach_resp.raise_for_status()

            return {"id": file_id, "content": content}
        except Exception:
            logger.warning(
                "LlamaStack write failed — memory not persisted", exc_info=True
            )
            return None

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Replace a memory entry by deleting the old file and writing a new one.

        Args:
            memory_id: File ID of the memory to replace.
            content: New content.
            **kwargs: Forwarded to :meth:`write`.

        Returns:
            Result from :meth:`write`, or ``None`` on failure.
        """
        try:
            del_resp = await self._client.delete(
                f"/v1/vector_stores/{self._vector_store_id}/files/{memory_id}"
            )
            if del_resp.status_code not in (200, 204, 404):
                del_resp.raise_for_status()
        except Exception:
            logger.warning(
                "LlamaStack update: failed to delete memory %s — will still write replacement",
                memory_id,
                exc_info=True,
            )

        return await self.write(content, **kwargs)

    async def report_contradiction(self, memory_id: str, description: str) -> None:
        logger.warning(
            "Contradiction reported for memory %s: %s "
            "(LlamaStack backend does not track contradictions)",
            memory_id,
            description,
        )


async def create_llamastack_client(config_path: Path) -> MemoryClientBase:
    """Build a ``LlamaStackMemoryClient`` from ``.memory-llamastack.yaml``.

    Config schema::

        endpoint: ${LLAMASTACK_ENDPOINT:-http://localhost:8321}
        vector_store: ${MEMORY_VECTOR_STORE:-agent-memory}
        embedding_model: ${EMBEDDING_MODEL:-all-MiniLM-L6-v2}
        embedding_dimension: 384  # must match the model's output dimension
        api_key: null  # optional

    Finds or creates the named vector store on startup.  Both
    ``embedding_model`` and ``embedding_dimension`` are required by
    LlamaStack when creating a new store (LlamaStack 0.3.x defaults
    to 768 if omitted, which is wrong for most models).  Returns
    ``NullMemoryClient`` on any error so the agent always gets a
    usable client.
    """
    try:
        from fipsagents.baseagent.config import parse_yaml_with_env

        raw = config_path.read_text(encoding="utf-8")
        cfg = parse_yaml_with_env(raw) or {}

        endpoint: str = cfg.get("endpoint", "http://localhost:8321")
        vector_store_name: str = cfg.get("vector_store", "agent-memory")
        embedding_model: str = cfg.get("embedding_model", "all-MiniLM-L6-v2")
        embedding_dimension: int = int(cfg.get("embedding_dimension", 384))
        api_key: str | None = cfg.get("api_key") or None

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = httpx.AsyncClient(
            base_url=endpoint, headers=headers, timeout=30.0
        )

        # Find or create the vector store.
        list_resp = await client.get("/v1/vector_stores")
        list_resp.raise_for_status()
        stores = list_resp.json().get("data", [])
        store_id: str | None = next(
            (s["id"] for s in stores if s.get("name") == vector_store_name), None
        )

        if store_id is None:
            create_resp = await client.post(
                "/v1/vector_stores",
                json={
                    "name": vector_store_name,
                    "embedding_model": embedding_model,
                    "embedding_dimension": embedding_dimension,
                },
            )
            create_resp.raise_for_status()
            store_id = create_resp.json()["id"]
            logger.info(
                "LlamaStack memory backend: created vector store %r (id=%s)",
                vector_store_name,
                store_id,
            )
        else:
            logger.info(
                "LlamaStack memory backend enabled (store: %r, id=%s, endpoint: %s)",
                vector_store_name,
                store_id,
                endpoint,
            )

        return LlamaStackMemoryClient(client=client, vector_store_id=store_id)

    except Exception:
        logger.warning(
            "Failed to initialise LlamaStack memory backend from %s — "
            "falling back to NullMemoryClient",
            config_path,
            exc_info=True,
        )
        return NullMemoryClient()
