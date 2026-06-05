"""OpenAIChatServer — FastAPI server wrapping a BaseAgent subclass."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

try:
    from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError as exc:  # pragma: no cover — helpful error path
    raise ImportError(
        "fipsagents.server requires the [server] extra. "
        "Install with: pip install 'fipsagents[server]'"
    ) from exc

from fipsagents.baseagent import BaseAgent
from fipsagents.baseagent.events import ContentDelta, StreamComplete, StreamMetrics
from fipsagents.serialization.openai_sse import stream_events_as_sse

from .models import (
    ChatCompletionRequest,
    CreateFeedbackRequest,
    CreateSessionRequest,
    ForkSessionRequest,
    ForkSessionResponse,
    RevertSessionRequest,
    UpdateFeedbackRequest,
    _SESSION_ID_RE,
    _extract_overrides,
    _messages_to_dicts,
    _sync_response,
)
from .budget import BudgetExceededError, create_budget_enforcer
from .collector import TraceCollector
from .metrics import NullMetricsCollector, create_metrics_collector
from .sessions import SessionStore, create_session_store
from .tracing import NullTraceStore, TraceStore, create_trace_store
from .feedback import (
    FeedbackRecord,
    FeedbackStore,
    _generate_feedback_id,
    _utc_now_iso,
    create_feedback_store,
)
from .files import (
    FileRecord,
    FileStore,
    _generate_file_id,
    _sha256,
    create_file_store,
    detect_mime,
)
from .parser import FileParser, create_parser
from .scanner import VirusScanner, create_scanner
from .chunker import Chunker, count_tokens, create_chunker
from .chunk_store import (
    ChunkStore,
    NullChunkStore,
    create_pgvector_chunk_store,
)
from .graph_store import (
    GraphStore,
    NullGraphStore,
    create_age_graph_store,
)

logger = logging.getLogger(__name__)


def _new_trace_id() -> str:
    """Generate a trace identifier matching ``TraceCollector``'s format."""
    return f"trace_{uuid.uuid4().hex[:16]}"


class _HttpStoreContextMiddleware(BaseHTTPMiddleware):
    """Forward inbound Authorization + traceparent to outgoing Http*Store calls.

    Stores are stateless objects that don't see request scope.  This
    middleware captures the headers into contextvars consumed by
    :class:`fipsagents.server.http.\\_PlatformClient` so per-request
    JWTs and distributed-trace context flow through to the platform
    service without changing the SessionStore/TraceStore/FeedbackStore
    ABCs.
    """

    async def dispatch(self, request, call_next):
        # Lazy import: only matters when the http backend is configured.
        from .http import set_request_context, reset_request_context

        tokens = set_request_context(
            authorization=request.headers.get("authorization")
            or request.headers.get("Authorization"),
            traceparent=request.headers.get("traceparent"),
        )
        try:
            return await call_next(request)
        finally:
            reset_request_context(tokens)


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------


class OpenAIChatServer:
    """FastAPI server exposing OpenAI-compatible chat completions.

    Wraps any :class:`~fipsagents.baseagent.BaseAgent` subclass, owning the
    agent lifecycle from startup to shutdown. The agent class is instantiated
    once at application start — all requests share a single agent instance,
    serialised through ``_agent_lock``.

    Args:
        agent_class: A :class:`BaseAgent` subclass (pass the class, not an
            instance). The server instantiates it with ``config_path`` and
            ``base_dir`` at startup.
        config_path: Path to the agent YAML config file.
        base_dir: Optional base directory for relative paths inside the agent
            config. Defaults to the config file's parent directory.
        title: FastAPI application title. Defaults to ``agent_class.__name__``.
        version: FastAPI application version string.
    """

    def __init__(
        self,
        agent_class: type[BaseAgent],
        config_path: str | Path = "agent.yaml",
        *,
        base_dir: str | Path | None = None,
        title: str | None = None,
        version: str = "0.1.0",
    ) -> None:
        self._agent_class = agent_class
        self._config_path = Path(config_path)
        self._base_dir = Path(base_dir) if base_dir is not None else None

        self._agent: BaseAgent | None = None
        self._agent_lock = asyncio.Lock()
        self._session_store: SessionStore | None = None
        self._trace_store: TraceStore | None = None
        self._feedback_store: FeedbackStore | None = None
        self._file_store: FileStore | None = None
        self._bytes_store: Any = None  # BytesStore — owned by us, closed at shutdown
        self._file_parser: FileParser | None = None
        self._virus_scanner: VirusScanner | None = None
        self._chunker: Chunker | None = None
        self._chunk_store: ChunkStore | None = None
        self._graph_store: GraphStore | None = None
        self._work_item_store: Any = None
        self._lease_expiry_task: asyncio.Task[None] | None = None
        self._chunking_tasks: set[asyncio.Task] = set()
        self._metrics_collector: Any = None  # Set in lifespan
        self._budget_enforcer: Any = None  # Set in lifespan
        self._compactor: Any = None  # Set in lifespan
        self._permission_source: Any = None  # Set in lifespan
        self._housekeeping_task: asyncio.Task | None = None
        self._sqlite_mgr: Any = None
        self._event_sources: list[Any] = []
        self._event_sink: Any = None
        self._event_tasks: list[asyncio.Task] = []
        self._state_recovery_cfg: Any = None

        app_title = title if title is not None else agent_class.__name__
        self.app = FastAPI(
            title=app_title,
            version=version,
            lifespan=self._lifespan,
        )
        self.app.add_middleware(_HttpStoreContextMiddleware)
        self._register_routes()

    # -- Lifespan ------------------------------------------------------------

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):  # noqa: ARG002
        self._agent = self._agent_class(
            config_path=self._config_path,
            base_dir=self._base_dir,
        )
        await self._agent.setup()

        # Initialize session and trace stores from config.
        server_cfg = self._agent.config.server
        sqlite_conn = None

        # Per-store backend = explicit override or fall through to
        # storage.backend.  An ``http`` backend is allowed at the per-store
        # level even when storage.backend is None or ``sqlite`` — that's
        # the whole point of the per-store override (eg feedback->http,
        # sessions->sqlite).
        sessions_backend = (
            server_cfg.sessions.backend or server_cfg.storage.backend
            if server_cfg.sessions.enabled else None
        )
        traces_backend = (
            server_cfg.traces.backend or server_cfg.storage.backend
            if server_cfg.traces.enabled else None
        )
        feedback_backend = (
            server_cfg.feedback.backend or server_cfg.storage.backend
            if server_cfg.feedback.enabled else None
        )
        files_backend = (
            server_cfg.files.backend or server_cfg.storage.backend
            if server_cfg.files.enabled else None
        )

        work_items_cfg = getattr(server_cfg, "work_items", None)
        work_items_backend = (
            work_items_cfg.backend or server_cfg.storage.backend
            if work_items_cfg is not None and work_items_cfg.enabled else None
        )

        has_sqlite_feature = "sqlite" in {
            sessions_backend, traces_backend, feedback_backend, files_backend,
            work_items_backend,
        }
        if has_sqlite_feature:
            from .sqlite import SqliteConnectionManager

            self._sqlite_mgr = SqliteConnectionManager()
            sqlite_conn = await self._sqlite_mgr.acquire(
                server_cfg.storage.sqlite_path,
            )

        self._session_store = create_session_store(
            sessions_backend,
            sqlite_path=server_cfg.storage.sqlite_path,
            database_url=server_cfg.storage.database_url,
            sqlite_connection=sqlite_conn,
            platform_url=server_cfg.storage.platform_url,
            platform_token=server_cfg.storage.platform_token,
        )
        self._trace_store = create_trace_store(
            traces_backend,
            sqlite_path=server_cfg.storage.sqlite_path,
            database_url=server_cfg.storage.database_url,
            sqlite_connection=sqlite_conn,
            exporter=server_cfg.traces.exporter,
            otel_endpoint=server_cfg.traces.otel_endpoint,
            service_name=server_cfg.traces.service_name,
            platform_url=server_cfg.storage.platform_url,
            platform_token=server_cfg.storage.platform_token,
        )
        self._feedback_store = create_feedback_store(
            feedback_backend,
            sqlite_path=server_cfg.storage.sqlite_path,
            database_url=server_cfg.storage.database_url,
            sqlite_connection=sqlite_conn,
            platform_url=server_cfg.storage.platform_url,
            platform_token=server_cfg.storage.platform_token,
        )
        files_sqlite_path = (
            server_cfg.files.sqlite_path
            or server_cfg.storage.sqlite_path
        )
        # When files override storage.sqlite_path, acquire a separate
        # connection from the manager (it dedupes by resolved path, so
        # passing the unchanged storage path falls back to sqlite_conn).
        files_sqlite_conn = sqlite_conn
        if (
            files_backend == "sqlite"
            and server_cfg.files.sqlite_path
            and self._sqlite_mgr is not None
        ):
            files_sqlite_conn = await self._sqlite_mgr.acquire(files_sqlite_path)
        # Build the BytesStore from bytes_backend config (per ADR-0001).
        # Default (type=local_fs) keeps 0.16.0 deployments working.
        from .bytes_store import create_bytes_store
        bytes_store = create_bytes_store(
            server_cfg.files.bytes_backend.type,
            bytes_dir=server_cfg.files.bytes_dir,
            s3_bucket=server_cfg.files.bytes_backend.bucket,
            s3_endpoint=(
                server_cfg.files.bytes_backend.endpoint or None
            ),
            s3_region=server_cfg.files.bytes_backend.region,
            s3_access_key=(
                server_cfg.files.bytes_backend.access_key or None
            ),
            s3_secret_key=(
                server_cfg.files.bytes_backend.secret_key or None
            ),
            s3_prefix=server_cfg.files.bytes_backend.prefix,
            s3_path_style=server_cfg.files.bytes_backend.path_style,
        )
        # Hold a reference so we can close the bytes store at shutdown.
        # FileStore.close() only closes BytesStore when it owns the
        # lifecycle (i.e. was synthesized internally); when we inject
        # one, ownership stays with the caller.
        self._bytes_store = bytes_store
        self._file_store = create_file_store(
            files_backend,
            sqlite_path=files_sqlite_path,
            database_url=server_cfg.storage.database_url,
            bytes_dir=server_cfg.files.bytes_dir,
            bytes_store=bytes_store,
            sqlite_connection=files_sqlite_conn,
        )
        self._file_parser = create_parser(
            enabled=server_cfg.files.enabled,
            parser_config=server_cfg.files.parser,
        )
        self._virus_scanner = create_scanner(
            url=server_cfg.files.scanner.url,
            timeout_seconds=server_cfg.files.scanner.timeout_seconds,
        )

        # Chunking layer (ADR-0002). Always allocate a chunker (cheap)
        # so the upload path can compute token counts; the chunk_store
        # only does real work when chunking.enabled and backend=pgvector.
        chunking_cfg = server_cfg.files.chunking
        self._chunker = create_chunker(
            enabled=server_cfg.files.enabled and chunking_cfg.enabled,
        )
        if (
            server_cfg.files.enabled
            and chunking_cfg.enabled
            and chunking_cfg.backend == "pgvector"
        ):
            self._chunk_store = await create_pgvector_chunk_store(
                database_url=(
                    chunking_cfg.database_url
                    or server_cfg.storage.database_url
                ),
                embedding_url=chunking_cfg.embedding_url,
                embedding_model=chunking_cfg.embedding_model,
                embedding_dimension=chunking_cfg.embedding_dimension,
                table_name=chunking_cfg.table_name,
            )
        else:
            self._chunk_store = NullChunkStore()

        # Graph store (Apache AGE).
        graph_cfg = server_cfg.graph
        if graph_cfg.enabled and graph_cfg.backend == "age":
            self._graph_store = await create_age_graph_store(
                database_url=(
                    graph_cfg.database_url
                    or server_cfg.storage.database_url
                ),
                graph_name=graph_cfg.graph_name,
            )
        else:
            self._graph_store = NullGraphStore()

        # Work-item store.
        if work_items_cfg is not None and work_items_cfg.enabled:
            from .work_items import create_work_item_store
            self._work_item_store = create_work_item_store(
                work_items_backend,
                sqlite_path=server_cfg.storage.sqlite_path,
                sqlite_connection=sqlite_conn,
                database_url=server_cfg.storage.database_url,
            )
            self._lease_expiry_task = asyncio.create_task(
                self._run_lease_expiry(
                    work_items_cfg.expire_check_interval_seconds,
                ),
            )
            logger.info(
                "WorkItemStore initialized (backend=%s)", work_items_backend,
            )

        # Initialize metrics collector.
        self._metrics_collector = create_metrics_collector(
            enabled=server_cfg.metrics.enabled,
            token_label_mode=server_cfg.metrics.token_label_mode,
        )

        # Initialize budget enforcer.  No-op when no limits are configured.
        self._budget_enforcer = create_budget_enforcer(
            self._agent.config.budget,
            pricing=self._agent.config.pricing,
            session_store=self._session_store,
        )

        # Initialize compactor.
        from .compactor import create_compactor
        compaction_cfg = getattr(server_cfg, "compaction", None)
        if (
            compaction_cfg is not None
            and compaction_cfg.enabled
            and compaction_cfg.backend == "llm"
        ):
            summary_model = (
                compaction_cfg.summary_model or self._agent.config.model.name
            )
            summary_endpoint = self._agent.config.model.endpoint

            async def _compaction_model_fn(messages: list[dict]) -> str:
                import openai as _oai
                client = _oai.AsyncOpenAI(
                    base_url=summary_endpoint or None,
                    api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
                )
                try:
                    resp = await client.chat.completions.create(
                        model=summary_model,
                        messages=messages,
                        temperature=0.3,
                        max_tokens=2000,
                    )
                    return resp.choices[0].message.content or ""
                finally:
                    await client.close()

            self._compactor = create_compactor(
                compaction_cfg.backend,
                model_fn=_compaction_model_fn,
                threshold_messages=compaction_cfg.threshold_messages,
                keep_recent_turns=compaction_cfg.keep_recent_turns,
                summary_role=compaction_cfg.summary_role,
                context_limit=compaction_cfg.context_limit,
                reserve_tokens=compaction_cfg.reserve_tokens,
            )
        else:
            self._compactor = create_compactor(None)

        # Initialize permission source.
        from .permissions import create_permission_source
        perm_cfg = getattr(server_cfg, "permissions", None)
        if perm_cfg is not None:
            self._permission_source = create_permission_source(
                perm_cfg.source,
                rules=[r.model_dump() for r in perm_cfg.rules],
                default_action=perm_cfg.default_action,
            )
        else:
            self._permission_source = create_permission_source(None)

        # Initialize event sources and sink.
        from .events import create_event_source, create_event_sink
        event_src_configs = getattr(server_cfg, "event_sources", None) or []
        event_sink_config = getattr(server_cfg, "event_sink", None)

        if event_sink_config is not None:
            self._event_sink = create_event_sink(event_sink_config)
            await self._event_sink.setup()
        else:
            from .sinks.null import NullSink
            self._event_sink = NullSink()

        for src_cfg in event_src_configs:
            source = create_event_source(src_cfg)
            await source.setup(app=self.app)
            self._event_sources.append(source)
            task = asyncio.create_task(
                self._event_loop(source, self._event_sink),
                name=f"event_loop_{source.source_id}",
            )
            self._event_tasks.append(task)

        if self._event_sources:
            logger.info(
                "Event sources started: %s",
                ", ".join(s.source_id for s in self._event_sources),
            )

        # State recovery config.
        recovery_cfg = getattr(server_cfg, "state_recovery", None)
        if (
            recovery_cfg is not None
            and recovery_cfg.enabled
            and getattr(self._agent, "state_type", None) is not None
        ):
            self._state_recovery_cfg = recovery_cfg
            if not getattr(server_cfg, "traces", None) or not server_cfg.traces.enabled:
                logger.warning(
                    "state_recovery.enabled but traces are disabled; "
                    "replay from event log not possible"
                )
            logger.info("State recovery enabled for %s", self._agent_class.__name__)

        # Run housekeeping only if at least one *locally persistent*
        # backend is in play.  HTTP-backed stores delegate housekeeping
        # to the platform service.
        local_backends = {
            sessions_backend, traces_backend, feedback_backend, files_backend,
        } & {"sqlite", "postgres"}
        if local_backends:
            self._housekeeping_task = asyncio.create_task(self._run_housekeeping())

        logger.info("OpenAIChatServer: %s ready", self._agent_class.__name__)
        try:
            yield
        finally:
            if self._housekeeping_task:
                self._housekeeping_task.cancel()
                try:
                    await self._housekeeping_task
                except asyncio.CancelledError:
                    pass
            # Cancel and drain event tasks.
            for task in self._event_tasks:
                task.cancel()
            if self._event_tasks:
                await asyncio.gather(*self._event_tasks, return_exceptions=True)
            self._event_tasks.clear()
            for source in self._event_sources:
                await source.close()
            self._event_sources.clear()
            if self._event_sink is not None:
                await self._event_sink.close()
            await self._agent.shutdown()
            # Drain any in-flight async chunking tasks before tearing
            # down the stores they're writing to.
            if self._chunking_tasks:
                pending = list(self._chunking_tasks)
                self._chunking_tasks.clear()
                await asyncio.gather(*pending, return_exceptions=True)
            await self._session_store.close()
            await self._trace_store.close()
            await self._feedback_store.close()
            await self._file_store.close()
            if self._chunk_store is not None:
                await self._chunk_store.close()
            if self._graph_store is not None:
                await self._graph_store.close()
            if self._lease_expiry_task is not None:
                self._lease_expiry_task.cancel()
                try:
                    await self._lease_expiry_task
                except asyncio.CancelledError:
                    pass
            if self._work_item_store is not None:
                await self._work_item_store.close()
            if self._bytes_store is not None:
                await self._bytes_store.close()
            await self._virus_scanner.close()
            if self._compactor is not None:
                await self._compactor.close()
            if self._permission_source is not None:
                await self._permission_source.close()
            if self._sqlite_mgr:
                await self._sqlite_mgr.close_all()
            self._agent = None

    # -- Housekeeping --------------------------------------------------------

    async def _run_housekeeping(self, interval_seconds: int = 3600) -> None:
        """Periodically clean up expired sessions and traces."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self._do_housekeeping()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Housekeeping error", exc_info=True)

    async def _run_lease_expiry(self, interval: int = 60) -> None:
        """Periodically expire stale work-item leases."""
        while True:
            await asyncio.sleep(interval)
            try:
                if self._work_item_store is not None:
                    expired = await self._work_item_store.expire_leases()
                    if expired:
                        logger.info(
                            "Expired %d work-item leases", len(expired),
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning(
                    "Lease expiry sweep failed", exc_info=True,
                )

    async def _do_housekeeping(self) -> None:
        """Run one housekeeping pass."""
        from datetime import datetime, timedelta, timezone

        if self._agent is None:
            return

        server_cfg = self._agent.config.server

        if server_cfg.sessions.max_age_hours > 0 and self._session_store:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=server_cfg.sessions.max_age_hours,
            )
            deleted = await self._session_store.delete_before(cutoff)
            if deleted:
                logger.info("Housekeeping: removed %d expired sessions", deleted)

        if server_cfg.traces.max_age_hours > 0 and self._trace_store:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=server_cfg.traces.max_age_hours,
            )
            deleted = await self._trace_store.delete_before(cutoff)
            if deleted:
                logger.info("Housekeeping: removed %d expired traces", deleted)

        if server_cfg.feedback.max_age_hours > 0 and self._feedback_store:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=server_cfg.feedback.max_age_hours,
            )
            deleted = await self._feedback_store.delete_before(cutoff)
            if deleted:
                logger.info("Housekeeping: removed %d expired feedback records", deleted)

        if server_cfg.files.max_age_hours > 0 and self._file_store:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=server_cfg.files.max_age_hours,
            )
            deleted = await self._file_store.delete_before(cutoff)
            if deleted:
                logger.info("Housekeeping: removed %d expired files", deleted)

    # -- Route registration --------------------------------------------------

    def _register_routes(self) -> None:
        self.app.api_route("/healthz", methods=["GET", "HEAD"])(self._healthz)
        self.app.api_route("/readyz", methods=["GET", "HEAD"])(self._readyz)
        self.app.get("/v1/agent-info")(self._agent_info)
        self.app.post("/v1/sessions")(self._create_session)
        self.app.get("/v1/sessions/{session_id}")(self._get_session)
        self.app.get("/v1/sessions/{session_id}/usage")(self._get_session_usage)
        self.app.delete("/v1/sessions/{session_id}")(self._delete_session)
        self.app.post("/v1/sessions/{session_id}/fork")(self._fork_session)
        self.app.post(
            "/v1/sessions/{session_id}/revert", status_code=204,
        )(self._revert_session)
        self.app.get("/v1/traces")(self._list_traces)
        self.app.get("/v1/traces/{trace_id}")(self._get_trace)
        self.app.post("/v1/feedback")(self._create_feedback)
        self.app.patch("/v1/feedback/{feedback_id}")(self._update_feedback)
        self.app.get("/v1/feedback/stats")(self._feedback_stats)
        self.app.get("/v1/feedback")(self._list_feedback)
        self.app.post("/v1/files")(self._upload_file)
        self.app.get("/v1/files")(self._list_files)
        self.app.get("/v1/files/{file_id}")(self._get_file)
        self.app.delete("/v1/files/{file_id}")(self._delete_file)
        self.app.post("/v1/chat/completions")(self._chat_completions)
        self.app.get("/metrics")(self._metrics_endpoint)

        # Work-item management endpoints (separate module).
        from .work_item_routes import register_work_item_routes
        register_work_item_routes(self.app, lambda: self._work_item_store)

        # Trust/scoreboard endpoints (separate module).
        from .trust_routes import register_trust_routes
        register_trust_routes(self.app, lambda: self._agent)

    # -- Endpoint handlers ---------------------------------------------------

    async def _healthz(self) -> dict[str, str]:
        return {"status": "ok"}

    async def _readyz(self):
        if self._agent is None:
            return JSONResponse({"status": "not ready"}, status_code=503)
        return {"status": "ready"}

    async def _agent_info(self):
        if self._agent is None:
            raise HTTPException(status_code=503, detail="Agent not ready")

        agent = self._agent

        # Always read from the prompt files, not agent.messages, which gets
        # overwritten by _collect_sync / _stream on every chat request.
        system_prompt = agent.build_system_prompt()

        info: dict[str, Any] = {}

        # Include agent identity if available in config.
        if (
            agent.config is not None
            and hasattr(agent.config, "agent")
        ):
            info["agent"] = {
                "name": agent.config.agent.name,
                "description": agent.config.agent.description,
                "version": agent.config.agent.version,
            }

        info["model"] = {
            "name": agent.config.model.name,
            "temperature": agent.config.model.temperature,
            "max_tokens": agent.config.model.max_tokens,
        }
        info["system_prompt"] = system_prompt
        info["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in agent.tools.get_llm_tools()
        ]

        return JSONResponse(info)

    async def _create_session(self, body: CreateSessionRequest = Body(default_factory=CreateSessionRequest)):
        if self._session_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        perm_cfg = getattr(self._agent.config.server, "permissions", None)
        perm_scope = perm_cfg.source if (perm_cfg and perm_cfg.source) else None
        sid = await self._session_store.create(
            body.session_id,
            permission_scope_active=perm_scope,
        )
        return JSONResponse({"session_id": sid}, status_code=201)

    async def _get_session(self, session_id: str):
        if self._session_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        messages = await self._session_store.load(session_id)
        if messages is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return JSONResponse({"session_id": session_id, "messages": messages})

    async def _delete_session(self, session_id: str):
        if self._session_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        existed = await self._session_store.delete(session_id)
        if not existed:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return JSONResponse({"deleted": True})

    async def _fork_session(
        self,
        session_id: str,
        body: ForkSessionRequest = Body(default_factory=ForkSessionRequest),
    ):
        """Branch a session, copying messages up to *from_message_index*."""
        if self._session_store is None:
            raise HTTPException(status_code=501, detail="Session persistence not configured")
        if not await self._session_store.exists(session_id):
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        try:
            new_id = await self._session_store.fork(session_id, body.from_message_index)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        forked_messages = await self._session_store.load(new_id)
        message_count = len(forked_messages) if forked_messages else 0

        return JSONResponse(
            ForkSessionResponse(
                session_id=new_id,
                parent_session_id=session_id,
                message_count=message_count,
            ).model_dump(),
            status_code=201,
        )

    async def _revert_session(self, session_id: str, body: RevertSessionRequest):
        """Truncate a session's messages to *to_message_index*."""
        if self._session_store is None:
            raise HTTPException(status_code=501, detail="Session persistence not configured")
        if not await self._session_store.exists(session_id):
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        try:
            await self._session_store.revert(session_id, body.to_message_index)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _get_session_usage(self, session_id: str):
        """Return computed token + dollar usage for a session.

        Companion to ``GET /v1/sessions/{id}`` (raw messages) and the
        platform's ``/cost_data`` endpoint (raw counters). This route
        layers the configured :class:`~fipsagents.baseagent.config.PricingConfig`
        on top of the cumulative counters so callers (gateway, UI,
        BudgetEnforcer) get a single dollar figure without knowing the
        per-model rate table.

        404 when the session does not exist.  When the session exists
        but no turns have been recorded yet, all counters are zero and
        ``cost_usd`` is ``0.0``.
        """
        from .pricing import compute_cost, rate_for_model

        if self._session_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        if not await self._session_store.exists(session_id):
            raise HTTPException(
                status_code=404, detail=f"Session {session_id} not found",
            )

        cost_data = await self._session_store.get_cost_data(session_id)
        input_tokens = int(cost_data.get("input_tokens", 0) or 0)
        output_tokens = int(cost_data.get("output_tokens", 0) or 0)
        cached_tokens = int(cost_data.get("cached_tokens", 0) or 0)
        turn_count = int(cost_data.get("turn_count", 0) or 0)
        # Prefer the model recorded on cost_data (the model that
        # actually billed the tokens) over the agent's currently
        # configured default, which can drift between turns.
        model_name = cost_data.get("model") or self._agent.config.model.name

        pricing = self._agent.config.pricing
        rate = rate_for_model(model_name, pricing)
        cost_usd = compute_cost(
            model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            pricing=pricing,
        )

        return JSONResponse({
            "session_id": session_id,
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "turn_count": turn_count,
            "cost_usd": cost_usd,
            "pricing": {
                "input_per_1k": rate.input_per_1k,
                "output_per_1k": rate.output_per_1k,
                "cached_input_per_1k": rate.cached_input_per_1k,
                "per_request": rate.per_request,
            },
        })

    async def _list_traces(self, limit: int = 50, offset: int = 0):
        if self._trace_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        from dataclasses import asdict
        summaries = await self._trace_store.list_traces(limit=limit, offset=offset)
        return JSONResponse([asdict(s) for s in summaries])

    async def _get_trace(self, trace_id: str):
        if self._trace_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        from dataclasses import asdict
        trace = await self._trace_store.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")
        return JSONResponse(asdict(trace))

    async def _create_feedback(self, body: CreateFeedbackRequest, request: Request):
        if self._feedback_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        # Identity is gateway-issued via X-Auth-Subject (gateway-template#21
        # v1). Default to "anonymous" when running without a gateway in
        # front (local dev, smoke tests).
        user_id = request.headers.get("X-Auth-Subject", "anonymous")
        record = FeedbackRecord(
            feedback_id=_generate_feedback_id(),
            trace_id=body.trace_id or _new_trace_id(),
            session_id=body.session_id,
            rating=body.rating,
            comment=body.comment,
            correction=body.correction,
            model_id=body.model_id,
            latency_ms=body.latency_ms,
            turn_index=body.turn_index,
            agent_type=body.agent_type,
            created_at=_utc_now_iso(),
            user_id=user_id,
        )
        feedback_id = await self._feedback_store.add(record)
        return JSONResponse({"feedback_id": feedback_id}, status_code=201)

    async def _update_feedback(self, feedback_id: str, body: UpdateFeedbackRequest):
        if self._feedback_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        record = await self._feedback_store.update(
            feedback_id,
            rating=body.rating,
            comment=body.comment,
            correction=body.correction,
        )
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"Feedback {feedback_id} not found",
            )
        from dataclasses import asdict
        return JSONResponse(asdict(record))

    async def _list_feedback(
        self,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        if self._feedback_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        from dataclasses import asdict
        from datetime import datetime
        since_dt = datetime.fromisoformat(since) if since else None
        until_dt = datetime.fromisoformat(until) if until else None
        records = await self._feedback_store.query(
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            since=since_dt,
            until=until_dt,
            limit=min(limit, 1000),
            offset=max(offset, 0),
        )
        return JSONResponse([asdict(r) for r in records])

    async def _feedback_stats(
        self,
        window: str = "day",
        agent_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ):
        if self._feedback_store is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        from dataclasses import asdict
        from datetime import datetime
        if window not in ("hour", "day", "week"):
            raise HTTPException(status_code=400, detail="window must be 'hour', 'day', or 'week'")
        since_dt = datetime.fromisoformat(since) if since else None
        until_dt = datetime.fromisoformat(until) if until else None
        results = await self._feedback_store.stats(
            window=window,
            agent_type=agent_type,
            since=since_dt,
            until=until_dt,
        )
        return JSONResponse([asdict(r) for r in results])

    async def _upload_file(
        self,
        request: Request,
        file: UploadFile = File(...),
        session_id: str | None = Form(default=None),
    ):
        """Persist an uploaded file via the configured FileStore.

        Returns the metadata record (file_id, filename, mime_type,
        size_bytes, sha256, parse_status). The parser does not run yet
        in this endpoint — clients should poll
        ``GET /v1/files/{file_id}`` to observe parse_status transitions
        once the parser is wired in a later release.
        """
        if self._file_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")

        files_cfg = self._agent.config.server.files
        if not files_cfg.enabled:
            raise HTTPException(
                status_code=404, detail="File uploads are not enabled",
            )

        if session_id is not None and not _SESSION_ID_RE.match(session_id):
            raise HTTPException(
                status_code=400,
                detail="session_id must be 1-128 chars: letters, digits, "
                "hyphens, or underscores",
            )

        # Stream-read with a hard cap so a malicious client can't OOM
        # the server with a single oversized upload. Buffering the
        # whole file is acceptable here because the configured limit
        # is bounded; production deployments with large-file needs
        # should run a streaming proxy (eg gateway-template) in front.
        max_size = files_cfg.max_file_size_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"File exceeds max_file_size_bytes "
                        f"({max_size} bytes)"
                    ),
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        # Prefer content-sniffed MIME over the client-supplied
        # Content-Type. A client can lie about Content-Type (rename
        # foo.exe to foo.pdf and POST as application/pdf) but cannot
        # rewrite the file's magic bytes — libmagic reads those.
        # Falls back to the client claim when libmagic is unavailable.
        sniffed = detect_mime(data)
        claimed = file.content_type or "application/octet-stream"
        mime_type = sniffed or claimed
        if files_cfg.allowed_mime_types and mime_type not in files_cfg.allowed_mime_types:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"MIME type '{mime_type}' is not in the allowlist"
                ),
            )

        # Virus scan. NullScanner (the default when no URL is
        # configured) returns clean immediately. HttpScanner posts to
        # the configured sidecar; on infected → 422, on scanner error
        # honour fail_mode.
        if self._virus_scanner is not None:
            scan = await self._virus_scanner.scan(
                data, filename=file.filename or "unnamed",
            )
            if scan.infected:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "infected",
                        "viruses": scan.viruses,
                    },
                )
            if scan.error is not None:
                fail_mode = files_cfg.scanner.fail_mode
                logger.warning(
                    "VirusScanner: %s (fail_mode=%s)", scan.error, fail_mode,
                )
                if fail_mode == "closed":
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error": "scanner_unavailable",
                            "message": scan.error,
                        },
                    )

        user_id = request.headers.get("X-Auth-Subject", "anonymous")
        record = FileRecord(
            file_id=_generate_file_id(),
            filename=file.filename or "unnamed",
            mime_type=mime_type,
            size_bytes=total,
            sha256=_sha256(data),
            user_id=user_id,
            session_id=session_id,
        )

        # Inline parsing — populate extracted_text + parse_status on the
        # record before save so the file is immediately usable as a
        # reference in chat completions. Background-queue parsing for
        # large/slow inputs is a future PR.
        if self._file_parser is not None:
            outcome = await self._file_parser.parse(
                data, mime_type=mime_type, filename=record.filename,
            )
            record.parse_status = outcome.status  # type: ignore[assignment]
            record.extracted_text = outcome.text
            record.parse_error = outcome.error

        # Decide chunk_status before persisting so the row reflects what
        # is about to happen. The actual chunking runs after save() in a
        # background task (ADR-0002 lifecycle).
        chunking_cfg = files_cfg.chunking
        chunk_threshold = chunking_cfg.small_file_threshold_tokens
        will_chunk = (
            chunking_cfg.enabled
            and not isinstance(self._chunk_store, NullChunkStore)
            and record.extracted_text is not None
            and count_tokens(record.extracted_text) > chunk_threshold
        )
        if will_chunk:
            record.chunk_status = "processing"
        elif record.extracted_text is not None:
            # Below threshold or chunking disabled — full-text path.
            record.chunk_status = "skipped"
        # else: leave the default "pending" — no extracted text means
        # the chunking decision is moot.

        await self._file_store.save(record, data)

        # Kick off async chunking. The handle is held in
        # ``_chunking_tasks`` so the lifespan shutdown can drain it.
        if will_chunk:
            task = asyncio.create_task(
                self._chunk_uploaded_file(record),
                name=f"chunk_file_{record.file_id}",
            )
            self._chunking_tasks.add(task)
            task.add_done_callback(self._chunking_tasks.discard)

        return JSONResponse(
            {
                "file_id": record.file_id,
                "filename": record.filename,
                "mime_type": record.mime_type,
                "size_bytes": record.size_bytes,
                "sha256": record.sha256,
                "parse_status": record.parse_status,
                "parse_error": record.parse_error,
                "chunk_status": record.chunk_status,
                "chunk_count": record.chunk_count,
                "session_id": record.session_id,
                "created_at": record.created_at,
            },
            status_code=201,
        )

    async def _chunk_uploaded_file(self, record: FileRecord) -> None:
        """Async chunk + embed task spawned from ``_upload_file``.

        Writes status transitions back to the file store so a polling
        client (or the chat-completion retrieval path) can see when
        chunks become available. Failures are logged and recorded as
        ``chunk_status: failed`` — they do not propagate to the upload
        response (which already 201-ed).
        """
        if (
            self._chunker is None
            or self._chunk_store is None
            or self._file_store is None
        ):
            return
        if not record.extracted_text:
            return
        chunking_cfg = self._agent.config.server.files.chunking  # type: ignore[union-attr]
        try:
            chunks = await self._chunker.chunk(
                record.extracted_text,
                chunk_size_tokens=chunking_cfg.chunk_size_tokens,
                chunk_overlap_tokens=chunking_cfg.chunk_overlap_tokens,
            )
            if not chunks:
                await self._file_store.update_chunk_status(
                    record.file_id,
                    chunk_status="skipped",
                    chunk_count=0,
                )
                return
            written = await self._chunk_store.save_chunks(
                record.file_id,
                chunks,
                user_id=record.user_id,
                session_id=record.session_id,
            )
            if written > 0:
                await self._file_store.update_chunk_status(
                    record.file_id,
                    chunk_status="completed",
                    chunk_count=written,
                )
            else:
                await self._file_store.update_chunk_status(
                    record.file_id,
                    chunk_status="failed",
                    chunk_count=0,
                )
        except Exception:
            logger.warning(
                "Chunking failed for file_id=%s — falling back to full-text",
                record.file_id,
                exc_info=True,
            )
            try:
                await self._file_store.update_chunk_status(
                    record.file_id,
                    chunk_status="failed",
                    chunk_count=0,
                )
            except Exception:  # pragma: no cover — defensive
                logger.debug(
                    "Could not write chunk_status=failed for %s",
                    record.file_id, exc_info=True,
                )

    async def _get_file(self, file_id: str):
        """Return file metadata. 404 if not found or store disabled."""
        if self._file_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        if not self._agent.config.server.files.enabled:
            raise HTTPException(
                status_code=404, detail="File uploads are not enabled",
            )
        record = await self._file_store.get_metadata(file_id)
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"File {file_id} not found",
            )
        from dataclasses import asdict
        return JSONResponse(asdict(record))

    async def _delete_file(self, file_id: str):
        """Remove a file's metadata and bytes. 404 on unknown."""
        if self._file_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        if not self._agent.config.server.files.enabled:
            raise HTTPException(
                status_code=404, detail="File uploads are not enabled",
            )
        # ADR-0002 cascade: drop chunks before metadata so a user
        # observing concurrent operations never sees orphan chunks
        # outliving their parent file. Best-effort — failures are
        # logged but do not block the metadata delete (the cascade is
        # also re-run by housekeeping over time).
        if self._chunk_store is not None and not isinstance(
            self._chunk_store, NullChunkStore,
        ):
            try:
                await self._chunk_store.delete_for_file(file_id)
            except Exception:
                logger.warning(
                    "ChunkStore: cascade delete failed for %s",
                    file_id, exc_info=True,
                )
        deleted = await self._file_store.delete(file_id)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"File {file_id} not found",
            )
        return JSONResponse({"deleted": True, "file_id": file_id})

    async def _list_files(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
    ):
        """List files attached to a session, newest first.

        ``session_id`` is required — listing every file across all
        sessions would leak metadata across users in shared deployments
        and isn't a use case the FileStore ABC supports.
        """
        if self._file_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        if not self._agent.config.server.files.enabled:
            raise HTTPException(
                status_code=404, detail="File uploads are not enabled",
            )
        if not _SESSION_ID_RE.match(session_id):
            raise HTTPException(
                status_code=400,
                detail="session_id must be 1-128 chars: letters, digits, "
                "hyphens, or underscores",
            )
        from dataclasses import asdict
        records = await self._file_store.list_for_session(
            session_id,
            limit=min(max(limit, 0), 1000),
            offset=max(offset, 0),
        )
        return JSONResponse([asdict(r) for r in records])

    async def _resolve_file_attachments(
        self,
        file_ids: list[str],
        *,
        last_user_message: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch each file's content and produce system messages.

        Three branches per file (ADR-0002):

        - Chunked path: when chunking is enabled, the file has finished
          chunking (``chunk_count > 0``), and ``last_user_message`` is
          non-empty, retrieve top-K chunks via the configured
          ``ChunkStore`` and inject only those.
        - Full-text path: the existing 0.17.0 behavior — inject the
          whole ``extracted_text``. Used when chunking is disabled, the
          file is below the size threshold, chunking is still in
          progress (warm-up window), or chunking failed (graceful
          degradation).
        - Stub path: file has no extracted text yet (parse pending /
          failed / skipped) — inject a "content not available" note.

        Unknown ``file_id`` values raise HTTP 400 — the caller controls
        the list and a missing reference is always a client bug.
        """
        if self._file_store is None or self._agent is None:
            raise HTTPException(status_code=503, detail="Server not ready")
        files_cfg = self._agent.config.server.files
        if not files_cfg.enabled:
            raise HTTPException(
                status_code=400,
                detail="file_ids supplied but file uploads are not enabled",
            )

        chunking_cfg = files_cfg.chunking
        chunked_enabled = (
            chunking_cfg.enabled
            and self._chunk_store is not None
            and not isinstance(self._chunk_store, NullChunkStore)
            and bool(last_user_message)
        )

        messages: list[dict[str, Any]] = []
        for fid in file_ids:
            record = await self._file_store.get_metadata(fid)
            if record is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"file_id {fid} not found",
                )
            header = (
                f"[Attached file: {record.filename} "
                f"({record.mime_type}, {record.size_bytes} bytes)]"
            )

            # Chunked retrieval branch.
            if (
                chunked_enabled
                and record.chunk_count > 0
                and record.chunk_status == "completed"
                and self._chunk_store is not None
            ):
                chunks = await self._chunk_store.search(
                    fid,
                    last_user_message,
                    limit=chunking_cfg.retrieval_top_k,
                    min_score=chunking_cfg.retrieval_min_score,
                )
                if chunks:
                    body = "\n---\n".join(c.content for c in chunks)
                    messages.append({
                        "role": "system",
                        "content": f"{header}\n{body}",
                    })
                    continue
                # No matches — fall through to full-text rather than
                # leaving the model with nothing.

            if record.extracted_text:
                text = record.extracted_text
                max_tok = files_cfg.max_injection_tokens
                if max_tok > 0:
                    tok_count = count_tokens(text)
                    if tok_count > max_tok:
                        # Character-based first cut (~4 chars/token) to
                        # avoid re-counting the full text.
                        text = text[: max_tok * 4]
                        # Verify and trim further if the estimate was
                        # too generous.
                        while count_tokens(text) > max_tok:
                            text = text[: int(len(text) * 0.9)]
                        omitted = tok_count - count_tokens(text)
                        text += (
                            f"\n\n[... content truncated — {omitted} "
                            "tokens omitted. Enable chunking for "
                            "full-content retrieval.]"
                        )
                messages.append({
                    "role": "system",
                    "content": f"{header}\n{text}",
                })
            else:
                messages.append({
                    "role": "system",
                    "content": (
                        f"{header} — content not available "
                        f"(parse_status: {record.parse_status})"
                    ),
                })
        return messages

    async def _resolve_image_file_ids(
        self, messages: list[dict[str, Any]]
    ) -> None:
        """Rewrite ``file_id:<id>`` image URLs to inline ``data:`` URIs.

        Walks user messages whose ``content`` is a list of OpenAI-shaped
        content blocks; for each ``image_url`` block whose ``url`` matches
        ``file_id:<id>``, fetches the bytes from the configured
        :class:`BytesStore`, sniffs the MIME type, and rewrites the URL in
        place to ``data:{mime};base64,{...}`` before the request reaches
        the model.

        Mutates *messages* in place. No-op when no list-content user
        messages reference ``file_id:`` URLs. Raises HTTP 400 if a
        referenced file is missing or its MIME type cannot be detected,
        and 503 if the server has no BytesStore configured.
        """
        # Cheap pre-scan: avoid a 503 / lookup when no message references
        # a file_id. Most requests do not.
        has_ref = False
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "image_url":
                    continue
                url = (block.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("file_id:"):
                    has_ref = True
                    break
            if has_ref:
                break
        if not has_ref:
            return

        if self._bytes_store is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "image_url references a file_id but no BytesStore is "
                    "configured (server.files.enabled=false)"
                ),
            )

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "image_url":
                    continue
                image_url = block.get("image_url") or {}
                url = image_url.get("url", "")
                if not (isinstance(url, str) and url.startswith("file_id:")):
                    continue
                file_id = url[len("file_id:") :]
                data = await self._bytes_store.get(file_id)
                if data is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"file_id {file_id} not found",
                    )
                mime = detect_mime(data)
                if not mime:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"could not detect MIME type for file_id {file_id}; "
                            "image_url requires a recognisable image format"
                        ),
                    )
                encoded = base64.b64encode(data).decode("ascii")
                image_url["url"] = f"data:{mime};base64,{encoded}"

    def _should_trace(self) -> bool:
        """Decide whether to trace this request based on sampling rate."""
        if self._trace_store is None or self._agent is None:
            return False
        rate = self._agent.config.server.traces.sampling_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

    async def _metrics_endpoint(self):
        if self._metrics_collector is None or isinstance(
            self._metrics_collector, NullMetricsCollector
        ):
            raise HTTPException(status_code=404, detail="Metrics not enabled")
        return Response(
            content=self._metrics_collector.generate_metrics(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def _chat_completions(self, request: Request, req: ChatCompletionRequest):
        if self._agent is None:
            raise HTTPException(status_code=503, detail="Agent not ready")

        agent = self._agent
        model_name = req.model or agent.config.model.name
        incoming = _messages_to_dicts(req.messages)
        overrides = _extract_overrides(req)
        # Tenant identity is gateway-stamped; default to "default" so
        # the metrics label space stays bounded when running without a
        # gateway in front (local dev, smoke tests).
        tenant_id = request.headers.get("X-Tenant") or "default"

        # Budget pre-check: rejects (402) if cumulative session/tenant
        # cost is already over a configured hard limit.  No-op when no
        # budget is configured.
        if self._budget_enforcer is not None:
            try:
                await self._budget_enforcer.check_before_request(
                    session_id=req.session_id, tenant_id=tenant_id,
                )
            except BudgetExceededError as exc:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "budget_exceeded",
                        "scope": exc.scope,
                        "identifier": exc.identifier,
                        "current_usd": exc.current_usd,
                        "limit_usd": exc.limit_usd,
                    },
                ) from exc

        # Session: load prior messages if session_id provided.
        from fipsagents.baseagent.agent import _stamp_message_id

        if req.session_id and self._session_store:
            stored = await self._session_store.load(req.session_id)
            if stored:
                for msg in stored:
                    _stamp_message_id(msg)
                incoming = stored + incoming
            else:
                logger.info("Session %s not found; will auto-create on save", req.session_id)

        for msg in incoming:
            _stamp_message_id(msg)

        # Guard: reject if session has a pending question not answered by this request.
        _pending_question_data: dict | None = None
        if req.session_id and self._session_store:
            session_state = await self._session_store.get_state(req.session_id)
            pending_q = session_state.get("pending_question")
            if pending_q:
                import json as _json
                try:
                    _pending_question_data = _json.loads(pending_q) if isinstance(pending_q, str) else pending_q
                except (ValueError, TypeError):
                    logger.error(
                        "Corrupted pending_question JSON in session %s: %r",
                        req.session_id, pending_q,
                    )
                    _pending_question_data = {"question_id": str(pending_q)}
                pq_id = _pending_question_data.get("question_id", pending_q)
                if not req.answers_to_question_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "pending_question",
                            "question_id": pq_id,
                            "message": "Session has an unanswered question. "
                                       "Include answers_to_question_id in your request.",
                        },
                    )

        # Answer injection: replace the sentinel tool result with the answer.
        if req.answers_to_question_id and req.session_id and self._session_store:
            await self._session_store.update_state(
                req.session_id, pending_question=None,
            )
            tool_call_id = (
                _pending_question_data.get("tool_call_id")
                if _pending_question_data else None
            )
            if not tool_call_id:
                logger.error(
                    "pending_question missing tool_call_id for session %s",
                    req.session_id,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Session state corrupted: pending_question lacks tool_call_id",
                )
            # Extract the answer from the last user message and track
            # its ID so we drop exactly that message, not a duplicate.
            answer_content = ""
            answer_msg_id = None
            for msg in reversed(incoming):
                if msg.get("role") == "user":
                    c = msg.get("content", "")
                    answer_content = c if isinstance(c, str) else str(c)
                    answer_msg_id = msg.get("id")
                    break
            # Permission-ask: execute or deny the tool based on the answer.
            if _pending_question_data and _pending_question_data.get("permission_ask"):
                perm_tool = _pending_question_data.get("tool_name")
                perm_args = _pending_question_data.get("tool_args", {})
                _answer_lower = answer_content.strip().lower()
                _approved = _answer_lower in (
                    "allow", "allowed", "yes", "approve", "approved",
                )
                if _approved and perm_tool and self._agent is not None:
                    _result = await self._agent.tools.execute(perm_tool, perm_args)
                    _content = (
                        _result.result if not _result.is_error
                        else f"ERROR: {_result.error}"
                    )
                    for msg in incoming:
                        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                            msg["content"] = _content
                            break
                    if self._agent is not None:
                        self._agent._permission_preapproved.add(tool_call_id)
                else:
                    _deny = f"DENIED: Tool '{perm_tool}' was denied by the operator."
                    for msg in incoming:
                        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                            msg["content"] = _deny
                            break
            else:
                for msg in incoming:
                    if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                        msg["content"] = answer_content
                        break
            # Drop the answer message by ID — not by content match.
            if answer_msg_id:
                incoming = [m for m in incoming if m.get("id") != answer_msg_id]
            # Reset agent-side pending state.
            if self._agent is not None:
                self._agent._question_pending = None
                self._agent._question_events = []

        # File attachments: resolve file_ids to extracted text and inject
        # as system messages just before the current user turn so the
        # model treats them as context for this request.
        if req.file_ids:
            # Pull the last user message text so chunk retrieval has a
            # query. Fall back to an empty string when the request has
            # no user-role message (e.g. system-only payloads) — the
            # resolver then takes the full-text branch.
            last_user = ""
            for msg in reversed(incoming):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        last_user = content
                    elif isinstance(content, list):
                        # Multimodal turn: join text from text-typed
                        # blocks. Image blocks contribute nothing to
                        # chunk retrieval.
                        last_user = "\n".join(
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    break
            file_msgs = await self._resolve_file_attachments(
                req.file_ids, last_user_message=last_user,
            )
            if file_msgs:
                # Insert before the last incoming message (the user's
                # current turn) so prior session history retains its
                # ordering and the file context is closest to the
                # question being asked.
                if len(incoming) >= 1:
                    incoming = incoming[:-1] + file_msgs + incoming[-1:]
                else:
                    incoming = file_msgs + incoming

        # Multimodal: resolve any ``file_id:<id>`` image URLs to inline
        # ``data:`` URIs. Runs after file_ids extraction so the existing
        # text-RAG path is unaffected, and the rewrite is the last
        # mutation of the message list before the model call.
        await self._resolve_image_file_ids(incoming)

        # Trace ID: always generated so clients can correlate this
        # completion with feedback/observability data, even when tracing
        # persistence is disabled or sampled out. If a parent trace
        # context is propagated, use that trace_id so we join the
        # distributed trace.
        from .propagation import extract_trace_context
        parent_ctx = extract_trace_context(request.headers)
        trace_id = (parent_ctx.trace_id if parent_ctx else None) or _new_trace_id()

        # Tracing: create collector if sampling says yes.
        collector: TraceCollector | None = None
        if self._should_trace():
            traces_cfg = agent.config.server.traces
            collector = TraceCollector(
                self._trace_store,
                trace_id=trace_id,
                session_id=req.session_id,
                model=model_name,
                provider=getattr(agent.config.model, "provider", None),
                parent_span_id=parent_ctx.parent_span_id if parent_ctx else None,
                fidelity=traces_cfg.fidelity,
            )
            collector.begin_request(
                {"model": model_name, "stream": req.stream, "session_id": req.session_id},
                messages=list(incoming) if traces_cfg.fidelity != "minimal" else None,
            )

        # Metrics: start timing.
        metrics_start: float | None = None
        if self._metrics_collector is not None:
            metrics_start = self._metrics_collector.record_request_start()

        # Forward the inbound Authorization header so delegate_to_agent can
        # propagate it to subagents configured with ``identity: inherit``.
        # Reset to None after the response so the value doesn't leak into
        # subsequent requests when the agent instance is reused.
        agent._inbound_auth_header = request.headers.get("authorization")

        # Propagate delegation depth from upstream callers so this agent's
        # delegate_to_agent / spawn_agent depth checks enforce correctly
        # in multi-hop remote chains.
        _raw_depth = request.headers.get("x-subagent-depth")
        if _raw_depth is not None:
            try:
                agent._delegation_depth = int(_raw_depth)
            except (ValueError, TypeError):
                pass

        if not req.stream:
            try:
                content, metrics, finish_reason = await self._collect_sync(
                    agent, incoming, model_name=model_name,
                    overrides=overrides, collector=collector,
                    tenant_id=tenant_id, session_id=req.session_id,
                )
            finally:
                agent._inbound_auth_header = None
                agent._delegation_depth = 0
                agent._permission_source = None
                agent._permission_preapproved = set()
            # Session: save after sync response.
            if req.session_id and self._session_store:
                await self._session_store.save(req.session_id, agent.messages)
                await self._persist_cost_data(
                    req.session_id, metrics, model_name,
                )
                # Persist pending question state.
                q_pending = getattr(agent, "_question_pending", None)
                if q_pending:
                    import json as _q_json
                    await self._session_store.update_state(
                        req.session_id,
                        pending_question=_q_json.dumps(q_pending),
                    )
            # Budget post-record: refresh the in-process tenant counter
            # from the new session cost. Logs soft-warning crossings.
            # Run even without a session_id so future tenant-only modes work.
            if self._budget_enforcer is not None:
                try:
                    await self._budget_enforcer.record_after_request(
                        session_id=req.session_id, tenant_id=tenant_id,
                    )
                except Exception:  # noqa: BLE001 — keep response alive
                    logger.warning(
                        "Budget post-record failed", exc_info=True,
                    )
            if collector:
                await collector.end_request()
            if self._metrics_collector and metrics_start is not None:
                self._metrics_collector.record_request_end(
                    model_name, False, "ok", metrics_start,
                )
            return JSONResponse(
                _sync_response(
                    model_name,
                    content,
                    metrics=metrics,
                    finish_reason=finish_reason,
                ),
                headers={"X-Trace-Id": trace_id},
            )

        return StreamingResponse(
            self._stream(
                incoming, model_name, overrides=overrides,
                session_id=req.session_id, collector=collector,
                metrics_start=metrics_start, trace_id=trace_id,
                tenant_id=tenant_id, run_budget_post=True,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )

    # -- Compaction helper ---------------------------------------------------

    async def _maybe_compact(
        self,
        agent: BaseAgent,
        *,
        session_id: str | None = None,
    ) -> None:
        """Run compaction on ``agent.messages`` if the compactor triggers."""
        from .compactor import NullCompactor

        if self._compactor is None or isinstance(self._compactor, NullCompactor):
            return

        # Skip compaction when pending state exists.
        if session_id and self._session_store:
            _state = await self._session_store.get_state(session_id)
            if (
                _state.get("pending_question")
                or _state.get("open_tool_calls")
                or _state.get("pending_subagent_calls")
            ):
                logger.debug("Compaction skipped: pending state")
                return

        if not await self._compactor.should_compact(agent.messages):
            return

        result = await self._compactor.compact(agent.messages)
        if not result.skipped:
            agent.messages = result.messages
            logger.info(
                "Compaction: %d -> %d messages",
                result.original_count,
                result.compacted_count,
            )
            if session_id and self._session_store:
                import json as _cjson
                await self._session_store.update_state(
                    session_id,
                    compaction_state=_cjson.dumps({"compaction_count": 1}),
                )
        else:
            logger.debug("Compaction skipped: %s", result.skip_reason)

    # -- Event loop ------------------------------------------------------------

    async def _event_loop(
        self,
        source: Any,
        sink: Any,
    ) -> None:
        """Process events from a source, serialised through _agent_lock."""
        import traceback
        from datetime import UTC, datetime

        from fipsagents.baseagent.config import EventRetryConfig
        from .events import OutboundEvent

        logger.info("Event loop started: source=%s", source.source_id)
        retry_cfg = getattr(source.config, "retry", None)
        if retry_cfg is None:
            retry_cfg = EventRetryConfig()

        try:
            async for event in source.consume():
                def _now() -> datetime:
                    return datetime.now(tz=UTC)

                for attempt in range(retry_cfg.max_attempts):
                    try:
                        content = await self._process_event(event, source)
                        await sink.emit(OutboundEvent(
                            correlation_id=event.event_id,
                            event_type="response",
                            payload={"content": content},
                            source=event.source,
                            timestamp=_now(),
                        ))
                        await source.acknowledge(event.event_id)
                        break
                    except Exception as exc:
                        is_retriable = (
                            type(exc).__name__ in retry_cfg.retriable_errors
                        )
                        if is_retriable and attempt + 1 < retry_cfg.max_attempts:
                            delay = min(
                                retry_cfg.backoff_base ** attempt,
                                retry_cfg.backoff_max,
                            )
                            logger.warning(
                                "Event %s: retriable error (attempt %d/%d), "
                                "retrying in %.1fs: %s",
                                event.event_id, attempt + 1,
                                retry_cfg.max_attempts, delay, exc,
                            )
                            await asyncio.sleep(delay)
                            continue
                        logger.error(
                            "Event %s: processing failed: %s",
                            event.event_id, exc,
                        )
                        await sink.emit(OutboundEvent(
                            correlation_id=event.event_id,
                            event_type="processing_failed",
                            payload={"error": traceback.format_exc()},
                            source=event.source,
                            timestamp=_now(),
                        ))
                        await source.acknowledge(event.event_id)
                        break
        except asyncio.CancelledError:
            logger.info("Event loop cancelled: source=%s", source.source_id)
        except Exception:
            logger.exception(
                "Event loop crashed: source=%s", source.source_id,
            )

    async def _process_event(
        self,
        event: Any,
        source: Any,
    ) -> str:
        """Process a single inbound event through the agent.

        Reuses _collect_sync which handles _agent_lock, compaction,
        permissions, and the observer chain (metrics, tracing).
        """
        from fipsagents.baseagent.agent import _stamp_message_id

        from .events import default_translate_event

        assert self._agent is not None
        agent = self._agent

        # Translate event to messages.
        messages = default_translate_event(event)
        for msg in messages:
            _stamp_message_id(msg)

        # Resolve session key.
        session_key = event.session_key or source.source_id

        # Load session if store available.
        if self._session_store:
            stored = await self._session_store.load(session_key)
            if stored:
                for msg in stored:
                    _stamp_message_id(msg)
                messages = stored + messages

        # Run agent via _collect_sync (handles lock, compaction, etc).
        content, metrics, finish_reason = await self._collect_sync(
            agent,
            messages,
            model_name=agent.config.model.name,
            session_id=session_key,
        )

        # Save session.
        if self._session_store:
            await self._session_store.save(session_key, agent.messages)

        return content

    # -- Sync ----------------------------------------------------------------

    async def _collect_sync(
        self,
        agent: BaseAgent,
        incoming: list[dict[str, Any]],
        *,
        model_name: str = "",
        overrides: dict[str, Any] | None = None,
        collector: TraceCollector | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, StreamMetrics | None, str]:
        """Drive ``astep_stream`` for a non-streaming response.

        Fully drains the iterator so any post-``StreamComplete`` hooks
        in the subclass (e.g. memory writes) run to completion.

        If no ``ContentDelta`` events are emitted (e.g. the agent executed
        tools and the final content was appended directly to
        ``agent.messages`` without streaming deltas), fall back to the
        last assistant message in the conversation history.
        """
        parts: list[str] = []
        metrics: StreamMetrics | None = None
        finish_reason = "stop"
        async with self._agent_lock:
            agent.messages = list(incoming)
            agent._question_pending = None
            agent._question_events = []
            agent._permission_source = self._permission_source
            _perm_cfg = getattr(
                getattr(agent, "config", None), "server", None,
            )
            _perm_cfg = getattr(_perm_cfg, "permissions", None)
            agent._permission_mode = (
                getattr(_perm_cfg, "mode", "enforce") if _perm_cfg else "enforce"
            )
            agent._permission_preapproved = set()
            agent._work_item_store = self._work_item_store
            agent._work_item_actor_id = session_id or "anonymous"
            agent._work_item_events = []

            # Compaction check (before agent loop).
            await self._maybe_compact(agent, session_id=session_id)

            # State recovery: load per-session state.
            if self._state_recovery_cfg and agent.state_type is not None:
                agent._agent_state = await self._load_or_recover_state(
                    agent, session_id,
                )

            events = agent.astep_stream(max_iterations=10, **(overrides or {}))
            if self._metrics_collector is not None:
                events = self._metrics_collector.observe(
                    events,
                    model=model_name,
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
            if getattr(agent, "_agent_state", None) is not None:
                from fipsagents.baseagent.state import StateReducerObserver
                events = StateReducerObserver(agent).observe(events)
            if collector:
                events = collector.observe(events)
            async for event in events:
                if isinstance(event, ContentDelta):
                    parts.append(event.content)
                elif isinstance(event, StreamComplete):
                    metrics = event.metrics
                    finish_reason = event.finish_reason

            # State recovery: checkpoint after turn completes.
            if getattr(agent, "_agent_state", None) is not None and session_id:
                await self._checkpoint_state(
                    agent, session_id,
                    trace_id=collector.trace_id if collector else "",
                )

        content = "".join(parts)

        # Strip echoed memory injection tags from the response. When
        # injection_mode is "user_turn" the framework wraps memories in
        # <injection_tag>...</injection_tag> before sending them to the model.
        # Some models echo those tags back verbatim; strip them defensively.
        if agent.config.memory.injection_mode == "user_turn":
            tag = re.escape(agent.config.memory.injection_tag)
            content = re.sub(
                rf"<{tag}>.*?</{tag}>", "", content, flags=re.DOTALL
            ).strip()

        # Fallback: if no ContentDelta events were yielded but the agent
        # appended an assistant message (common after tool execution in
        # subclasses that override astep_stream), use that content.
        if not content and agent.messages:
            for msg in reversed(agent.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    content = msg["content"]
                    break

        return content, metrics, finish_reason

    # -- Streaming -----------------------------------------------------------

    async def _stream(
        self,
        incoming: list[dict[str, Any]],
        model_name: str,
        *,
        overrides: dict[str, Any] | None = None,
        session_id: str | None = None,
        collector: TraceCollector | None = None,
        metrics_start: float | None = None,
        trace_id: str | None = None,
        tenant_id: str | None = None,
        run_budget_post: bool = False,
    ) -> AsyncIterator[str]:
        """Drive the agent's event stream, serialising to OpenAI SSE chunks.

        NOTE: Memory injection tags are stripped in ``_collect_sync`` for
        non-streaming responses. For streaming, tag echoing is rare and
        cross-chunk stripping would add latency/complexity. If needed, a
        post-processing filter can be added later.
        """
        async with self._agent_lock:
            assert self._agent is not None
            self._agent.messages = list(incoming)
            self._agent._question_pending = None
            self._agent._question_events = []
            self._agent._permission_source = self._permission_source
            _perm_cfg = getattr(
                getattr(self._agent, "config", None),
                "server", None,
            )
            _perm_cfg = getattr(_perm_cfg, "permissions", None)
            self._agent._permission_mode = (
                getattr(_perm_cfg, "mode", "enforce") if _perm_cfg else "enforce"
            )
            self._agent._permission_preapproved = set()
            self._agent._work_item_store = self._work_item_store
            self._agent._work_item_actor_id = session_id or "anonymous"
            self._agent._work_item_events = []

            # Compaction check (before agent loop).
            await self._maybe_compact(
                self._agent, session_id=session_id,
            )

            # State recovery: load per-session state.
            if self._state_recovery_cfg and self._agent.state_type is not None:
                self._agent._agent_state = await self._load_or_recover_state(
                    self._agent, session_id,
                )

            stream_status = "ok"
            captured_metrics: StreamMetrics | None = None
            try:
                events = self._agent.astep_stream(max_iterations=10, **(overrides or {}))
                if self._metrics_collector is not None:
                    events = self._metrics_collector.observe(
                        events,
                        model=model_name,
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                if getattr(self._agent, "_agent_state", None) is not None:
                    from fipsagents.baseagent.state import StateReducerObserver
                    events = StateReducerObserver(self._agent).observe(events)
                if collector:
                    events = collector.observe(events)

                # Pass-through observer that snapshots the StreamMetrics
                # from the StreamComplete event so the post-stream
                # cost-data accumulator can read them.
                async def _capture_metrics(stream):
                    nonlocal captured_metrics
                    async for ev in stream:
                        if isinstance(ev, StreamComplete):
                            captured_metrics = ev.metrics
                        yield ev

                events = _capture_metrics(events)
                async for chunk in stream_events_as_sse(
                    events, model_name, trace_id=trace_id,
                ):
                    yield chunk
            except Exception:
                logger.exception("Stream errored")
                stream_status = "error"
            finally:
                self._agent._inbound_auth_header = None
                self._agent._delegation_depth = 0
                self._agent._permission_source = None
                self._agent._permission_preapproved = set()

            # Tracing: finalize after streaming completes.
            if collector:
                await collector.end_request()

            # Metrics: record request end.
            if self._metrics_collector and metrics_start is not None:
                self._metrics_collector.record_request_end(
                    model_name, True, stream_status, metrics_start,
                )

            # Session: save after streaming completes.
            if session_id and self._session_store:
                await self._session_store.save(session_id, self._agent.messages)
                await self._persist_cost_data(
                    session_id, captured_metrics, model_name,
                )
                # Persist pending question state.
                q_pending = getattr(self._agent, "_question_pending", None)
                if q_pending:
                    import json as _q_json
                    await self._session_store.update_state(
                        session_id,
                        pending_question=_q_json.dumps(q_pending),
                    )

            # State recovery: checkpoint after turn completes.
            if getattr(self._agent, "_agent_state", None) is not None and session_id:
                await self._checkpoint_state(
                    self._agent, session_id,
                    trace_id=collector.trace_id if collector else "",
                )

            # Budget post-record (mirrors the sync path).
            if run_budget_post and self._budget_enforcer is not None:
                try:
                    await self._budget_enforcer.record_after_request(
                        session_id=session_id, tenant_id=tenant_id,
                    )
                except Exception:  # noqa: BLE001 — keep response alive
                    logger.warning(
                        "Budget post-record failed", exc_info=True,
                    )

    # -- Cost-data accumulator -----------------------------------------------

    async def _persist_cost_data(
        self,
        session_id: str,
        metrics: StreamMetrics | None,
        model_name: str,
    ) -> None:
        """Accumulate this turn's token usage into the session's cost_data.

        Cumulative-for-the-session: read the existing accumulator, add
        this turn's deltas, write it back. Failures are logged and
        swallowed -- cost tracking must never break the chat response.

        Backends that don't support reading cost_data (eg
        :class:`HttpSessionStore`) raise :class:`NotImplementedError`
        from ``get_cost_data``; in that case we treat the existing
        total as empty and the next ``update`` records this turn's
        delta only. A follow-up issue tracks exposing the platform
        read endpoint so HTTP-backed deployments get cumulative totals.
        """
        if metrics is None or self._session_store is None:
            return

        prompt = metrics.prompt_tokens
        completion = metrics.completion_tokens
        # Nothing useful to record when the provider didn't report usage.
        if prompt is None and completion is None:
            return

        try:
            existing = await self._session_store.get_cost_data(session_id)
        except NotImplementedError:
            existing = {}
        except Exception:  # noqa: BLE001 — keep chat response alive
            logger.warning(
                "Failed to read cost_data for %s; using empty baseline",
                session_id,
                exc_info=True,
            )
            existing = {}

        new_data = {
            "input_tokens": int(existing.get("input_tokens", 0) or 0)
            + int(prompt or 0),
            "output_tokens": int(existing.get("output_tokens", 0) or 0)
            + int(completion or 0),
            "cached_tokens": int(existing.get("cached_tokens", 0) or 0),
            "model": model_name or existing.get("model"),
            "turn_count": int(existing.get("turn_count", 0) or 0) + 1,
        }

        # Roll up subagent token usage from this turn into the parent's
        # session totals. The buffer is populated by delegate_to_agent and
        # drained here so tokens are not double-counted across turns.
        subagent_usages = getattr(self._agent, "_subagent_token_usage", None)
        if subagent_usages:
            new_data["input_tokens"] += sum(
                int(u.get("input", 0) or 0) for u in subagent_usages
            )
            new_data["output_tokens"] += sum(
                int(u.get("output", 0) or 0) for u in subagent_usages
            )
            new_data["cached_tokens"] += sum(
                int(u.get("cached", 0) or 0) for u in subagent_usages
            )
            subagent_usages.clear()

        try:
            await self._session_store.update(session_id, cost_data=new_data)
        except Exception:  # noqa: BLE001 — keep chat response alive
            logger.warning(
                "Failed to persist cost_data for %s",
                session_id,
                exc_info=True,
            )

    # -- State recovery -------------------------------------------------------

    async def _load_or_recover_state(
        self,
        agent: BaseAgent,
        session_id: str | None,
    ) -> Any:
        """Load agent state from checkpoint, replaying missed events."""
        if not session_id or not self._session_store:
            return agent.state_type()  # type: ignore[union-attr]

        from .recovery import recover_state

        recovered = await recover_state(
            agent, session_id,
            self._session_store,
            self._trace_store or NullTraceStore(),
        )
        return recovered if recovered is not None else agent.state_type()  # type: ignore[union-attr]

    async def _checkpoint_state(
        self,
        agent: BaseAgent,
        session_id: str,
        *,
        trace_id: str = "",
    ) -> None:
        """Persist a state checkpoint to the session store."""
        if agent._agent_state is None or self._session_store is None:
            return
        import json as _ckpt_json
        from datetime import datetime, timezone
        from fipsagents.baseagent.state import state_schema_key

        checkpoint_data = _ckpt_json.dumps({
            "state": agent._agent_state.model_dump(),
            "last_trace_id": trace_id,
            "last_span_id": "",
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": state_schema_key(type(agent._agent_state)),
        })
        try:
            await self._session_store.update_state(
                session_id, checkpoint_state=checkpoint_data,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to checkpoint state for %s", session_id,
                exc_info=True,
            )

    # -- Run -----------------------------------------------------------------

    def run(self, *, host: str = "0.0.0.0", port: int = 8080, **uvicorn_kwargs) -> None:
        """Start the server with uvicorn.

        Requires the ``[server]`` extra (uvicorn is included).

        Args:
            host: Bind address.
            port: Bind port.
            **uvicorn_kwargs: Additional keyword arguments forwarded to
                ``uvicorn.run``.
        """
        try:
            import uvicorn
        except ImportError as exc:
            raise ImportError(
                "fipsagents.server requires the [server] extra. "
                "Install with: pip install 'fipsagents[server]'"
            ) from exc

        uvicorn.run(self.app, host=host, port=port, **uvicorn_kwargs)
