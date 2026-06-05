"""Persistent property-graph store backed by Apache AGE.

Implementations:

- :class:`NullGraphStore` — no-op default.  Every method returns the
  zero-value (0, ``None``, ``[]``, ``False``).
- :class:`AgeGraphStore` — production backend.  asyncpg pool with
  per-connection AGE initialisation, Cypher executed via
  ``ag_catalog.cypher()`` SQL wrapper.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    import asyncpg

logger = logging.getLogger(__name__)

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_label(label: str) -> None:
    if not _VALID_IDENTIFIER.match(label):
        raise ValueError(f"invalid label: {label!r}")


def _escape_cypher_value(value: Any) -> str:
    """Serialise a Python primitive to a Cypher literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise ValueError(
        f"unsupported Cypher value type {type(value).__name__}: {value!r}"
    )


def _build_property_string(properties: dict[str, Any] | None) -> str:
    """Build ``{key1: val1, key2: val2}`` Cypher syntax, or empty string."""
    if not properties:
        return ""
    pairs = ", ".join(
        f"{k}: {_escape_cypher_value(v)}" for k, v in properties.items()
    )
    return " {" + pairs + "}"


def _parse_agtype(raw: Any) -> Any:
    """Parse an AGE ``agtype`` value into a Python object.

    AGE appends ``::vertex``, ``::edge``, etc. to composite-type text
    representations.  Strip those before JSON-parsing.
    """
    if raw is None:
        return None
    text = str(raw)
    text = re.sub(r"::(?:vertex|edge|path|numeric)\s*$", "", text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class GraphStore(ABC):
    """Persistent property-graph store backed by Apache AGE."""

    @abstractmethod
    async def add_node(self, label: str, properties: dict[str, Any] | None = None) -> int:
        """Create a vertex.  Return AGE vertex ID."""

    @abstractmethod
    async def add_edge(self, start_id: int, end_id: int, label: str,
                       properties: dict[str, Any] | None = None) -> int:
        """Create a directed edge.  Return AGE edge ID."""

    @abstractmethod
    async def get_node(self, node_id: int) -> dict[str, Any] | None:
        """Return node dict with ``id``, ``label``, ``properties``, or ``None``."""

    @abstractmethod
    async def get_neighbors(self, node_id: int, *, edge_label: str | None = None,
                            direction: Literal["out", "in", "both"] = "both") -> list[dict[str, Any]]:
        """Return list of neighbor dicts."""

    @abstractmethod
    async def query_cypher(self, cypher: str,
                           params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute raw Cypher.  Return result rows as dicts."""

    @abstractmethod
    async def search_nodes(self, label: str,
                           property_filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Find nodes by label + optional property equality."""

    @abstractmethod
    async def delete_node(self, node_id: int) -> bool:
        """DETACH DELETE a node.  Return ``True`` if it existed."""

    @abstractmethod
    async def delete_edge(self, edge_id: int) -> bool:
        """DELETE an edge.  Return ``True`` if it existed."""

    async def close(self) -> None:
        """Release resources.  Default: no-op."""


# ---------------------------------------------------------------------------
# Null implementation
# ---------------------------------------------------------------------------


class NullGraphStore(GraphStore):
    """No-op graph store.  Every method returns the zero-value."""

    async def add_node(self, label: str, properties: dict[str, Any] | None = None) -> int:
        return 0

    async def add_edge(self, start_id: int, end_id: int, label: str,
                       properties: dict[str, Any] | None = None) -> int:
        return 0

    async def get_node(self, node_id: int) -> dict[str, Any] | None:
        return None

    async def get_neighbors(self, node_id: int, *, edge_label: str | None = None,
                            direction: Literal["out", "in", "both"] = "both") -> list[dict[str, Any]]:
        return []

    async def query_cypher(self, cypher: str,
                           params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    async def search_nodes(self, label: str,
                           property_filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    async def delete_node(self, node_id: int) -> bool:
        return False

    async def delete_edge(self, edge_id: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# Apache AGE implementation
# ---------------------------------------------------------------------------


def _node_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "label": str(row[1]),
        "properties": row[2] if isinstance(row[2], dict) else {},
    }


class AgeGraphStore(GraphStore):
    """Apache AGE property-graph store.

    All Cypher goes through :meth:`_cypher` which wraps queries in the
    ``ag_catalog.cypher()`` SQL function.  The pool must have been
    created with an ``init`` callback that runs ``LOAD 'age'`` and sets
    the search path (see :func:`create_age_graph_store`).
    """

    def __init__(self, pool: "asyncpg.Pool", graph_name: str) -> None:
        self._pool = pool
        self._graph = graph_name

    async def _cypher(self, cypher_text: str, column_defs: str) -> list[tuple[Any, ...]]:
        sql = (
            f"SELECT * FROM cypher('{self._graph}', $$\n"
            f"    {cypher_text}\n"
            f"$$) AS ({column_defs});"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [
            tuple(_parse_agtype(col) for col in row.values())
            for row in rows
        ]

    _NODE_COLS = "id agtype, lbl agtype, props agtype"

    # -- writes ------------------------------------------------------------

    async def add_node(self, label: str, properties: dict[str, Any] | None = None) -> int:
        _validate_label(label)
        cypher = f"CREATE (n:{label}{_build_property_string(properties)}) RETURN id(n)"
        try:
            rows = await self._cypher(cypher, "id agtype")
            return int(rows[0][0])
        except Exception:
            logger.warning("AgeGraphStore: add_node failed (label=%s)", label, exc_info=True)
            return 0

    async def add_edge(self, start_id: int, end_id: int, label: str,
                       properties: dict[str, Any] | None = None) -> int:
        _validate_label(label)
        props = _build_property_string(properties)
        cypher = (
            f"MATCH (a), (b) WHERE id(a) = {int(start_id)} AND id(b) = {int(end_id)} "
            f"CREATE (a)-[e:{label}{props}]->(b) RETURN id(e)"
        )
        try:
            rows = await self._cypher(cypher, "id agtype")
            if not rows:
                logger.warning(
                    "AgeGraphStore: add_edge returned no rows — start or end "
                    "node may not exist (start=%d, end=%d, label=%s)",
                    start_id, end_id, label,
                )
                return 0
            return int(rows[0][0])
        except Exception:
            logger.warning(
                "AgeGraphStore: add_edge failed (start=%d, end=%d, label=%s)",
                start_id, end_id, label, exc_info=True,
            )
            return 0

    # -- reads -------------------------------------------------------------

    async def get_node(self, node_id: int) -> dict[str, Any] | None:
        cypher = f"MATCH (n) WHERE id(n) = {int(node_id)} RETURN id(n), label(n), properties(n)"
        try:
            rows = await self._cypher(cypher, self._NODE_COLS)
            return _node_from_row(rows[0]) if rows else None
        except Exception:
            logger.warning("AgeGraphStore: get_node failed (id=%d)", node_id, exc_info=True)
            return None

    async def get_neighbors(self, node_id: int, *, edge_label: str | None = None,
                            direction: Literal["out", "in", "both"] = "both") -> list[dict[str, Any]]:
        if edge_label is not None:
            _validate_label(edge_label)
            edge_spec = f"[e:{edge_label}]"
        else:
            edge_spec = "[e]"

        nid = int(node_id)
        if direction == "out":
            pattern = f"(n)-{edge_spec}->(m)"
        elif direction == "in":
            pattern = f"(n)<-{edge_spec}-(m)"
        else:
            pattern = f"(n)-{edge_spec}-(m)"

        cypher = (
            f"MATCH {pattern} WHERE id(n) = {nid} "
            f"RETURN id(m), label(m), properties(m), id(e), label(e), properties(e)"
        )
        col_defs = "m_id agtype, m_lbl agtype, m_props agtype, e_id agtype, e_lbl agtype, e_props agtype"
        try:
            rows = await self._cypher(cypher, col_defs)
        except Exception:
            logger.warning(
                "AgeGraphStore: get_neighbors failed (id=%d, direction=%s)",
                node_id, direction, exc_info=True,
            )
            return []

        return [
            {
                "id": int(r[0]),
                "label": str(r[1]),
                "properties": r[2] if isinstance(r[2], dict) else {},
                "edge_id": int(r[3]),
                "edge_label": str(r[4]),
                "edge_properties": r[5] if isinstance(r[5], dict) else {},
            }
            for r in rows
        ]

    async def query_cypher(self, cypher: str,
                           params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if params:
            logger.warning(
                "AgeGraphStore: query_cypher params are not yet supported "
                "by Apache AGE — params will be ignored: %s",
                list(params.keys()),
            )
        try:
            rows = await self._cypher(cypher, "result agtype")
            return [{"result": row[0]} for row in rows]
        except Exception:
            logger.warning("AgeGraphStore: query_cypher failed", exc_info=True)
            return []

    async def search_nodes(self, label: str,
                           property_filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        _validate_label(label)
        if property_filter:
            clauses = " AND ".join(
                f"n.{k} = {_escape_cypher_value(v)}" for k, v in property_filter.items()
            )
            where = f" WHERE {clauses}"
        else:
            where = ""
        cypher = f"MATCH (n:{label}){where} RETURN id(n), label(n), properties(n)"
        try:
            rows = await self._cypher(cypher, self._NODE_COLS)
        except Exception:
            logger.warning("AgeGraphStore: search_nodes failed (label=%s)", label, exc_info=True)
            return []
        return [_node_from_row(r) for r in rows]

    # -- deletes -----------------------------------------------------------

    async def delete_node(self, node_id: int) -> bool:
        cypher = f"MATCH (n) WHERE id(n) = {int(node_id)} DETACH DELETE n RETURN true"
        try:
            rows = await self._cypher(cypher, "deleted agtype")
            return len(rows) > 0
        except Exception:
            logger.warning("AgeGraphStore: delete_node failed (id=%d)", node_id, exc_info=True)
            return False

    async def delete_edge(self, edge_id: int) -> bool:
        cypher = f"MATCH ()-[e]->() WHERE id(e) = {int(edge_id)} DELETE e RETURN true"
        try:
            rows = await self._cypher(cypher, "deleted agtype")
            return len(rows) > 0
        except Exception:
            logger.warning("AgeGraphStore: delete_edge failed (id=%d)", edge_id, exc_info=True)
            return False

    async def close(self) -> None:
        try:
            await self._pool.close()
        except Exception:  # pragma: no cover — defensive
            logger.debug("AgeGraphStore: pool close raised", exc_info=True)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


async def initialise_age_schema(pool: "asyncpg.Pool", graph_name: str) -> None:
    """Ensure the AGE extension is installed and *graph_name* exists.

    ``LOAD 'age'`` is handled per-connection via the pool ``init``
    callback, not here.
    """
    if not _VALID_IDENTIFIER.match(graph_name):
        raise ValueError(f"initialise_age_schema: invalid graph_name {graph_name!r}")

    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        row = await conn.fetchrow(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1", graph_name,
        )
        if row is None:
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            await conn.execute(f"SELECT create_graph('{graph_name}')")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def _init_age_connection(conn: "asyncpg.Connection") -> None:
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


async def create_age_graph_store(
    *,
    database_url: str,
    graph_name: str = "agent_knowledge",
) -> GraphStore:
    """Create an :class:`AgeGraphStore` against *database_url*.

    Falls back to :class:`NullGraphStore` on any initialisation error
    so the server always gets a usable store.
    """
    try:
        import asyncpg  # noqa: F811
    except ImportError:
        logger.error(
            "create_age_graph_store: asyncpg not installed. "
            "Install with: pip install fipsagents[graph]",
        )
        return NullGraphStore()

    if not database_url:
        logger.error(
            "create_age_graph_store: database_url is empty — "
            "falling back to NullGraphStore",
        )
        return NullGraphStore()

    if not _VALID_IDENTIFIER.match(graph_name):
        logger.error(
            "create_age_graph_store: invalid graph_name %r — "
            "falling back to NullGraphStore",
            graph_name,
        )
        return NullGraphStore()

    try:
        pool = await asyncpg.create_pool(database_url, init=_init_age_connection)
    except Exception:
        logger.warning(
            "create_age_graph_store: pool creation failed — "
            "falling back to NullGraphStore",
            exc_info=True,
        )
        return NullGraphStore()

    try:
        await initialise_age_schema(pool, graph_name)
    except Exception:
        logger.warning(
            "create_age_graph_store: schema init failed — "
            "falling back to NullGraphStore",
            exc_info=True,
        )
        await pool.close()
        return NullGraphStore()

    logger.info("AgeGraphStore enabled (graph=%s)", graph_name)
    return AgeGraphStore(pool, graph_name)
