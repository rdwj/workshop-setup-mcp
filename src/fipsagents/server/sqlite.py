"""Shared SQLite connection manager for server-layer stores.

When both sessions and traces use the ``sqlite`` backend, they share a
single ``aiosqlite`` connection per database path.  The connection
manager deduplicates by resolved absolute path and owns the lifecycle
of every connection it creates.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class SqliteConnectionManager:
    """Cache and reuse aiosqlite connections by resolved database path."""

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}

    async def acquire(self, db_path: str) -> Any:
        """Return an existing connection or create a new one.

        *db_path* is resolved to an absolute path so that
        ``"./agent.db"`` and ``"/abs/path/agent.db"`` hit the same
        cache entry.
        """
        resolved = os.path.abspath(db_path)
        if resolved in self._connections:
            return self._connections[resolved]

        # Ensure the parent directory exists. When the chart sets
        # FILES_SQLITE_DB_PATH=<pvc>/.metadata/agent.db, the `.metadata/`
        # subdir won't exist on a freshly-provisioned PVC.
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)

        import aiosqlite

        conn = await aiosqlite.connect(resolved)
        self._connections[resolved] = conn
        logger.debug("SqliteConnectionManager: opened %s", resolved)
        return conn

    async def close_all(self) -> None:
        """Close every managed connection and clear the cache."""
        for path, conn in self._connections.items():
            try:
                await conn.close()
                logger.debug("SqliteConnectionManager: closed %s", path)
            except Exception:
                logger.warning(
                    "SqliteConnectionManager: error closing %s", path, exc_info=True,
                )
        self._connections.clear()
