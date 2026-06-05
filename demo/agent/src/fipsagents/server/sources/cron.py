"""Cron event source -- fires events on a 5-field POSIX cron schedule.

Parses standard ``minute hour day-of-month month day-of-week`` expressions
and yields :class:`InboundEvent` at each matching time.  Supports wildcards,
ranges, lists, and step values.  Does NOT support ``@yearly``/``@reboot``
macros, a seconds field, or ``L``/``W``/``#`` extensions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..events import EventSource, InboundEvent, TokenBucketRateLimiter

logger = logging.getLogger(__name__)

UTC = timezone.utc

# Maximum years to scan forward before giving up.  Prevents infinite
# loops on expressions that can never match (e.g. Feb 30).
_MAX_SCAN_YEARS = 4


class CronExpression:
    """5-field POSIX cron parser.

    Fields::

        ┌───────────── minute (0-59)
        │ ┌───────────── hour (0-23)
        │ │ ┌───────────── day of month (1-31)
        │ │ │ ┌───────────── month (1-12)
        │ │ │ │ ┌───────────── day of week (0-7, 0 and 7 = Sunday)
        * * * * *

    Supports: ``*``, ranges (``1-5``), lists (``1,3,5``), steps
    (``*/15``, ``1-5/2``).  Raises :class:`ValueError` on malformed
    expressions.
    """

    _FIELD_BOUNDS: list[tuple[str, int, int]] = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 7),
    ]

    def __init__(self, expression: str) -> None:
        expression = expression.strip()
        if not expression:
            raise ValueError("Cron expression must not be empty")

        if expression.startswith("@"):
            raise ValueError(
                f"Cron macros like {expression!r} are not supported; "
                f"use a 5-field expression"
            )

        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(
                f"Expected 5 fields, got {len(fields)}: {expression!r}"
            )

        parsed: list[set[int]] = []
        for raw, (name, lo, hi) in zip(fields, self._FIELD_BOUNDS):
            parsed.append(self._parse_field(raw, lo, hi, name))

        self.minutes = parsed[0]
        self.hours = parsed[1]
        self.days_of_month = parsed[2]
        self.months = parsed[3]

        # Normalise day-of-week: both 0 and 7 mean Sunday.
        dow = parsed[4]
        if 7 in dow:
            dow = (dow - {7}) | {0}
        self.days_of_week = dow

        self._expression = expression

    # -- field parsing -------------------------------------------------

    @staticmethod
    def _parse_field(
        field: str, min_val: int, max_val: int, name: str,
    ) -> set[int]:
        """Parse a single cron field into a set of valid integers."""
        result: set[int] = set()

        for token in field.split(","):
            token = token.strip()
            if not token:
                raise ValueError(f"Empty element in {name} field")

            # Handle step suffix: ``*/2``, ``1-5/2``
            step = 1
            if "/" in token:
                base, step_str = token.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    raise ValueError(
                        f"Invalid step {step_str!r} in {name} field"
                    ) from None
                if step <= 0:
                    raise ValueError(
                        f"Step must be positive in {name} field, got {step}"
                    )
                token = base

            if token == "*":
                result.update(range(min_val, max_val + 1, step))
            elif "-" in token:
                parts = token.split("-", 1)
                try:
                    lo, hi = int(parts[0]), int(parts[1])
                except ValueError:
                    raise ValueError(
                        f"Invalid range {token!r} in {name} field"
                    ) from None
                if lo > hi:
                    raise ValueError(
                        f"Invalid range {lo}-{hi} in {name} field "
                        f"(start > end)"
                    )
                if lo < min_val or hi > max_val:
                    raise ValueError(
                        f"Range {lo}-{hi} out of bounds "
                        f"[{min_val}-{max_val}] for {name}"
                    )
                result.update(range(lo, hi + 1, step))
            else:
                # Plain integer (possibly with a step already extracted
                # from ``N/S`` -- that's nonsensical, treat N as start
                # of a one-element range).
                try:
                    val = int(token)
                except ValueError:
                    raise ValueError(
                        f"Invalid value {token!r} in {name} field"
                    ) from None
                if val < min_val or val > max_val:
                    raise ValueError(
                        f"Value {val} out of bounds "
                        f"[{min_val}-{max_val}] for {name}"
                    )
                result.add(val)

        if not result:
            raise ValueError(f"Field {name} resolved to an empty set")

        return result

    # -- next-fire calculation -----------------------------------------

    def next_fire_time(self, from_dt: datetime) -> datetime:
        """Return the next datetime AFTER *from_dt* that matches.

        Uses a simple minute-by-minute scan capped at ``_MAX_SCAN_YEARS``
        years forward.  Raises :class:`RuntimeError` if no match is found
        (e.g. ``0 0 30 2 *`` -- Feb 30 never exists).
        """
        # Start scanning from the next whole minute after from_dt.
        dt = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        cutoff = from_dt + timedelta(days=_MAX_SCAN_YEARS * 366)

        while dt <= cutoff:
            # Check month first -- if it doesn't match, skip ahead.
            if dt.month not in self.months:
                dt = self._advance_month(dt)
                continue

            # Check day-of-month and day-of-week.
            if dt.day not in self.days_of_month:
                dt = self._advance_day(dt)
                continue

            if dt.weekday() not in self._isoweekday_set():
                dt = self._advance_day(dt)
                continue

            if dt.hour not in self.hours:
                dt = self._advance_hour(dt)
                continue

            if dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
                continue

            return dt

        raise RuntimeError(
            f"No matching time found within {_MAX_SCAN_YEARS} years "
            f"for expression {self._expression!r}"
        )

    def _isoweekday_set(self) -> set[int]:
        """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
        mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
        return {mapping[d] for d in self.days_of_week}

    @staticmethod
    def _advance_month(dt: datetime) -> datetime:
        """Jump to midnight on the 1st of the next month."""
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0)
        return dt.replace(month=dt.month + 1, day=1, hour=0, minute=0)

    @staticmethod
    def _advance_day(dt: datetime) -> datetime:
        """Jump to midnight of the next day."""
        next_day = dt + timedelta(days=1)
        return next_day.replace(hour=0, minute=0)

    @staticmethod
    def _advance_hour(dt: datetime) -> datetime:
        """Jump to the start of the next hour."""
        return (dt.replace(minute=0) + timedelta(hours=1))

    def __repr__(self) -> str:
        return f"CronExpression({self._expression!r})"


class CronSource(EventSource):
    """Event source that fires on a cron schedule.

    Reads ``schedule``, ``event_type``, and ``max_events_per_second``
    from its config (a :class:`CronSourceConfig`).
    """

    def __init__(self, source_id: str, *, config: object | None = None) -> None:
        super().__init__(source_id, config=config)
        if config is None:
            raise ValueError("CronSource requires a config with 'schedule'")
        self._cron = CronExpression(config.schedule)  # type: ignore[union-attr]
        self._event_type: str = config.event_type  # type: ignore[union-attr]
        rate: float = getattr(config, "max_events_per_second", 1.0)
        self._limiter = TokenBucketRateLimiter(rate)

    async def consume(self) -> AsyncIterator[InboundEvent]:
        """Sleep until the next fire time, then yield an event."""
        while True:
            now = datetime.now(tz=UTC)
            next_fire = self._cron.next_fire_time(now)
            delta = (next_fire - now).total_seconds()

            if delta > 0:
                logger.debug(
                    "CronSource %s: sleeping %.1fs until %s",
                    self.source_id,
                    delta,
                    next_fire.isoformat(),
                )
                await asyncio.sleep(delta)

            await self._limiter.acquire()

            yield InboundEvent(
                event_id=uuid4().hex,
                event_type=self._event_type,
                payload={"scheduled_time": next_fire.isoformat()},
                source=self.source_id,
                timestamp=datetime.now(tz=UTC),
                session_key=f"event:cron:{self._event_type}",
            )
