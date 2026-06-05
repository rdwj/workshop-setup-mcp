"""Virus scanning for /v1/files uploads.

The scanner is invoked between MIME sniffing and parsing on every
upload. The default (no URL configured) is :class:`NullScanner` —
every file passes. Production deployments configure
:class:`HttpScanner` with the URL of a ClamAV sidecar that exposes a
small REST contract:

- ``POST <url>`` with the raw file bytes (``application/octet-stream``)
- ``200 OK`` body ``{"infected": false, "viruses": []}`` — clean
- ``422`` or ``200`` with ``{"infected": true, "viruses": [...]}`` — infected

Any other status is a sidecar error; the configured ``fail_mode``
decides whether the upload is accepted or rejected.

The scanner does not pretend to be the only line of defense. MIME
sniffing rejects mislabelled files before the scanner runs, and the
upload size cap rejects oversize payloads earlier still. The scanner
catches binary content that passes those checks but contains a known
virus signature.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """Outcome of a virus scan.

    - ``infected`` is the authoritative answer: True means reject,
      False means accept.
    - ``viruses`` lists virus names when present (informational only).
    - ``error`` is set when the scanner could not produce a verdict;
      callers honour ``fail_mode`` to decide whether ``infected`` is
      effectively True or False in that case.
    """

    infected: bool
    viruses: list[str] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def clean(cls) -> "ScanResult":
        return cls(infected=False)

    @classmethod
    def found(cls, viruses: list[str]) -> "ScanResult":
        return cls(infected=True, viruses=list(viruses))

    @classmethod
    def failed(cls, error: str) -> "ScanResult":
        return cls(infected=False, error=error)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class VirusScanner(ABC):
    """Pluggable virus-scanning backend."""

    @abstractmethod
    async def scan(
        self, data: bytes, *, filename: str,
    ) -> ScanResult:
        """Scan *data* for viruses. Never raises; errors → ``ScanResult.failed``."""

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


# ---------------------------------------------------------------------------
# Null scanner
# ---------------------------------------------------------------------------


class NullScanner(VirusScanner):
    """No scanning — every file is treated as clean.

    Default when ``files.scanner.url`` is unset. Suitable for dev
    environments and any deployment where another component (eg the
    gateway) handles virus scanning instead.
    """

    async def scan(
        self, data: bytes, *, filename: str,
    ) -> ScanResult:
        return ScanResult.clean()


# ---------------------------------------------------------------------------
# HTTP scanner
# ---------------------------------------------------------------------------


class HttpScanner(VirusScanner):
    """POST file bytes to a configurable scanner URL.

    Expected sidecar contract:

    - ``200`` with body ``{"infected": false, ...}``           → clean
    - ``200`` with body ``{"infected": true, "viruses": [...]}`` → infected
    - ``422``                                                  → infected
    - any other status                                         → scanner error

    The body is sent as ``application/octet-stream`` with the
    ``X-Filename`` header set so the scanner can apply per-format
    rules. Connection pooling is via a shared ``httpx.AsyncClient``
    that lives for the duration of the server.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not url:
            raise ValueError("HttpScanner requires a non-empty url")
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client

    async def scan(
        self, data: bytes, *, filename: str,
    ) -> ScanResult:
        try:
            client = self._get_client()
            response = await client.post(  # type: ignore[attr-defined]
                self._url,
                content=data,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Filename": filename,
                },
            )
        except Exception as exc:
            return ScanResult.failed(
                f"scanner request failed ({type(exc).__name__}): {exc}",
            )

        status = response.status_code
        # 422 is the explicit "infected" signal in our contract.
        if status == 422:
            viruses = _extract_viruses(response)
            return ScanResult.found(viruses or ["unknown"])

        if status != 200:
            return ScanResult.failed(
                f"scanner returned HTTP {status}",
            )

        # 200 — body decides clean vs infected.
        body = _safe_json(response)
        if body is None:
            # Sidecar didn't return JSON; assume clean since 200 is the
            # success code in HTTP semantics.
            return ScanResult.clean()
        if body.get("infected") is True:
            return ScanResult.found(body.get("viruses") or ["unknown"])
        return ScanResult.clean()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()  # type: ignore[attr-defined]
            except Exception:
                logger.warning("HttpScanner: error closing client", exc_info=True)
            self._client = None


def _safe_json(response: object) -> dict | None:
    try:
        body = response.json()  # type: ignore[attr-defined]
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _extract_viruses(response: object) -> list[str]:
    body = _safe_json(response)
    if body is None:
        return []
    viruses = body.get("viruses")
    if isinstance(viruses, list):
        return [str(v) for v in viruses]
    return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_scanner(
    *,
    url: str = "",
    timeout_seconds: float = 30.0,
) -> VirusScanner:
    """Create a scanner from config values.

    - Empty ``url`` → :class:`NullScanner` (no scanning).
    - Non-empty ``url`` → :class:`HttpScanner` posting to that URL.
    """
    if not url:
        return NullScanner()
    return HttpScanner(url, timeout_seconds=timeout_seconds)
