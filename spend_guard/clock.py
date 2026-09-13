"""Time, injected.

Nothing in this package calls :func:`datetime.now` directly. Period boundaries,
reservation leases and burn projection all read the clock they were handed, so
the whole decision surface is reproducible in a test without sleeping, without
freezing global state and without a network round trip to anybody's API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Union

try:  # pragma: no cover - trivial import shim
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8 is out of support anyway
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = ["Clock", "SystemClock", "FixedClock", "ensure_utc"]


def ensure_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime.

    A naive datetime is *assumed* to be UTC rather than rejected, because the
    alternative is an exception on the one line of a caller's integration that
    is hardest to reach in their tests. The assumption is documented and the
    conversion is lossless for anyone already working in UTC.
    """
    if not isinstance(moment, datetime):
        raise TypeError(f"expected datetime, got {type(moment).__name__}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@runtime_checkable
class Clock(Protocol):
    """Anything that can say what time it is."""

    def now(self) -> datetime:  # pragma: no cover - protocol declaration
        ...


class SystemClock:
    """The real clock. The only place this package touches wall time."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """A clock that only moves when a test moves it."""

    __slots__ = ("_now",)

    def __init__(self, moment: datetime) -> None:
        self._now = ensure_utc(moment)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> "FixedClock":
        self._now = ensure_utc(moment)
        return self

    def advance(self, delta: Union[timedelta, int, float]) -> "FixedClock":
        """Move forward by a :class:`timedelta` or a number of seconds."""
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=float(delta))
        self._now = self._now + delta
        return self

    def __repr__(self) -> str:
        return f"FixedClock({self._now.isoformat()})"
