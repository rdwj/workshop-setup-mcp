"""File persistence backends.

Stores metadata (filename, MIME type, size, SHA-256, extracted text) and
raw bytes for files uploaded to the agent. The ``POST /v1/files``
endpoint persists uploads via this module; ``ChatCompletionRequest``'s
``file_ids`` field resolves to extracted text injected into message
context before BaseAgent processes the request.

Two-tier separation: metadata lives in a relational store (SQLite or
Postgres), bytes live in object storage (local filesystem for dev,
S3-compatible for production). For dev parity, ``SqliteFileStore``
owns both — metadata in SQLite plus bytes in a local directory sharded
by ``file_id`` prefix. UUID-based keys are used everywhere; the
user-supplied filename is metadata only and never appears in a
storage path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .bytes_store import BytesStore, LocalFsBytesStore

logger = logging.getLogger(__name__)


def _generate_file_id() -> str:
    return f"file_{uuid.uuid4().hex[:24]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bytes_path(bytes_dir: str, file_id: str) -> str:
    """Sharded path under *bytes_dir* keyed by *file_id*.

    Two-character prefix shard keeps a single directory from growing
    unbounded. ``file_<32 hex>`` → ``<bytes_dir>/fi/file_<32 hex>``.
    """
    shard = file_id[:2] if len(file_id) >= 2 else "00"
    return os.path.join(bytes_dir, shard, file_id)


# Module-level cache so we only complain about a missing libmagic once
# per process instead of on every upload.
_magic_unavailable_logged = False

# Magic-byte signatures for the built-in fallback sniffer.  Each entry
# is ``(offset, prefix_bytes, mime_type)``.  Checked in order; first
# match wins.
_SIGNATURES: list[tuple[int, bytes, str]] = [
    # Images
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"RIFF", "image/webp"),       # needs secondary check for WEBP
    (0, b"BM", "image/bmp"),
    (0, b"II\x2a\x00", "image/tiff"),  # little-endian TIFF
    (0, b"MM\x00\x2a", "image/tiff"),  # big-endian TIFF
    # Documents
    (0, b"%PDF", "application/pdf"),
    # Archives
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"\x1f\x8b", "application/gzip"),
    # Audio / video
    (0, b"OggS", "audio/ogg"),
    (0, b"\xff\xfb", "audio/mpeg"),    # MP3 frame sync
    (0, b"\xff\xf3", "audio/mpeg"),
    (0, b"\xff\xf2", "audio/mpeg"),
    (0, b"ID3", "audio/mpeg"),         # MP3 with ID3 tag
    (0, b"fLaC", "audio/flac"),
    (0, b"RIFF", "audio/wav"),         # needs secondary check for WAVE
]

# RIFF container needs a secondary fourcc check at offset 8.
_RIFF_SUBTYPES: dict[bytes, str] = {
    b"WEBP": "image/webp",
    b"WAVE": "audio/wav",
    b"AVI ": "video/x-msvideo",
}


def _sniff_builtin(data: bytes) -> str | None:
    """Pure-Python magic-byte sniffer — no external dependencies.

    Handles the most common binary signatures and a basic text/binary
    heuristic.  Returns None when the content doesn't match any known
    pattern and doesn't look like plain text.
    """
    if not data:
        return None

    # Check fixed-offset magic-byte signatures.
    for offset, sig, mime in _SIGNATURES:
        if data[offset:offset + len(sig)] == sig:
            # RIFF container: disambiguate via the fourcc at offset 8.
            if sig == b"RIFF" and len(data) >= 12:
                fourcc = bytes(data[8:12])
                riff_mime = _RIFF_SUBTYPES.get(fourcc)
                if riff_mime:
                    return riff_mime
                continue  # unrecognised RIFF variant — skip
            return mime

    # ftyp-based container (MP4 / M4A / QuickTime).
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"

    # Text heuristic: if the first 8 KiB contain only bytes that are
    # common in UTF-8 / ASCII text, call it text/plain.
    sample = data[:8192]
    # Allow TAB (0x09), LF (0x0a), CR (0x0d), and printable ASCII
    # (0x20-0x7e), plus any multi-byte UTF-8 continuation/start
    # bytes (0x80-0xfe).  Reject NUL and most C0 control chars.
    _TEXT_SAFE = frozenset(
        {0x09, 0x0A, 0x0D}
        | set(range(0x20, 0x7F))
        | set(range(0x80, 0xFF))
    )
    if all(b in _TEXT_SAFE for b in sample):
        return "text/plain"

    return None


def detect_mime(data: bytes) -> str | None:
    """Sniff the MIME type of *data* via libmagic, falling back to a
    built-in pure-Python sniffer when libmagic is unavailable.

    Returns the detected MIME (e.g. ``application/pdf``) when
    identification succeeds; returns ``None`` only when the content
    cannot be recognised by either backend.

    Prefers libmagic for its breadth (hundreds of signatures) but the
    built-in fallback covers the most common binary formats and a
    text/binary heuristic so MIME-dependent features (allowlist, file
    upload type recording, data-URI conversion) work without requiring
    a C library.
    """
    global _magic_unavailable_logged
    try:
        magic_mod = _get_magic_module()
    except ImportError:
        if not _magic_unavailable_logged:
            logger.warning(
                "detect_mime: python-magic / libmagic not available; "
                "using built-in magic-byte sniffer. Install "
                "python-magic and libmagic for broader MIME coverage "
                "(fipsagents[files] + system libmagic).",
            )
            _magic_unavailable_logged = True
        return _sniff_builtin(data)
    try:
        return magic_mod.from_buffer(data)
    except Exception as exc:
        logger.warning("detect_mime: libmagic raised %s: %s", type(exc).__name__, exc)
        return _sniff_builtin(data)


def _get_magic_module():
    """Return a cached ``magic.Magic(mime=True)`` instance.

    Split out for test injection — callers can monkey-patch this to
    simulate libmagic being absent without uninstalling the library.
    """
    cached = getattr(_get_magic_module, "_cached", None)
    if cached is not None:
        return cached
    import magic  # type: ignore[import-not-found]

    m = magic.Magic(mime=True)
    _get_magic_module._cached = m  # type: ignore[attr-defined]
    return m


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


ParseStatus = Literal["pending", "processing", "completed", "failed", "skipped"]
ChunkStatus = Literal["pending", "processing", "completed", "failed", "skipped"]


@dataclass
class FileRecord:
    """A file uploaded to the agent.

    ``user_id`` mirrors :class:`FeedbackRecord` semantics: the
    gateway-issued ``X-Auth-Subject`` header value, or ``"anonymous"``
    when unauthenticated. ``session_id`` is optional — files can exist
    independently of a session.

    ``parse_status`` lifecycle:

    - ``pending``    — bytes uploaded, parsing not yet attempted (default)
    - ``processing`` — parse in flight
    - ``completed``  — ``extracted_text`` is populated
    - ``failed``     — ``parse_error`` is populated
    - ``skipped``    — file type intentionally not parsed (binary, unknown)

    ``chunk_status`` lifecycle (ADR-0002):

    - ``pending``    — chunking not yet attempted (default; also when
                       chunking is disabled in config)
    - ``processing`` — async chunking in flight after upload
    - ``completed``  — chunks written to ChunkStore; ``chunk_count > 0``
    - ``failed``     — chunking attempted but raised
    - ``skipped``    — file is below ``small_file_threshold_tokens`` and
                       takes the full-text path; ``chunk_count == 0``
    """

    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    user_id: str = "anonymous"
    session_id: str | None = None
    extracted_text: str | None = None
    parse_status: ParseStatus = "pending"
    parse_error: str | None = None
    chunk_status: ChunkStatus = "pending"
    chunk_count: int = 0
    created_at: str = field(default_factory=_utc_now_iso)
    deleted_at: str | None = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class FileStore(ABC):
    """Pluggable file persistence backend (metadata + bytes)."""

    @abstractmethod
    async def save(self, record: FileRecord, data: bytes) -> str:
        """Persist *record* metadata and *data* bytes atomically.

        ``record.size_bytes`` and ``record.sha256`` MUST match
        ``len(data)`` and the SHA-256 of *data*; the implementation may
        verify and raise ``ValueError`` on mismatch.

        Returns the ``file_id``.
        """

    @abstractmethod
    async def get_metadata(self, file_id: str) -> FileRecord | None:
        """Retrieve metadata. Returns None if not found or soft-deleted."""

    @abstractmethod
    async def get_bytes(self, file_id: str) -> bytes | None:
        """Retrieve raw bytes. Returns None if not found."""

    @abstractmethod
    async def get_extracted_text(self, file_id: str) -> str | None:
        """Retrieve extracted text (parser output). None if not parsed."""

    @abstractmethod
    async def update_extracted_text(
        self,
        file_id: str,
        *,
        extracted_text: str | None = None,
        parse_status: ParseStatus | None = None,
        parse_error: str | None = None,
    ) -> bool:
        """Update parse-result fields. Returns True if the file existed."""

    @abstractmethod
    async def update_chunk_status(
        self,
        file_id: str,
        *,
        chunk_status: ChunkStatus | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        """Update chunk-result fields (ADR-0002).

        Called by the server's async chunking task when it transitions
        a file from ``processing`` → ``completed`` (or ``failed``).
        Returns True when the file existed.
        """

    @abstractmethod
    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FileRecord]:
        """Return files attached to *session_id*, newest first."""

    @abstractmethod
    async def delete(self, file_id: str) -> bool:
        """Remove file metadata and bytes. Returns True if it existed."""

    @abstractmethod
    async def delete_before(self, cutoff: datetime) -> int:
        """Delete files created before *cutoff*. Returns count deleted."""

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


# ---------------------------------------------------------------------------
# Null backend
# ---------------------------------------------------------------------------


class NullFileStore(FileStore):
    """No persistence — uploads are accepted but immediately discarded."""

    async def save(self, record: FileRecord, data: bytes) -> str:
        logger.debug("NullFileStore: discarded %s (%d bytes)", record.file_id, len(data))
        return record.file_id

    async def get_metadata(self, file_id: str) -> FileRecord | None:
        return None

    async def get_bytes(self, file_id: str) -> bytes | None:
        return None

    async def get_extracted_text(self, file_id: str) -> str | None:
        return None

    async def update_extracted_text(
        self,
        file_id: str,
        *,
        extracted_text: str | None = None,
        parse_status: ParseStatus | None = None,
        parse_error: str | None = None,
    ) -> bool:
        return False

    async def update_chunk_status(
        self,
        file_id: str,
        *,
        chunk_status: ChunkStatus | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        return False

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FileRecord]:
        return []

    async def delete(self, file_id: str) -> bool:
        return False

    async def delete_before(self, cutoff: datetime) -> int:
        return 0


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SqliteFileStore(FileStore):
    """SQLite metadata + sharded local-filesystem bytes.

    Suitable for development and single-replica edge deployments. For
    production, pair Postgres metadata with an S3-compatible bytes
    backend (MinIO).
    """

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS files (
    file_id          TEXT PRIMARY KEY,
    session_id       TEXT,
    user_id          TEXT NOT NULL DEFAULT 'anonymous',
    filename         TEXT NOT NULL,
    mime_type        TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    extracted_text   TEXT,
    parse_status     TEXT NOT NULL DEFAULT 'pending',
    parse_error      TEXT,
    chunk_status     TEXT NOT NULL DEFAULT 'pending',
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    bytes_path       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    deleted_at       TEXT
)"""
    _CREATE_INDEX_SESSION = (
        "CREATE INDEX IF NOT EXISTS idx_files_session ON files (session_id)"
    )
    _CREATE_INDEX_CREATED = (
        "CREATE INDEX IF NOT EXISTS idx_files_created ON files (created_at)"
    )
    # ADR-0002: chunk_status / chunk_count are added via ALTER for tables
    # created on 0.16.0 / 0.17.0.  SQLite has no IF NOT EXISTS for ADD
    # COLUMN, so we probe with PRAGMA table_info before applying.
    _MIGRATIONS = (
        ("chunk_status",
         "ALTER TABLE files ADD COLUMN chunk_status TEXT NOT NULL "
         "DEFAULT 'pending'"),
        ("chunk_count",
         "ALTER TABLE files ADD COLUMN chunk_count INTEGER NOT NULL "
         "DEFAULT 0"),
    )

    def __init__(
        self,
        db_path: str = "./agent.db",
        *,
        bytes_dir: str = "./files",
        bytes_store: BytesStore | None = None,
        connection: Any = None,
    ) -> None:
        self._db_path = db_path
        self._bytes_dir = bytes_dir
        # When the caller passes their own bytes_store we don't own
        # its lifecycle. When we synthesize one from bytes_dir we close
        # it on shutdown.
        if bytes_store is None:
            self._bytes_store: BytesStore = LocalFsBytesStore(bytes_dir)
            self._owns_bytes_store = True
        else:
            self._bytes_store = bytes_store
            self._owns_bytes_store = False
        self._db: Any = connection
        self._managed = connection is not None
        self._initialized = False

    async def _get_db(self) -> Any:
        if self._db is None:
            import aiosqlite

            self._db = await aiosqlite.connect(self._db_path)
        if not self._initialized:
            await self._ensure_schema()
        return self._db

    async def _ensure_schema(self) -> None:
        db = self._db
        await db.execute(self._CREATE_TABLE)
        await db.execute(self._CREATE_INDEX_SESSION)
        await db.execute(self._CREATE_INDEX_CREATED)
        # Migrate older tables to the ADR-0002 columns when missing.
        cursor = await db.execute("PRAGMA table_info(files)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column, ddl in self._MIGRATIONS:
            if column not in existing:
                await db.execute(ddl)
        await db.commit()
        self._initialized = True

    async def save(self, record: FileRecord, data: bytes) -> str:
        if record.size_bytes != len(data):
            raise ValueError(
                f"size_bytes mismatch: record says {record.size_bytes}, "
                f"data is {len(data)} bytes"
            )
        actual_sha = _sha256(data)
        if record.sha256 and record.sha256 != actual_sha:
            raise ValueError(
                f"sha256 mismatch for {record.file_id}: "
                f"record says {record.sha256}, data hashes to {actual_sha}"
            )
        # Trust caller-provided sha256 if present, else fill it in.
        sha = record.sha256 or actual_sha

        # Write bytes first; if metadata insert fails we have an
        # orphan in BytesStore (mitigated by housekeeping). The reverse
        # would orphan the metadata pointing at nothing.
        await self._bytes_store.put(
            record.file_id, data, content_type=record.mime_type,
        )

        # bytes_path is vestigial post-ADR-0001 — kept NOT NULL so old
        # rows don't break; populated with file_id so the column has
        # something meaningful when humans inspect the table.
        db = await self._get_db()
        await db.execute(
            "INSERT INTO files ("
            "  file_id, session_id, user_id, filename, mime_type, "
            "  size_bytes, sha256, extracted_text, parse_status, "
            "  parse_error, chunk_status, chunk_count, bytes_path, "
            "  created_at, deleted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.file_id,
                record.session_id,
                record.user_id,
                record.filename,
                record.mime_type,
                record.size_bytes,
                sha,
                record.extracted_text,
                record.parse_status,
                record.parse_error,
                record.chunk_status,
                record.chunk_count,
                record.file_id,
                record.created_at,
                record.deleted_at,
            ),
        )
        await db.commit()
        logger.debug(
            "SqliteFileStore: saved %s (%d bytes, sha %s..)",
            record.file_id, record.size_bytes, sha[:8],
        )
        return record.file_id

    async def get_metadata(self, file_id: str) -> FileRecord | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT file_id, session_id, user_id, filename, mime_type, "
            "       size_bytes, sha256, extracted_text, parse_status, "
            "       parse_error, chunk_status, chunk_count, "
            "       created_at, deleted_at "
            "FROM files WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return FileRecord(
            file_id=row[0],
            session_id=row[1],
            user_id=row[2],
            filename=row[3],
            mime_type=row[4],
            size_bytes=row[5],
            sha256=row[6],
            extracted_text=row[7],
            parse_status=row[8],
            parse_error=row[9],
            chunk_status=row[10],
            chunk_count=row[11],
            created_at=row[12],
            deleted_at=row[13],
        )

    async def get_bytes(self, file_id: str) -> bytes | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT 1 FROM files "
            "WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = await self._bytes_store.get(file_id)
        if data is None:
            logger.warning(
                "SqliteFileStore: metadata for %s exists but bytes "
                "missing in BytesStore",
                file_id,
            )
        return data

    async def get_extracted_text(self, file_id: str) -> str | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT extracted_text FROM files "
            "WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row[0]

    async def update_extracted_text(
        self,
        file_id: str,
        *,
        extracted_text: str | None = None,
        parse_status: ParseStatus | None = None,
        parse_error: str | None = None,
    ) -> bool:
        if extracted_text is None and parse_status is None and parse_error is None:
            return await self._exists(file_id)

        sets: list[str] = []
        params: list[Any] = []
        if extracted_text is not None:
            sets.append("extracted_text = ?")
            params.append(extracted_text)
        if parse_status is not None:
            sets.append("parse_status = ?")
            params.append(parse_status)
        if parse_error is not None:
            sets.append("parse_error = ?")
            params.append(parse_error)
        params.append(file_id)

        db = await self._get_db()
        cursor = await db.execute(
            f"UPDATE files SET {', '.join(sets)} "
            "WHERE file_id = ? AND deleted_at IS NULL",
            tuple(params),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def _exists(self, file_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT 1 FROM files WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        return await cursor.fetchone() is not None

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FileRecord]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT file_id, session_id, user_id, filename, mime_type, "
            "       size_bytes, sha256, extracted_text, parse_status, "
            "       parse_error, chunk_status, chunk_count, "
            "       created_at, deleted_at "
            "FROM files "
            "WHERE session_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC "
            "LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [
            FileRecord(
                file_id=r[0],
                session_id=r[1],
                user_id=r[2],
                filename=r[3],
                mime_type=r[4],
                size_bytes=r[5],
                sha256=r[6],
                extracted_text=r[7],
                parse_status=r[8],
                parse_error=r[9],
                chunk_status=r[10],
                chunk_count=r[11],
                created_at=r[12],
                deleted_at=r[13],
            )
            for r in rows
        ]

    async def update_chunk_status(
        self,
        file_id: str,
        *,
        chunk_status: ChunkStatus | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        if chunk_status is None and chunk_count is None:
            return await self._exists(file_id)

        sets: list[str] = []
        params: list[Any] = []
        if chunk_status is not None:
            sets.append("chunk_status = ?")
            params.append(chunk_status)
        if chunk_count is not None:
            sets.append("chunk_count = ?")
            params.append(chunk_count)
        params.append(file_id)

        db = await self._get_db()
        cursor = await db.execute(
            f"UPDATE files SET {', '.join(sets)} "
            "WHERE file_id = ? AND deleted_at IS NULL",
            tuple(params),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def delete(self, file_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT 1 FROM files "
            "WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        await db.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        await db.commit()
        # Best-effort bytes delete; if the bytes are already gone we
        # still consider the metadata-side delete a success.
        try:
            await self._bytes_store.delete(file_id)
        except Exception:
            logger.warning(
                "SqliteFileStore: bytes_store.delete failed for %s",
                file_id, exc_info=True,
            )
        return True

    async def delete_before(self, cutoff: datetime) -> int:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT file_id FROM files WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        for (fid,) in rows:
            try:
                await self._bytes_store.delete(fid)
            except Exception:
                logger.warning(
                    "SqliteFileStore: housekeeping bytes_store.delete failed "
                    "for %s", fid, exc_info=True,
                )
        await db.execute(
            "DELETE FROM files WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        await db.commit()
        deleted = len(rows)
        if deleted:
            logger.debug(
                "SqliteFileStore: housekeeping removed %d files", deleted,
            )
        return deleted

    async def close(self) -> None:
        if self._db is not None and not self._managed:
            await self._db.close()
            self._db = None
            self._initialized = False
        if self._owns_bytes_store:
            await self._bytes_store.close()


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------


class PostgresFileStore(FileStore):
    """Enterprise file persistence — Postgres metadata + local-FS bytes.

    Mirrors :class:`PostgresSessionStore` exactly: asyncpg pool managed
    lazily, schema created on first access, ``IF NOT EXISTS`` everywhere
    so re-runs are safe. The bytes layout is identical to
    :class:`SqliteFileStore` — sharded local filesystem under
    ``bytes_dir`` — because the S3-compatible bytes backend is a
    follow-up PR. Production deployments using Postgres should mount
    ``bytes_dir`` on a PVC sized for the expected upload volume.
    """

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS files (
    file_id          TEXT PRIMARY KEY,
    session_id       TEXT,
    user_id          TEXT NOT NULL DEFAULT 'anonymous',
    filename         TEXT NOT NULL,
    mime_type        TEXT NOT NULL,
    size_bytes       BIGINT NOT NULL,
    sha256           TEXT NOT NULL,
    extracted_text   TEXT,
    parse_status     TEXT NOT NULL DEFAULT 'pending',
    parse_error      TEXT,
    chunk_status     TEXT NOT NULL DEFAULT 'pending',
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    bytes_path       TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ
)"""
    _CREATE_INDEX_SESSION = (
        "CREATE INDEX IF NOT EXISTS idx_files_session ON files (session_id)"
    )
    _CREATE_INDEX_CREATED = (
        "CREATE INDEX IF NOT EXISTS idx_files_created ON files (created_at)"
    )
    # ADR-0002: idempotent migrations for tables created on 0.16.0/0.17.0.
    _MIGRATIONS = (
        "ALTER TABLE files ADD COLUMN IF NOT EXISTS chunk_status TEXT "
        "NOT NULL DEFAULT 'pending'",
        "ALTER TABLE files ADD COLUMN IF NOT EXISTS chunk_count INTEGER "
        "NOT NULL DEFAULT 0",
    )

    def __init__(
        self,
        database_url: str,
        *,
        bytes_dir: str = "./files",
        bytes_store: BytesStore | None = None,
    ) -> None:
        self._database_url = database_url
        self._bytes_dir = bytes_dir
        if bytes_store is None:
            self._bytes_store: BytesStore = LocalFsBytesStore(bytes_dir)
            self._owns_bytes_store = True
        else:
            self._bytes_store = bytes_store
            self._owns_bytes_store = False
        self._pool: Any = None  # asyncpg.Pool
        self._initialized = False

    async def _get_pool(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._database_url)
        if not self._initialized:
            await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        pool = self._pool
        async with pool.acquire() as conn:
            await conn.execute(self._CREATE_TABLE)
            await conn.execute(self._CREATE_INDEX_SESSION)
            await conn.execute(self._CREATE_INDEX_CREATED)
            for stmt in self._MIGRATIONS:
                await conn.execute(stmt)
        self._initialized = True

    async def save(self, record: FileRecord, data: bytes) -> str:
        if record.size_bytes != len(data):
            raise ValueError(
                f"size_bytes mismatch: record says {record.size_bytes}, "
                f"data is {len(data)} bytes"
            )
        actual_sha = _sha256(data)
        if record.sha256 and record.sha256 != actual_sha:
            raise ValueError(
                f"sha256 mismatch for {record.file_id}: "
                f"record says {record.sha256}, data hashes to {actual_sha}"
            )
        sha = record.sha256 or actual_sha

        # Bytes first; orphan bytes on metadata-insert failure are
        # mitigated by housekeeping. Reverse order would orphan
        # metadata pointing at nothing.
        await self._bytes_store.put(
            record.file_id, data, content_type=record.mime_type,
        )

        # FileRecord.created_at is a string for SQLite-friendly storage;
        # parse it back to a datetime for TIMESTAMPTZ. Default to now()
        # when the field is empty (tests that build a record without
        # touching the default factory).
        created_at = _parse_iso(record.created_at) or datetime.now(timezone.utc)
        deleted_at = _parse_iso(record.deleted_at)

        # bytes_path is vestigial post-ADR-0001; populated with file_id
        # so the column has something meaningful when humans inspect.
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO files ("
                "  file_id, session_id, user_id, filename, mime_type, "
                "  size_bytes, sha256, extracted_text, parse_status, "
                "  parse_error, chunk_status, chunk_count, bytes_path, "
                "  created_at, deleted_at"
                ") VALUES "
                "($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, "
                " $13, $14, $15)",
                record.file_id,
                record.session_id,
                record.user_id,
                record.filename,
                record.mime_type,
                record.size_bytes,
                sha,
                record.extracted_text,
                record.parse_status,
                record.parse_error,
                record.chunk_status,
                record.chunk_count,
                record.file_id,
                created_at,
                deleted_at,
            )
        logger.debug(
            "PostgresFileStore: saved %s (%d bytes, sha %s..)",
            record.file_id, record.size_bytes, sha[:8],
        )
        return record.file_id

    async def get_metadata(self, file_id: str) -> FileRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id, session_id, user_id, filename, mime_type, "
                "       size_bytes, sha256, extracted_text, parse_status, "
                "       parse_error, chunk_status, chunk_count, "
                "       created_at, deleted_at "
                "FROM files WHERE file_id = $1 AND deleted_at IS NULL",
                file_id,
            )
        if row is None:
            return None
        return FileRecord(
            file_id=row["file_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            extracted_text=row["extracted_text"],
            parse_status=row["parse_status"],
            parse_error=row["parse_error"],
            chunk_status=row["chunk_status"],
            chunk_count=row["chunk_count"],
            created_at=_iso(row["created_at"]),
            deleted_at=_iso(row["deleted_at"]),
        )

    async def get_bytes(self, file_id: str) -> bytes | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM files "
                "WHERE file_id = $1 AND deleted_at IS NULL",
                file_id,
            )
        if row is None:
            return None
        data = await self._bytes_store.get(file_id)
        if data is None:
            logger.warning(
                "PostgresFileStore: metadata for %s exists but bytes "
                "missing in BytesStore",
                file_id,
            )
        return data

    async def get_extracted_text(self, file_id: str) -> str | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT extracted_text FROM files "
                "WHERE file_id = $1 AND deleted_at IS NULL",
                file_id,
            )
        if row is None:
            return None
        return row["extracted_text"]

    async def update_extracted_text(
        self,
        file_id: str,
        *,
        extracted_text: str | None = None,
        parse_status: ParseStatus | None = None,
        parse_error: str | None = None,
    ) -> bool:
        if extracted_text is None and parse_status is None and parse_error is None:
            return await self._exists(file_id)

        # Build SET clauses with positional placeholders ($2, $3, ...).
        sets: list[str] = []
        params: list[Any] = []
        if extracted_text is not None:
            sets.append(f"extracted_text = ${len(params) + 2}")
            params.append(extracted_text)
        if parse_status is not None:
            sets.append(f"parse_status = ${len(params) + 2}")
            params.append(parse_status)
        if parse_error is not None:
            sets.append(f"parse_error = ${len(params) + 2}")
            params.append(parse_error)

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE files SET {', '.join(sets)} "
                "WHERE file_id = $1 AND deleted_at IS NULL",
                file_id, *params,
            )
        return not result.endswith("0")

    async def _exists(self, file_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM files WHERE file_id = $1 AND deleted_at IS NULL",
                file_id,
            )
        return row is not None

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FileRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT file_id, session_id, user_id, filename, mime_type, "
                "       size_bytes, sha256, extracted_text, parse_status, "
                "       parse_error, chunk_status, chunk_count, "
                "       created_at, deleted_at "
                "FROM files "
                "WHERE session_id = $1 AND deleted_at IS NULL "
                "ORDER BY created_at DESC "
                "LIMIT $2 OFFSET $3",
                session_id, limit, offset,
            )
        return [
            FileRecord(
                file_id=r["file_id"],
                session_id=r["session_id"],
                user_id=r["user_id"],
                filename=r["filename"],
                mime_type=r["mime_type"],
                size_bytes=r["size_bytes"],
                sha256=r["sha256"],
                extracted_text=r["extracted_text"],
                parse_status=r["parse_status"],
                parse_error=r["parse_error"],
                chunk_status=r["chunk_status"],
                chunk_count=r["chunk_count"],
                created_at=_iso(r["created_at"]),
                deleted_at=_iso(r["deleted_at"]),
            )
            for r in rows
        ]

    async def update_chunk_status(
        self,
        file_id: str,
        *,
        chunk_status: ChunkStatus | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        if chunk_status is None and chunk_count is None:
            return await self._exists(file_id)

        sets: list[str] = []
        params: list[Any] = []
        if chunk_status is not None:
            sets.append(f"chunk_status = ${len(params) + 2}")
            params.append(chunk_status)
        if chunk_count is not None:
            sets.append(f"chunk_count = ${len(params) + 2}")
            params.append(chunk_count)

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE files SET {', '.join(sets)} "
                "WHERE file_id = $1 AND deleted_at IS NULL",
                file_id, *params,
            )
        return not result.endswith("0")

    async def delete(self, file_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM files "
                "WHERE file_id = $1 AND deleted_at IS NULL",
                file_id,
            )
            if row is None:
                return False
            await conn.execute(
                "DELETE FROM files WHERE file_id = $1",
                file_id,
            )
        try:
            await self._bytes_store.delete(file_id)
        except Exception:
            logger.warning(
                "PostgresFileStore: bytes_store.delete failed for %s",
                file_id, exc_info=True,
            )
        return True

    async def delete_before(self, cutoff: datetime) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT file_id FROM files WHERE created_at < $1",
                cutoff,
            )
            if not rows:
                return 0
            for r in rows:
                try:
                    await self._bytes_store.delete(r["file_id"])
                except Exception:
                    logger.warning(
                        "PostgresFileStore: housekeeping bytes_store.delete "
                        "failed for %s", r["file_id"], exc_info=True,
                    )
            await conn.execute(
                "DELETE FROM files WHERE created_at < $1",
                cutoff,
            )
        deleted = len(rows)
        if deleted:
            logger.debug(
                "PostgresFileStore: housekeeping removed %d files", deleted,
            )
        return deleted

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False
        if self._owns_bytes_store:
            await self._bytes_store.close()
            self._pool = None
            self._initialized = False


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a FileRecord ISO timestamp string into a UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    """Render a Postgres TIMESTAMPTZ back to FileRecord's ISO string."""
    if value is None:
        return None
    return value.isoformat()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_file_store(
    backend: str | None,
    *,
    sqlite_path: str = "./agent.db",
    database_url: str = "",
    bytes_dir: str = "./files",
    bytes_store: BytesStore | None = None,
    sqlite_connection: Any = None,
) -> FileStore:
    """Create a file store from config values.

    Supported metadata backends:
      - ``sqlite``   — :class:`SqliteFileStore` (single-replica, dev / edge)
      - ``postgres`` — :class:`PostgresFileStore` (metadata in PG)
      - ``http``     — platform-routed (not yet implemented)
      - ``None``     — :class:`NullFileStore` (accepted-then-discarded)

    Bytes storage is pluggable via *bytes_store* (per ADR-0001). When
    ``None``, a :class:`LocalFsBytesStore` is constructed from
    *bytes_dir* — backward-compatible with 0.16.0 deployments. Wire an
    :class:`S3BytesStore` for multi-replica or production deployments
    that need object storage; see :func:`create_bytes_store`.
    """
    if backend == "sqlite":
        return SqliteFileStore(
            sqlite_path,
            bytes_dir=bytes_dir,
            bytes_store=bytes_store,
            connection=sqlite_connection,
        )
    if backend == "postgres":
        if not database_url:
            raise ValueError("PostgresFileStore requires database_url")
        return PostgresFileStore(
            database_url,
            bytes_dir=bytes_dir,
            bytes_store=bytes_store,
        )
    if backend == "http":
        raise NotImplementedError(
            "FileStore backend 'http' is not yet implemented; "
            "use 'sqlite', 'postgres', or leave unset (Null)."
        )
    return NullFileStore()
