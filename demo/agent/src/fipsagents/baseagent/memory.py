"""Pluggable memory backends for BaseAgent.

Provides a common ``MemoryClientBase`` interface and multiple backend
implementations.  ``NullMemoryClient`` is the silent no-op fallback used
when no backend is configured or any backend fails to initialise.

``create_memory_client`` is the factory entry point.  It accepts an
optional ``MemoryConfig`` object to select a specific backend; without
one it auto-detects by looking for ``.memoryhub.yaml`` (backward compat).

The factory **never** raises — the agent always gets a usable client.

Supported backends:
  - ``memoryhub``   — MemoryHub SDK (auto-detected or explicit)
  - ``markdown``    — Human-readable markdown file(s) (via ``memory_markdown`` module)
  - ``sqlite``      — Local SQLite with FTS5 (via ``memory_sqlite`` module)
  - ``pgvector``    — PostgreSQL + pgvector (via ``memory_pgvector`` module)
  - ``llamastack``  — LlamaStack vector stores API (via ``memory_llamastack`` module)
  - ``custom``      — Any ``MemoryClientBase`` subclass at a dotted import path
  - ``null``        — Explicitly disabled
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fipsagents.baseagent.config import MemoryConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol — the interface both clients satisfy
# ---------------------------------------------------------------------------


class MemoryClientBase:
    """Base class defining the memory client interface.

    Both ``MemoryClient`` and ``NullMemoryClient`` expose these async
    methods so agent code never needs to check which implementation it has.
    """

    @property
    def project_config(self) -> Any | None:
        """Backend-specific project config. Returns None by default."""
        return None

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Search memories by query string."""
        raise NotImplementedError

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        """Write a new memory entry."""
        raise NotImplementedError

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Update an existing memory entry."""
        raise NotImplementedError

    async def report_contradiction(
        self, memory_id: str, description: str
    ) -> None:
        """Report a contradiction against an existing memory."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Null implementation — the "off" state
# ---------------------------------------------------------------------------


class NullMemoryClient(MemoryClientBase):
    """No-op memory client returned when MemoryHub is not configured.

    Every method succeeds silently and returns empty results, so agent
    code can call memory operations without guarding on configuration.
    """

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        return None

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        return None

    async def report_contradiction(
        self, memory_id: str, description: str
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# Real implementation — wraps the MemoryHub SDK
# ---------------------------------------------------------------------------


class MemoryClient(MemoryClientBase):
    """Async wrapper around the MemoryHub SDK.

    Instantiated only when ``.memoryhub.yaml`` exists and the ``memoryhub``
    package is importable.  All methods catch SDK/network errors and degrade
    gracefully (log + return empty) so a flaky MemoryHub server never
    crashes the agent.

    Parameters
    ----------
    sdk:
        An initialised MemoryHub SDK client instance (the object returned
        by ``memoryhub.MemoryHubClient(...)`` or equivalent).
    """

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    @property
    def project_config(self) -> Any | None:
        """The SDK's ProjectConfig, or None for older SDKs."""
        return getattr(self._sdk, "_project_config", None)

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            # SDK v0.5.0 uses .search(); earlier versions used .search_memory()
            _search = getattr(self._sdk, "search", None) or self._sdk.search_memory
            result = await _search(query=query, **kwargs)
            if isinstance(result, list):
                return result
            # SDK v0.5.0 returns SearchResult with .results attribute
            results = getattr(result, "results", None)
            if results is not None:
                # Convert Pydantic models to dicts if needed
                return [
                    r.model_dump() if hasattr(r, "model_dump") else r
                    for r in results
                ]
            # Fallback for older SDK versions
            return getattr(result, "memories", [])
        except Exception:
            logger.warning(
                "MemoryHub search failed for query %r — returning empty results",
                query,
                exc_info=True,
            )
            return []

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            # SDK v0.5.0 uses .write(); earlier versions used .write_memory()
            _write = getattr(self._sdk, "write", None) or self._sdk.write_memory
            result = await _write(content=content, **kwargs)
            if isinstance(result, dict):
                return result
            # SDK v0.5.0 returns WriteResult Pydantic model
            if hasattr(result, "model_dump"):
                return result.model_dump()
            return None
        except Exception:
            logger.warning(
                "MemoryHub write failed — memory not persisted",
                exc_info=True,
            )
            return None

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        try:
            # SDK v0.5.0 uses .update(); earlier versions used .update_memory()
            _update = getattr(self._sdk, "update", None) or self._sdk.update_memory
            result = await _update(memory_id=memory_id, content=content, **kwargs)
            return result if isinstance(result, dict) else None
        except Exception:
            logger.warning(
                "MemoryHub update failed for memory %s — not persisted",
                memory_id,
                exc_info=True,
            )
            return None

    async def report_contradiction(
        self, memory_id: str, description: str
    ) -> None:
        try:
            await self._sdk.report_contradiction(
                memory_id=memory_id, description=description
            )
        except Exception:
            logger.warning(
                "MemoryHub report_contradiction failed for memory %s",
                memory_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_UNSET = object()


async def create_memory_client(
    config_path: str | Path | object = _UNSET,
    *,
    config: MemoryConfig | None = None,
) -> MemoryClientBase:
    """Create the appropriate memory client based on configuration.

    When *config* is provided and ``config.backend`` is set, dispatches
    directly to the named backend.  Otherwise falls back to auto-detection
    via the ``.memoryhub.yaml`` file (backward compatible).

    This function **never** raises — the agent always gets a usable client.

    Parameters
    ----------
    config_path:
        Resolved path to the backend config file.  ``setup()`` resolves
        ``config.config_path`` against the agent's base directory and
        passes the result here.  When omitted, falls back to
        ``config.config_path`` (if *config* is provided) or
        ``.memoryhub.yaml`` (legacy default).
    config:
        Optional ``MemoryConfig`` from ``agent.yaml``.  When present,
        ``config.backend`` drives backend selection.

    Returns
    -------
    MemoryClientBase:
        A live backend client or ``NullMemoryClient``.
    """
    # Resolve effective backend and config path.
    # Priority: explicit positional > config.config_path > legacy default.
    backend = config.backend if config else None
    if config_path is not _UNSET:
        effective_path = Path(config_path)
    elif config is not None:
        effective_path = Path(config.config_path)
    else:
        effective_path = Path(".memoryhub.yaml")

    # Explicit dispatch when backend is set.
    if backend == "null":
        logger.debug("Memory backend explicitly set to 'null' — disabled")
        return NullMemoryClient()

    if backend == "memoryhub":
        return await _create_memoryhub_client(effective_path)

    if backend == "markdown":
        return await _create_markdown_client(effective_path)

    if backend == "sqlite":
        return await _create_sqlite_client(effective_path)

    if backend == "pgvector":
        return await _create_pgvector_client(effective_path)

    if backend == "llamastack":
        return await _create_llamastack_client(effective_path)

    if backend == "custom":
        if not config or not config.backend_class:
            logger.error(
                "Memory backend is 'custom' but no backend_class specified "
                "in memory config"
            )
            return NullMemoryClient()
        return await _create_custom_client(config.backend_class)

    # No explicit backend — auto-detect (backward compat).
    return await _create_memoryhub_client(effective_path)


# ---------------------------------------------------------------------------
# Backend helpers
# ---------------------------------------------------------------------------


async def _create_memoryhub_client(path: Path) -> MemoryClientBase:
    """Create a MemoryHub-backed memory client."""
    if not path.exists():
        logger.debug(
            "No MemoryHub config at %s — memory integration disabled", path
        )
        return NullMemoryClient()

    # Lazy import — memoryhub is an optional dependency.
    try:
        import memoryhub  # noqa: F811
    except ImportError:
        logger.warning(
            "memoryhub package is not installed but .memoryhub.yaml exists at "
            "%s — falling back to NullMemoryClient.  Install with: "
            "pip install memoryhub",
            path,
        )
        return NullMemoryClient()

    try:
        # Expand ${VAR:-default} placeholders in the YAML before parsing.
        # TODO: Remove this shim once the memoryhub SDK reads config through
        # an env-aware loader (upstream feature request — the SDK currently
        # calls yaml.safe_load directly and ignores env var placeholders).
        from fipsagents.baseagent.config import parse_yaml_with_env

        raw = path.read_text(encoding="utf-8")
        hub_config = parse_yaml_with_env(raw) or {}

        # Read the API key from the conventional location if not in config.
        api_key = hub_config.get("api_key")
        if not api_key:
            key_path = Path.home() / ".config" / "memoryhub" / "api-key"
            if key_path.exists():
                api_key = key_path.read_text(encoding="utf-8").strip()

        server_url = hub_config.get("server_url") or hub_config.get("url")

        # Stub-config short-circuit. A scaffolded `.memoryhub.yaml` may exist
        # with only comments / empty body — treat that as "memory not
        # configured" rather than a runtime failure. Without this, the SDK
        # raises MemoryHubError("url is required") and the generic except
        # below logs a full stack trace, which spooks first-time readers.
        if not server_url:
            logger.info(
                "MemoryHub config at %s has no server_url — memory disabled "
                "(set server_url to enable).",
                path,
            )
            return NullMemoryClient()

        # Build the SDK client — exact kwargs depend on the memoryhub SDK.
        sdk_kwargs: dict[str, Any] = {"server_url": server_url}
        if api_key:
            sdk_kwargs["api_key"] = api_key

        sdk = memoryhub.MemoryHubClient(**sdk_kwargs)

        # SDK v0.5.0 registers via __aenter__ (auto-calls register_session).
        # Older SDKs may expose register_session directly.
        if hasattr(sdk, "__aenter__"):
            await sdk.__aenter__()
        elif hasattr(sdk, "register_session"):
            await sdk.register_session(api_key=api_key)

        logger.info("MemoryHub integration enabled (config: %s)", path)
        return MemoryClient(sdk=sdk)

    except Exception:
        logger.warning(
            "Failed to initialise MemoryHub from %s — falling back to "
            "NullMemoryClient.  The agent will run without memory.",
            path,
            exc_info=True,
        )
        return NullMemoryClient()


async def _create_sqlite_client(config_path: Path) -> MemoryClientBase:
    """Create a SQLite-backed memory client."""
    try:
        from fipsagents.baseagent.memory_sqlite import create_sqlite_client

        return await create_sqlite_client(config_path)
    except ImportError:
        logger.error(
            "SQLite memory backend requested but memory_sqlite module "
            "not found — falling back to NullMemoryClient"
        )
        return NullMemoryClient()


async def _create_pgvector_client(config_path: Path) -> MemoryClientBase:
    """Create a PGVector-backed memory client."""
    try:
        from fipsagents.baseagent.memory_pgvector import create_pgvector_client

        return await create_pgvector_client(config_path)
    except ImportError:
        logger.error(
            "PGVector memory backend requested but memory_pgvector module "
            "not found — falling back to NullMemoryClient. "
            "Install with: pip install fipsagents[pgvector]"
        )
        return NullMemoryClient()


async def _create_markdown_client(config_path: Path) -> MemoryClientBase:
    """Create a markdown-backed memory client."""
    try:
        from fipsagents.baseagent.memory_markdown import create_markdown_client

        return await create_markdown_client(config_path)
    except ImportError:
        logger.error(
            "Markdown memory backend requested but memory_markdown module "
            "not found — falling back to NullMemoryClient"
        )
        return NullMemoryClient()


async def _create_llamastack_client(config_path: Path) -> MemoryClientBase:
    """Create a LlamaStack-backed memory client."""
    try:
        from fipsagents.baseagent.memory_llamastack import create_llamastack_client

        return await create_llamastack_client(config_path)
    except ImportError:
        logger.error(
            "LlamaStack memory backend requested but memory_llamastack module "
            "not found — falling back to NullMemoryClient"
        )
        return NullMemoryClient()


async def _create_custom_client(dotted_path: str) -> MemoryClientBase:
    """Import and instantiate a custom MemoryClientBase subclass."""
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if not (isinstance(cls, type) and issubclass(cls, MemoryClientBase)):
            logger.error(
                "Custom memory backend class %s is not a MemoryClientBase "
                "subclass — falling back to NullMemoryClient",
                dotted_path,
            )
            return NullMemoryClient()
        instance = cls()
        # If the custom class has an async setup method, call it.
        if hasattr(instance, "setup") and callable(instance.setup):
            await instance.setup()
        return instance
    except Exception:
        logger.warning(
            "Failed to load custom memory backend from %s — "
            "falling back to NullMemoryClient",
            dotted_path,
            exc_info=True,
        )
        return NullMemoryClient()
