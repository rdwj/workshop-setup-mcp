"""Markdown memory backend.

Human-readable, git-friendly memory for a single agent. Two modes,
selected by the config file:

- **Level 1 — single file.** One compound markdown document. Each memory
  is a ``## <heading>`` section. The Karpathy LLM Wiki pattern.
- **Level 2 — directory.** One ``.md`` file per topic. The filename (without
  extension) is the memory id. Use when you have multiple functional areas
  (standing instructions vs project memories) you want to curate separately.

The primary use case is "I want my agent's memory to be a file I can read,
edit, and commit to git." For search ranking, concurrent writes, or
per-entry timestamps, use the sqlite backend.

Design notes:

- ``search(query="")`` returns every section/file in stable file order as
  separate results. This is the recommended retrieval mode — agents should
  load the entire memory once at session start and inject it as a stable
  prefix, preserving prefix-cache hits across turns. Passing a non-empty
  query does a case-insensitive substring filter; no ranking.
- ``write`` appends. Memory IDs are section headings (Level 1) or filenames
  (Level 2). If no ``memory_id`` kwarg is supplied, a timestamp is used
  automatically. Caller is responsible for heading/filename uniqueness —
  duplicate headings are allowed but ``update()`` only touches the first
  match.
- ``update`` rewrites the body of a section (Level 1) or the contents of a
  file (Level 2) identified by ``memory_id``. Missing ids log a warning
  and return ``None``.
- ``report_contradiction`` is a log-only no-op; the human curates the file.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Matches safe characters for Level-2 filenames. Rejects path separators,
# whitespace, and anything that would complicate filesystem interaction.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        return None


def _parse_sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown doc into ``(heading, body)`` pairs.

    Sections start at ``## <heading>`` lines at column 0. Anything before
    the first such line is discarded. Body text is stripped of trailing
    whitespace but inner blank lines are preserved.
    """
    parts = re.split(r"^## (.+?)\s*$", text, flags=re.MULTILINE)
    # parts[0] is any content before the first heading — ignored.
    # Subsequent entries alternate: heading, body, heading, body, ...
    sections: list[tuple[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip()
        body = parts[i + 1].strip("\n")
        sections.append((heading, body))
    return sections


def _safe_filename(memory_id: str) -> str:
    """Return *memory_id* if it's a safe flat filename, else raise."""
    if not _SAFE_FILENAME_RE.match(memory_id):
        raise ValueError(
            f"Invalid memory_id for markdown filename: {memory_id!r}. "
            "Use only letters, digits, '.', '_', and '-'."
        )
    return memory_id


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MarkdownMemoryClient(MemoryClientBase):
    """Markdown-backed memory — single file or directory of files.

    Exactly one of *file* or *dir* must be provided. The other is ``None``.
    """

    def __init__(
        self,
        *,
        file: Path | None = None,
        dir: Path | None = None,
    ) -> None:
        if (file is None) == (dir is None):
            raise ValueError(
                "MarkdownMemoryClient requires exactly one of "
                "`file` or `dir`."
            )
        self._file = file
        self._dir = dir

    # -- Search -------------------------------------------------------------

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._search_sync, query)
        except Exception:
            logger.warning(
                "Markdown search failed for query %r", query, exc_info=True
            )
            return []

    def _search_sync(self, query: str) -> list[dict[str, Any]]:
        results = self._load_all()
        if query:
            needle = query.lower()
            results = [r for r in results if needle in r["content"].lower()]
        return results

    def _load_all(self) -> list[dict[str, Any]]:
        """Return every section/file in stable order.

        For Level 1, parses the single markdown file into sections.
        For Level 2, reads each ``*.md`` file in ``sorted()`` filename order
        and returns one result per file.
        """
        if self._file is not None:
            if not self._file.exists():
                return []
            text = self._file.read_text(encoding="utf-8")
            updated_at = _file_mtime_iso(self._file)
            return [
                {
                    "id": heading,
                    "content": body,
                    "created_at": None,
                    "updated_at": updated_at,
                }
                for heading, body in _parse_sections(text)
            ]

        assert self._dir is not None
        if not self._dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.md")):
            try:
                content = path.read_text(encoding="utf-8").strip("\n")
            except OSError:
                continue
            results.append(
                {
                    "id": path.stem,
                    "content": content,
                    "created_at": None,
                    "updated_at": _file_mtime_iso(path),
                }
            )
        return results

    # -- Write --------------------------------------------------------------

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        memory_id = kwargs.get("memory_id")
        try:
            return await asyncio.to_thread(self._write_sync, content, memory_id)
        except Exception:
            logger.warning(
                "Markdown write failed — memory not persisted", exc_info=True
            )
            return None

    def _write_sync(
        self, content: str, memory_id: str | None
    ) -> dict[str, Any]:
        if memory_id is None:
            memory_id = _now_iso()

        body = content.strip("\n")
        created_at = _now_iso()

        if self._file is not None:
            # Level 1: append a new section. Ensure there's at least one
            # blank line between the existing doc and the new section so
            # the heading renders cleanly.
            existing = (
                self._file.read_text(encoding="utf-8") if self._file.exists() else ""
            )
            separator = "" if not existing or existing.endswith("\n\n") else (
                "\n" if existing.endswith("\n") else "\n\n"
            )
            new_block = f"{separator}## {memory_id}\n\n{body}\n"
            with self._file.open("a", encoding="utf-8") as f:
                f.write(new_block)
        else:
            assert self._dir is not None
            safe = _safe_filename(memory_id)
            path = self._dir / f"{safe}.md"
            path.write_text(f"{body}\n", encoding="utf-8")

        return {
            "id": memory_id,
            "content": content,
            "created_at": created_at,
        }

    # -- Update -------------------------------------------------------------

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._update_sync, memory_id, content)
        except Exception:
            logger.warning(
                "Markdown update failed for memory %r — not persisted",
                memory_id,
                exc_info=True,
            )
            return None

    def _update_sync(
        self, memory_id: str, content: str
    ) -> dict[str, Any] | None:
        body = content.strip("\n")

        if self._file is not None:
            if not self._file.exists():
                logger.warning(
                    "Markdown update: file %s does not exist", self._file
                )
                return None
            text = self._file.read_text(encoding="utf-8")
            # Replace the first matching ``## <memory_id>`` block with a new
            # body. The replacement covers from the heading line through
            # (but not including) the next ``##`` heading, or end-of-file.
            pattern = re.compile(
                rf"(^## {re.escape(memory_id)}\s*\n)"
                rf"(?:.*?)"
                rf"(?=^## |\Z)",
                re.MULTILINE | re.DOTALL,
            )
            new_text, n = pattern.subn(
                lambda m: f"{m.group(1)}\n{body}\n\n", text, count=1
            )
            if n == 0:
                logger.warning(
                    "Markdown update: section %r not found in %s",
                    memory_id, self._file,
                )
                return None
            self._file.write_text(new_text, encoding="utf-8")
        else:
            assert self._dir is not None
            try:
                safe = _safe_filename(memory_id)
            except ValueError:
                logger.warning("Markdown update: invalid memory_id %r", memory_id)
                return None
            path = self._dir / f"{safe}.md"
            if not path.exists():
                logger.warning("Markdown update: file %s not found", path)
                return None
            path.write_text(f"{body}\n", encoding="utf-8")

        return {
            "id": memory_id,
            "content": content,
            "updated_at": _now_iso(),
        }

    # -- Contradictions -----------------------------------------------------

    async def report_contradiction(
        self, memory_id: str, description: str
    ) -> None:
        logger.warning(
            "Contradiction reported for memory %r: %s "
            "(markdown backend is human-curated; edit the file to resolve)",
            memory_id, description,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def create_markdown_client(config_path: Path) -> MemoryClientBase:
    """Build a ``MarkdownMemoryClient`` from ``.memory-markdown.yaml``.

    Config schema (exactly one of ``file`` or ``dir`` required):

    .. code-block:: yaml

        # Level 1 — single compound doc
        file: ./agent-memory.md

    .. code-block:: yaml

        # Level 2 — directory of topic files
        dir: ./memories

    Relative paths resolve against the config file's directory. Missing
    files/directories are created empty. Returns ``NullMemoryClient`` on
    any error so the agent always gets a usable client.
    """
    try:
        import yaml

        if not config_path.exists():
            logger.debug(
                "No markdown memory config at %s — memory integration disabled",
                config_path,
            )
            return NullMemoryClient()

        raw = config_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(raw) or {}

        file_raw = cfg.get("file")
        dir_raw = cfg.get("dir")
        if (file_raw is None) == (dir_raw is None):
            logger.error(
                "Markdown memory config %s must specify exactly one of "
                "`file` or `dir` — falling back to NullMemoryClient",
                config_path,
            )
            return NullMemoryClient()

        base = config_path.parent
        if file_raw is not None:
            target = Path(file_raw)
            if not target.is_absolute():
                target = base / target
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
            logger.info(
                "Markdown memory backend enabled (file: %s)", target
            )
            return MarkdownMemoryClient(file=target)

        assert dir_raw is not None
        target = Path(dir_raw)
        if not target.is_absolute():
            target = base / target
        target.mkdir(parents=True, exist_ok=True)
        logger.info("Markdown memory backend enabled (dir: %s)", target)
        return MarkdownMemoryClient(dir=target)

    except Exception:
        logger.warning(
            "Failed to initialise markdown memory backend from %s — "
            "falling back to NullMemoryClient",
            config_path,
            exc_info=True,
        )
        return NullMemoryClient()
