"""Chunkers for ``/v1/files`` extracted text.

Splits a long ``extracted_text`` into retrievable units before they are
embedded and written to a :class:`ChunkStore` (added in PR-B). The
chunker is strictly text-in / chunks-out — no embeddings, no I/O.

Two-tier dispatch by parser output, mirroring :mod:`fipsagents.server.parser`:

- ``RecursiveTokenChunker`` (default, no extra deps) — splits on
  paragraph boundaries first, then sentences, then hard-cuts at the
  token cap. Used for plaintext outputs and for Docling outputs that
  lack hierarchy.
- ``DoclingChunker`` (PR-D, opt-in via ``[files]`` extra) — uses
  Docling's native ``HybridChunker`` so chunk boundaries snap to
  headings, page breaks, and table cells.

Token counting uses ``tiktoken`` when importable and falls back to a
``len(text) // 4`` heuristic otherwise. Per ADR-0002, tiktoken is a
soft dependency: FIPS builds prefer fewer Rust extensions and the
heuristic is within ±20% for English-like text — good enough for
chunk-boundary decisions.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


_TIKTOKEN_ENCODING: Any | None = None
_TIKTOKEN_PROBED: bool = False


def _get_tiktoken_encoding() -> Any | None:
    """Return a cached ``cl100k_base`` encoding, or ``None`` if unavailable.

    Probes once and caches the result. ``cl100k_base`` is the encoding
    used by the OpenAI ``gpt-4`` / ``gpt-3.5-turbo`` family and produces
    counts within a few percent of most modern open-weight tokenizers
    for English text. It is the right "generic" choice when we do not
    know which model the deployment will route to.
    """
    global _TIKTOKEN_ENCODING, _TIKTOKEN_PROBED
    if _TIKTOKEN_PROBED:
        return _TIKTOKEN_ENCODING
    _TIKTOKEN_PROBED = True
    try:
        import tiktoken
    except ImportError:
        logger.info(
            "tiktoken not installed; using char/4 heuristic for chunk "
            "token counts. Install with: pip install tiktoken"
        )
        _TIKTOKEN_ENCODING = None
        return None
    try:
        _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # pragma: no cover — extremely rare
        logger.warning("tiktoken get_encoding failed: %s; using fallback", exc)
        _TIKTOKEN_ENCODING = None
    return _TIKTOKEN_ENCODING


def count_tokens(text: str) -> int:
    """Count tokens in *text*.

    Uses ``tiktoken`` when available, otherwise ``len(text) // 4`` as a
    rough English-language heuristic. The fallback systematically
    under-counts for CJK / token-dense scripts; that is acceptable for
    chunk-boundary decisions because it errs on the side of smaller
    chunks (more retrieval granularity, not fewer).
    """
    if not text:
        return 0
    enc = _get_tiktoken_encoding()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)


def _reset_tiktoken_cache_for_tests() -> None:
    """Reset the tiktoken probe cache. Test-only hook."""
    global _TIKTOKEN_ENCODING, _TIKTOKEN_PROBED
    _TIKTOKEN_ENCODING = None
    _TIKTOKEN_PROBED = False


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """A single retrievable unit produced by a :class:`Chunker`.

    ``content`` is what gets embedded and surfaced at retrieval time.
    ``metadata`` is opaque to the chunker contract and is reserved for
    parser-supplied breadcrumbs (page number, section path, table id,
    etc.) once :class:`DoclingChunker` lands in PR-D.
    """

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Chunker(ABC):
    """Pluggable text-to-chunks splitter."""

    @abstractmethod
    async def chunk(
        self,
        text: str,
        *,
        chunk_size_tokens: int = 600,
        chunk_overlap_tokens: int = 100,
    ) -> list[Chunk]:
        """Split *text* into chunks of at most ``chunk_size_tokens``.

        ``chunk_overlap_tokens`` is the target overlap between
        consecutive chunks; implementations are free to round to the
        nearest sensible boundary (sentence, paragraph) rather than
        slicing mid-token.
        """


# ---------------------------------------------------------------------------
# Recursive splitter helpers
# ---------------------------------------------------------------------------


# Paragraph break: one or more blank lines, with optional whitespace.
_PARAGRAPH_RE = re.compile(r"\n\s*\n+")

# Sentence break: terminator (. ! ?) followed by whitespace and a
# capital / digit / opening quote. Conservative — does not try to
# handle abbreviations, since over-splitting is harmless (chunks just
# get glued back by the size-bounded greedy assembler) but
# under-splitting can produce 2 K-token "sentences" that cannot be
# bounded.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in _PARAGRAPH_RE.split(text)]
    return [p for p in parts if p]


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_RE.split(text)]
    return [s for s in parts if s]


def _hard_split_by_tokens(text: str, max_tokens: int) -> list[str]:
    """Last-resort splitter for paragraphs/sentences that exceed the cap.

    Splits on whitespace-bounded words to avoid breaking inside a token
    when tiktoken is in use. If the result still over-shoots (extremely
    long single word), accepts the over-shoot rather than slicing into
    UTF-8 codepoints — chunks slightly above the cap are fine; chunks
    that mangle text are not.
    """
    words = text.split()
    if not words:
        return []
    out: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        word_tokens = count_tokens(word + " ")
        if current and current_tokens + word_tokens > max_tokens:
            out.append(" ".join(current))
            current = [word]
            current_tokens = word_tokens
        else:
            current.append(word)
            current_tokens += word_tokens
    if current:
        out.append(" ".join(current))
    return out


def _recursive_units(text: str, max_tokens: int) -> list[str]:
    """Break *text* into units no larger than ``max_tokens``.

    Tries paragraphs first, then sentences, then a hard word-bounded
    split. Each unit is below the cap (or as close as possible without
    slicing mid-word).
    """
    if count_tokens(text) <= max_tokens:
        return [text]

    units: list[str] = []
    paragraphs = _split_paragraphs(text) or [text]
    for para in paragraphs:
        if count_tokens(para) <= max_tokens:
            units.append(para)
            continue
        sentences = _split_sentences(para) or [para]
        for sent in sentences:
            if count_tokens(sent) <= max_tokens:
                units.append(sent)
            else:
                units.extend(_hard_split_by_tokens(sent, max_tokens))
    return units


def _greedy_assemble(
    units: list[str],
    *,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> list[Chunk]:
    """Greedy-pack *units* into chunks of at most ``chunk_size_tokens``.

    Adds ``chunk_overlap_tokens`` of trailing context from the previous
    chunk to the start of each new chunk (rounded to whole units). The
    overlap helps retrieval recall when a relevant span straddles a
    chunk boundary.
    """
    if not units:
        return []
    if chunk_overlap_tokens >= chunk_size_tokens:
        # Overlap larger than the chunk cap collapses to no overlap;
        # otherwise consecutive chunks would be near-duplicates.
        chunk_overlap_tokens = 0

    chunks: list[Chunk] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)
        if current and current_tokens + unit_tokens > chunk_size_tokens:
            content = "\n\n".join(current)
            chunks.append(Chunk(
                content=content,
                token_count=count_tokens(content),
            ))
            # Build overlap tail from the end of the chunk we just
            # emitted, snapping to whole units.
            current, current_tokens = _take_overlap(
                current, chunk_overlap_tokens,
            )
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        content = "\n\n".join(current)
        chunks.append(Chunk(
            content=content,
            token_count=count_tokens(content),
        ))
    return chunks


def _take_overlap(units: list[str], target_tokens: int) -> tuple[list[str], int]:
    """Return the trailing units of *units* totalling ~``target_tokens``."""
    if target_tokens <= 0 or not units:
        return [], 0
    tail: list[str] = []
    total = 0
    for unit in reversed(units):
        unit_tokens = count_tokens(unit)
        if total + unit_tokens > target_tokens and tail:
            break
        tail.insert(0, unit)
        total += unit_tokens
        if total >= target_tokens:
            break
    return tail, total


# ---------------------------------------------------------------------------
# Recursive token chunker
# ---------------------------------------------------------------------------


class RecursiveTokenChunker(Chunker):
    """Default chunker. Paragraph → sentence → word-bounded split.

    Suitable for any plaintext input. No external dependencies (uses
    tiktoken when present, otherwise the char/4 heuristic). Output
    chunks are joined with double newlines so the seams remain
    paragraph-shaped when the chunks are surfaced into a prompt.
    """

    async def chunk(
        self,
        text: str,
        *,
        chunk_size_tokens: int = 600,
        chunk_overlap_tokens: int = 100,
    ) -> list[Chunk]:
        if chunk_size_tokens <= 0:
            raise ValueError(
                f"chunk_size_tokens must be positive, got {chunk_size_tokens}",
            )
        if chunk_overlap_tokens < 0:
            raise ValueError(
                "chunk_overlap_tokens must be non-negative, got "
                f"{chunk_overlap_tokens}",
            )
        text = text.strip()
        if not text:
            return []

        units = _recursive_units(text, chunk_size_tokens)
        return _greedy_assemble(
            units,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )


# ---------------------------------------------------------------------------
# Null chunker
# ---------------------------------------------------------------------------


class NullChunker(Chunker):
    """No-op chunker — returns an empty list. Used when chunking is disabled."""

    async def chunk(
        self,
        text: str,
        *,
        chunk_size_tokens: int = 600,
        chunk_overlap_tokens: int = 100,
    ) -> list[Chunk]:
        return []


# ---------------------------------------------------------------------------
# Markdown-aware Docling chunker
# ---------------------------------------------------------------------------


# ATX-style markdown headings: 1-6 leading hashes, a space, then the
# heading text. Setext headings (=== / ---) are not handled — Docling's
# markdown export uses ATX exclusively.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)


def _split_by_headings(
    text: str,
) -> list[tuple[list[str], str]]:
    """Split *text* into (heading_path, body) sections.

    ``heading_path`` is a breadcrumb of titles from the document root to
    the section's heading (most recent ATX heading at each level). The
    body is everything between this heading and the next at the same or
    higher level. The *first* section may have an empty heading_path
    when the document begins with prose before any heading.

    Markdown produced by Docling's ``export_to_markdown`` typically
    uses level-1 for the document title, level-2 for major sections,
    level-3+ for subsections, and inline ``###`` markers for
    table-of-contents-style structure. The chunker uses these as
    *primary* split boundaries — a single heading boundary is a
    stronger semantic cut than a paragraph break.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [([], text)]

    sections: list[tuple[list[str], str]] = []
    cursor: list[str] = []  # current heading-path stack

    # Prologue: text before the first heading.
    first_start = matches[0].start()
    if first_start > 0:
        prologue = text[:first_start].strip()
        if prologue:
            sections.append(([], prologue))

    for i, match in enumerate(matches):
        level = len(match.group("hashes"))
        title = match.group("title").strip()
        # Pop until the path is shallower than the current heading,
        # then push the new title.
        while len(cursor) >= level:
            cursor.pop()
        # Pad with empty strings if the doc skips heading levels (h1 → h3).
        while len(cursor) < level - 1:
            cursor.append("")
        cursor.append(title)

        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        # Always emit, even if body is empty — preserves heading-path
        # context for empty sections (caller can drop them later).
        sections.append((list(cursor), body))

    return sections


def _heading_path_text(path: list[str]) -> str:
    """Render a heading path as ``H1 > H2 > H3``."""
    return " > ".join(p for p in path if p)


class DoclingChunker(Chunker):
    """Chunker that respects markdown headings produced by Docling.

    Splits the input on ATX heading boundaries first, captures the
    heading path in :attr:`Chunk.metadata.section_path`, then falls
    back to :class:`RecursiveTokenChunker`'s paragraph-→-sentence-→-
    word recursion *within* each section. The result is chunks that
    line up with the document's structural skeleton, which usually
    yields much better retrieval recall than chunking purely on
    paragraph breaks.

    Despite the name, this chunker does not require Docling at runtime
    — it works on any heading-rich markdown. Its purpose is to be the
    smarter default when the upstream parser was Docling, since
    Docling's output is reliably heading-rich. The ``[files]`` extra
    is the de-facto availability signal: when Docling is installed,
    most binary uploads land here; when it is not, the parser produces
    plaintext and there is no heading structure to exploit.

    Future work: when the parser starts handing forward the parsed
    ``DoclingDocument``, we can swap the markdown-walking layer for
    Docling's native ``HybridChunker`` to gain page numbers and
    table-cell awareness. The :class:`Chunker` contract stays the same.
    """

    def __init__(self) -> None:
        self._inner = RecursiveTokenChunker()

    async def chunk(
        self,
        text: str,
        *,
        chunk_size_tokens: int = 600,
        chunk_overlap_tokens: int = 100,
    ) -> list[Chunk]:
        if chunk_size_tokens <= 0:
            raise ValueError(
                f"chunk_size_tokens must be positive, got {chunk_size_tokens}",
            )
        if chunk_overlap_tokens < 0:
            raise ValueError(
                "chunk_overlap_tokens must be non-negative, got "
                f"{chunk_overlap_tokens}",
            )
        text = text.strip()
        if not text:
            return []

        sections = _split_by_headings(text)
        out: list[Chunk] = []
        for path, body in sections:
            if not body:
                continue
            section_label = _heading_path_text(path)

            # Small section that fits below the cap → emit as one chunk
            # with full heading path attribution.
            if count_tokens(body) <= chunk_size_tokens:
                out.append(Chunk(
                    content=_with_heading_prefix(section_label, body),
                    metadata={
                        "section_path": path,
                        "heading": path[-1] if path else "",
                    },
                    token_count=count_tokens(body),
                ))
                continue

            # Large section → recurse, prepending heading context to
            # each sub-chunk so the embedding sees the section title.
            sub_chunks = await self._inner.chunk(
                body,
                chunk_size_tokens=chunk_size_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
            )
            for chunk in sub_chunks:
                chunk.content = _with_heading_prefix(
                    section_label, chunk.content,
                )
                chunk.metadata = {
                    "section_path": path,
                    "heading": path[-1] if path else "",
                }
                # Re-tokenize since we just prepended.
                chunk.token_count = count_tokens(chunk.content)
                out.append(chunk)
        return out


def _with_heading_prefix(label: str, body: str) -> str:
    """Prepend ``[label]\\n`` to *body* when *label* is non-empty."""
    if not label:
        return body
    return f"[{label}]\n{body}"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _docling_available() -> bool:
    """Cheap probe: did the user install ``fipsagents[files]``?"""
    try:
        import docling  # noqa: F401
        return True
    except ImportError:
        return False


def create_chunker(*, enabled: bool = True) -> Chunker:
    """Create a chunker based on what's available in the environment.

    - ``enabled=False`` → :class:`NullChunker` (chunking disabled).
    - ``docling`` installed → :class:`DoclingChunker` (heading-aware).
    - Otherwise → :class:`RecursiveTokenChunker` (zero deps).

    Both the heading-aware and recursive chunkers satisfy the same
    contract; the heading-aware one is auto-selected because the only
    deployments that have it are exactly the ones whose parser
    produces structurally rich text — and on those, heading-aware
    chunks beat plain paragraph chunks for retrieval recall.
    """
    if not enabled:
        return NullChunker()
    if _docling_available():
        logger.info(
            "create_chunker: docling installed → using DoclingChunker "
            "(heading-aware splitting).",
        )
        return DoclingChunker()
    return RecursiveTokenChunker()
