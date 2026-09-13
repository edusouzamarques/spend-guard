"""Budget periods and their boundaries.

A period is a rule; a :class:`PeriodWindow` is one concrete half-open interval
``[start, end)`` produced by that rule. Windows tile time with no gap and no
overlap, which is what makes "spend in this period" a well-defined sum even
when the billing cycle starts on the 31st and February exists.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

from .clock import ensure_utc
from .errors import PeriodError

__all__ = ["PeriodKind", "Period", "PeriodWindow"]


class PeriodKind(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    FIXED_DAYS = "fixed_days"


_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _period_kind(value) -> PeriodKind:
    """Resolve a period kind, or raise :class:`PeriodError` naming the valid ones.

    Every route into a :class:`Period` goes through here. A declaration written
    as the bare string ``"fortnightly"`` has to fail the same way as the mapping
    form, or the CLI turns an operator's typo into a traceback instead of the
    one-line message the error contract promises.
    """
    if isinstance(value, PeriodKind):
        return value
    raw = str(value).strip().lower()
    try:
        return PeriodKind(raw)
    except ValueError:
        valid = ", ".join(k.value for k in PeriodKind)
        raise PeriodError(f"unknown period kind {raw!r}; valid: {valid}") from None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _anchor_day(year: int, month: int, day: int) -> date:
    """The anchor date in a month, clamped to the month's real length.

    An anchor of 31 lands on 28 in February and on 29 in a leap February. The
    clamp is what keeps consecutive windows touching instead of overlapping.
    """
    return date(year, month, min(day, _days_in_month(year, month)))


@dataclass(frozen=True)
class PeriodWindow:
    """One concrete budgeting interval, half-open: ``start <= t < end``."""

    key: str
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        moment = ensure_utc(moment)
        return self.start <= moment < self.end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def remaining(self, now: datetime) -> timedelta:
        """Time left in the window, never negative."""
        now = ensure_utc(now)
        if now >= self.end:
            return timedelta(0)
        if now <= self.start:
            return self.duration
        return self.end - now

    def elapsed(self, now: datetime) -> timedelta:
        now = ensure_utc(now)
        if now <= self.start:
            return timedelta(0)
        if now >= self.end:
            return self.duration
        return now - self.start

    def elapsed_fraction(self, now: datetime) -> Decimal:
        """Fraction of the window already spent, clamped to ``[0, 1]``."""
        total = self.duration.total_seconds()
        if total <= 0:  # pragma: no cover - construction forbids it
            raise PeriodError("period window has non-positive duration")
        return Decimal(self.elapsed(now).total_seconds()) / Decimal(total)

    def __str__(self) -> str:
        return f"{self.key} [{self.start.date()} .. {self.end.date()})"


@dataclass(frozen=True)
class Period:
    """The rule that generates windows.

    ``anchor_day`` shifts a monthly period off the 1st, for a provider whose
    billing cycle starts mid-month. ``week_starts_on`` is 0 for Monday.
    ``anchor_month`` shifts a quarterly or yearly period onto a fiscal year.
    ``length_days`` and ``epoch`` define a rolling fixed-length period.
    """

    kind: PeriodKind = PeriodKind.MONTHLY
    anchor_day: int = 1
    week_starts_on: int = 0
    anchor_month: int = 1
    length_days: int = 0
    epoch: Optional[date] = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PeriodKind):
            object.__setattr__(self, "kind", _period_kind(self.kind))
        if not 1 <= self.anchor_day <= 31:
            raise PeriodError(f"anchor_day must be 1..31, got {self.anchor_day}")
        if not 0 <= self.week_starts_on <= 6:
            raise PeriodError(
                f"week_starts_on must be 0 (Monday) .. 6 (Sunday), got {self.week_starts_on}"
            )
        if not 1 <= self.anchor_month <= 12:
            raise PeriodError(f"anchor_month must be 1..12, got {self.anchor_month}")
        if self.kind is PeriodKind.FIXED_DAYS:
            if self.length_days < 1:
                raise PeriodError("a fixed_days period needs length_days >= 1")
            if self.epoch is None:
                raise PeriodError("a fixed_days period needs an epoch date")
        elif self.length_days:
            raise PeriodError(
                f"length_days only applies to a fixed_days period, not {self.kind.value}"
            )

    # -- construction ------------------------------------------------------

    @classmethod
    def monthly(cls, anchor_day: int = 1) -> "Period":
        return cls(kind=PeriodKind.MONTHLY, anchor_day=anchor_day)

    @classmethod
    def daily(cls) -> "Period":
        return cls(kind=PeriodKind.DAILY)

    @classmethod
    def weekly(cls, week_starts_on: int = 0) -> "Period":
        return cls(kind=PeriodKind.WEEKLY, week_starts_on=week_starts_on)

    @classmethod
    def fixed_days(cls, length_days: int, epoch: date) -> "Period":
        return cls(kind=PeriodKind.FIXED_DAYS, length_days=length_days, epoch=epoch)

    @classmethod
    def from_mapping(cls, data) -> "Period":
        if isinstance(data, Period):
            return data
        if isinstance(data, str):
            return cls(kind=_period_kind(data))
        if not isinstance(data, dict):
            raise PeriodError(f"cannot read a period from {type(data).__name__}")
        kind = _period_kind(data.get("kind", "monthly"))
        week_start = data.get("week_starts_on", 0)
        if isinstance(week_start, str):
            name = week_start.strip().lower()
            if name not in _WEEKDAY_NAMES:
                raise PeriodError(f"unknown weekday {week_start!r}")
            week_start = _WEEKDAY_NAMES.index(name)
        epoch = data.get("epoch")
        if isinstance(epoch, str):
            try:
                epoch = date.fromisoformat(epoch)
            except ValueError:
                raise PeriodError(f"epoch {epoch!r} is not an ISO date") from None
        return cls(
            kind=kind,
            anchor_day=int(data.get("anchor_day", 1)),
            week_starts_on=int(week_start),
            anchor_month=int(data.get("anchor_month", 1)),
            length_days=int(data.get("length_days", 0)),
            epoch=epoch,
        )

    def to_mapping(self) -> dict:
        out = {"kind": self.kind.value}
        if self.kind is PeriodKind.MONTHLY and self.anchor_day != 1:
            out["anchor_day"] = self.anchor_day
        if self.kind is PeriodKind.WEEKLY and self.week_starts_on:
            out["week_starts_on"] = self.week_starts_on
        if self.kind in (PeriodKind.QUARTERLY, PeriodKind.YEARLY) and self.anchor_month != 1:
            out["anchor_month"] = self.anchor_month
        if self.kind is PeriodKind.FIXED_DAYS:
            out["length_days"] = self.length_days
            out["epoch"] = self.epoch.isoformat() if self.epoch else None
        return out

    # -- windows -----------------------------------------------------------

    def window(self, moment: datetime) -> PeriodWindow:
        """The single window that contains ``moment``."""
        moment = ensure_utc(moment)
        day = moment.date()
        if self.kind is PeriodKind.DAILY:
            start, end = day, day + timedelta(days=1)
            key = start.isoformat()
        elif self.kind is PeriodKind.WEEKLY:
            back = (day.weekday() - self.week_starts_on) % 7
            start = day - timedelta(days=back)
            end = start + timedelta(days=7)
            if self.week_starts_on == 0:
                iso = start.isocalendar()
                key = "%04d-W%02d" % (iso[0], iso[1])
            else:
                key = "W%s" % start.isoformat()
        elif self.kind is PeriodKind.MONTHLY:
            start = _anchor_day(day.year, day.month, self.anchor_day)
            if day < start:
                year, month = _shift_month(day.year, day.month, -1)
                start = _anchor_day(year, month, self.anchor_day)
            year, month = _shift_month(start.year, start.month, 1)
            end = _anchor_day(year, month, self.anchor_day)
            key = (
                "%04d-%02d" % (start.year, start.month)
                if self.anchor_day == 1
                else start.isoformat()
            )
        elif self.kind is PeriodKind.QUARTERLY:
            offset = (day.month - self.anchor_month) % 12
            months_back = offset % 3
            year, month = _shift_month(day.year, day.month, -months_back)
            start = date(year, month, 1)
            end_year, end_month = _shift_month(year, month, 3)
            end = date(end_year, end_month, 1)
            index = ((month - self.anchor_month) % 12) // 3 + 1
            fiscal_year = start.year if self.anchor_month == 1 else _fiscal_year(start, self.anchor_month)
            key = "%04d-Q%d" % (fiscal_year, index)
        elif self.kind is PeriodKind.YEARLY:
            start = date(day.year, self.anchor_month, 1)
            if day < start:
                start = date(day.year - 1, self.anchor_month, 1)
            end = date(start.year + 1, self.anchor_month, 1)
            key = "%04d" % start.year if self.anchor_month == 1 else "FY%04d" % start.year
        elif self.kind is PeriodKind.FIXED_DAYS:
            assert self.epoch is not None  # guaranteed by __post_init__
            index = (day - self.epoch).days // self.length_days
            start = self.epoch + timedelta(days=index * self.length_days)
            end = start + timedelta(days=self.length_days)
            key = "%s+%dd" % (start.isoformat(), self.length_days)
        else:  # pragma: no cover - PeriodKind is exhaustive above
            raise PeriodError(f"unsupported period kind {self.kind}")
        return PeriodWindow(key=key, start=_midnight(start), end=_midnight(end))

    def previous_window(self, window: PeriodWindow) -> PeriodWindow:
        return self.window(window.start - timedelta(microseconds=1))

    def next_window(self, window: PeriodWindow) -> PeriodWindow:
        return self.window(window.end)

    def windows_between(self, start: datetime, end: datetime):
        """Yield every window overlapping ``[start, end)``, oldest first."""
        start = ensure_utc(start)
        end = ensure_utc(end)
        if end <= start:
            return
        current = self.window(start)
        while current.start < end:
            yield current
            current = self.next_window(current)


def _fiscal_year(start: date, anchor_month: int) -> int:
    """Label a fiscal quarter by the year its fiscal year ends in."""
    return start.year + 1 if start.month >= anchor_month else start.year
