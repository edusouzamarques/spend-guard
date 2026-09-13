"""Burn projection.

Answers one question: at the current rate, does this period end over the
ceiling, and if so, when does it cross?

The rule the module is built around is that it refuses to guess. With no
elapsed time there is no rate, and a projection of zero would read as "all
clear" on exactly the day a runaway job starts. Every derived figure is
``None`` until there is enough history to compute it honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .clock import ensure_utc
from .guard import Snapshot
from .money import Money, Rounding
from .periods import PeriodWindow

__all__ = ["Projection", "project"]

_SECONDS_PER_DAY = Decimal(86400)


@dataclass(frozen=True)
class Projection:
    """A straight-line extrapolation of spend to the end of the period."""

    window: PeriodWindow
    now: datetime
    elapsed_fraction: Decimal
    committed: Money
    ceiling: Money
    burn_per_day: Optional[Money] = None
    projected_total: Optional[Money] = None
    projected_overage: Optional[Money] = None
    exhaustion_at: Optional[datetime] = None

    @property
    def has_rate(self) -> bool:
        return self.burn_per_day is not None

    @property
    def on_track(self) -> Optional[bool]:
        """True when the projection lands at or under the ceiling.

        ``None`` when there is no rate yet. A caller that treats ``None`` as
        falsey gets an alert instead of a false reassurance, which is the safe
        way round.
        """
        if self.projected_total is None:
            return None
        return self.projected_total.micros <= self.ceiling.micros

    @property
    def exhausts_before_period_end(self) -> bool:
        return self.exhaustion_at is not None and self.exhaustion_at < self.window.end

    @property
    def remaining(self) -> Money:
        return self.ceiling - self.committed

    def to_mapping(self) -> dict:
        return {
            "period": self.window.key,
            "now": self.now.isoformat(),
            "elapsed_fraction": str(self.elapsed_fraction.quantize(Decimal("0.0001"))),
            "committed": self.committed.format(),
            "ceiling": self.ceiling.format(),
            "remaining": self.remaining.format(),
            "burn_per_day": None if self.burn_per_day is None else self.burn_per_day.format(),
            "projected_total": (
                None if self.projected_total is None else self.projected_total.format()
            ),
            "projected_overage": (
                None if self.projected_overage is None else self.projected_overage.format()
            ),
            "exhaustion_at": (
                None if self.exhaustion_at is None else self.exhaustion_at.isoformat()
            ),
            "exhausts_before_period_end": self.exhausts_before_period_end,
            "on_track": self.on_track,
        }


def project(snapshot: Snapshot, *, include_reserved: bool = False) -> Projection:
    """Extrapolate ``snapshot`` to the end of its period.

    ``include_reserved`` counts in-flight holds as spend. That is the
    pessimistic reading and the right one when deciding whether to start more
    work; the default reports only money that has actually been committed.
    """
    window = snapshot.window
    now = ensure_utc(snapshot.now)
    spent = snapshot.used if include_reserved else snapshot.committed
    elapsed_fraction = window.elapsed_fraction(now)
    elapsed_seconds = Decimal(window.elapsed(now).total_seconds())

    if elapsed_seconds <= 0 or spent.micros <= 0:
        # No elapsed time, or nothing spent: there is no rate to extend. Saying
        # so beats reporting a confident zero.
        return Projection(
            window=window,
            now=now,
            elapsed_fraction=elapsed_fraction,
            committed=spent,
            ceiling=snapshot.ceiling,
        )

    per_second = spent.as_decimal() / elapsed_seconds
    burn_per_day = Money.from_decimal(per_second * _SECONDS_PER_DAY, Rounding.UP)
    total_seconds = Decimal(window.duration.total_seconds())
    projected_total = Money.from_decimal(per_second * total_seconds, Rounding.UP)
    overage = projected_total - snapshot.ceiling
    projected_overage = overage if overage.is_positive else Money.zero()

    exhaustion_at: Optional[datetime] = None
    if per_second > 0:
        seconds_to_ceiling = snapshot.ceiling.as_decimal() / per_second
        if seconds_to_ceiling <= Decimal(10) ** 12:  # keep timedelta in range
            exhaustion_at = window.start + timedelta(seconds=float(seconds_to_ceiling))

    return Projection(
        window=window,
        now=now,
        elapsed_fraction=elapsed_fraction,
        committed=spent,
        ceiling=snapshot.ceiling,
        burn_per_day=burn_per_day,
        projected_total=projected_total,
        projected_overage=projected_overage,
        exhaustion_at=exhaustion_at,
    )
