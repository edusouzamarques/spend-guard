"""Reservation state, derived by replaying the ledger.

The whole point of this module is the lease. A reservation holds budget from
the moment the caller asks until the moment it commits, because charging after
the fact is useless for refusing the call that breaks the ceiling. But a held
reservation whose owner died is indistinguishable from one whose owner is still
working, and a budget that leaks a little on every crash eventually refuses
everything.

So a reservation holds budget *until its lease expires*, and the arithmetic
consults the clock. An expired reservation stops counting immediately, whether
or not anybody swept it, whether or not the process that made it ever runs
again. Sweeping writes the fact down for the audit trail; it is not what makes
the budget recover.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .clock import ensure_utc
from .errors import LedgerCorruptError
from .ledger import Entry, EntryType
from .money import Money

__all__ = ["ReservationStatus", "Reservation", "LedgerState", "replay"]


class ReservationStatus(str, Enum):
    OPEN = "open"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Reservation:
    """Budget held for a call that has not finished yet."""

    id: str
    period_key: str
    work_key: str
    units: Decimal
    estimate: Money
    created_at: datetime
    lease_until: datetime
    category: str = ""
    status: ReservationStatus = ReservationStatus.OPEN
    actual: Optional[Money] = None
    estimated_price: bool = False
    note: str = ""

    def holds_at(self, now: datetime) -> bool:
        """True when this reservation still holds budget at ``now``.

        ``OPEN`` alone is not enough. A lease that has run out releases its
        hold without anybody being alive to say so.
        """
        if self.status is not ReservationStatus.OPEN:
            return False
        return ensure_utc(now) < self.lease_until

    def is_expired_at(self, now: datetime) -> bool:
        return (
            self.status is ReservationStatus.OPEN
            and ensure_utc(now) >= self.lease_until
        )

    @property
    def charged(self) -> Money:
        """What this reservation actually cost. Zero until committed."""
        if self.status is not ReservationStatus.COMMITTED:
            return Money.zero()
        return self.actual if self.actual is not None else self.estimate


@dataclass(frozen=True)
class LedgerState:
    """Everything the guard needs to make a decision, derived from entries."""

    reservations: Mapping[str, Reservation] = field(default_factory=dict)
    committed_by_period: Mapping[str, Money] = field(default_factory=dict)
    committed_by_period_category: Mapping[Tuple[str, str], Money] = field(
        default_factory=dict
    )
    periods_seen: frozenset = frozenset()
    halted: bool = False
    halt_reason: str = ""
    last_seq: int = 0

    # -- committed ---------------------------------------------------------

    def committed(self, period_key: str) -> Money:
        return self.committed_by_period.get(period_key, Money.zero())

    def committed_in_category(self, period_key: str, category: str) -> Money:
        return self.committed_by_period_category.get(
            (period_key, category), Money.zero()
        )

    # -- reserved ----------------------------------------------------------

    def open_reservations(self, period_key: str, now: datetime) -> List[Reservation]:
        return [
            reservation
            for reservation in self.reservations.values()
            if reservation.period_key == period_key and reservation.holds_at(now)
        ]

    def reserved(self, period_key: str, now: datetime) -> Money:
        total = Money.zero()
        for reservation in self.open_reservations(period_key, now):
            total = total + reservation.estimate
        return total

    def reserved_in_category(
        self, period_key: str, category: str, now: datetime
    ) -> Money:
        total = Money.zero()
        for reservation in self.open_reservations(period_key, now):
            if reservation.category == category:
                total = total + reservation.estimate
        return total

    def expired(self, now: datetime) -> List[Reservation]:
        """Open reservations whose lease has run out, oldest first."""
        stale = [
            reservation
            for reservation in self.reservations.values()
            if reservation.is_expired_at(now)
        ]
        stale.sort(key=lambda item: (item.lease_until, item.id))
        return stale

    def categories_used(self, period_key: str) -> List[str]:
        names = {
            category
            for (period, category) in self.committed_by_period_category
            if period == period_key
        }
        names.update(
            reservation.category
            for reservation in self.reservations.values()
            if reservation.period_key == period_key
        )
        return sorted(names)

    def get(self, reservation_id: str) -> Optional[Reservation]:
        return self.reservations.get(reservation_id)


def replay(entries: Iterable[Entry]) -> LedgerState:
    """Fold the ledger into current state.

    Pure: same entries in, same state out, no clock and no files. Any entry
    that cannot be applied raises :class:`LedgerCorruptError` rather than being
    skipped, because a ledger that has been edited by hand is exactly the case
    where quietly carrying on produces a wrong number nobody questions.
    """
    reservations: Dict[str, Reservation] = {}
    by_period: Dict[str, Money] = {}
    by_period_category: Dict[Tuple[str, str], Money] = {}
    periods_seen = set()
    halted = False
    halt_reason = ""
    last_seq = 0

    for entry in entries:
        last_seq = max(last_seq, entry.seq)
        if entry.period_key:
            periods_seen.add(entry.period_key)

        if entry.type is EntryType.RESERVE:
            if entry.reservation_id in reservations:
                raise LedgerCorruptError(
                    f"reservation {entry.reservation_id} is opened twice "
                    f"(entry seq {entry.seq})"
                )
            if not entry.lease_until:
                raise LedgerCorruptError(
                    f"reservation {entry.reservation_id} has no lease "
                    f"(entry seq {entry.seq}); an unbounded hold can never be "
                    "reclaimed after a crash"
                )
            reservations[entry.reservation_id] = Reservation(
                id=entry.reservation_id,
                period_key=entry.period_key,
                work_key=entry.work_key,
                units=entry.units,
                estimate=entry.amount,
                created_at=entry.ts,
                lease_until=entry.lease_until,
                category=entry.category,
                status=ReservationStatus.OPEN,
                estimated_price=entry.estimated,
                note=entry.reason,
            )
            continue

        if entry.type in (EntryType.COMMIT, EntryType.RELEASE, EntryType.EXPIRE):
            existing = reservations.get(entry.reservation_id)
            if existing is None:
                raise LedgerCorruptError(
                    f"entry seq {entry.seq} refers to unknown reservation "
                    f"{entry.reservation_id!r}"
                )
            if existing.status in (
                ReservationStatus.COMMITTED,
                ReservationStatus.RELEASED,
            ):
                raise LedgerCorruptError(
                    f"reservation {entry.reservation_id} is already "
                    f"{existing.status.value} but entry seq {entry.seq} tries to "
                    f"{entry.type.value} it"
                )

            if entry.type is EntryType.COMMIT:
                reservations[entry.reservation_id] = replace(
                    existing,
                    status=ReservationStatus.COMMITTED,
                    actual=entry.amount,
                )
                key = existing.period_key
                by_period[key] = by_period.get(key, Money.zero()) + entry.amount
                pair = (key, existing.category)
                by_period_category[pair] = (
                    by_period_category.get(pair, Money.zero()) + entry.amount
                )
            elif entry.type is EntryType.RELEASE:
                reservations[entry.reservation_id] = replace(
                    existing, status=ReservationStatus.RELEASED
                )
            else:
                reservations[entry.reservation_id] = replace(
                    existing, status=ReservationStatus.EXPIRED
                )
            continue

        if entry.type is EntryType.HALT:
            halted = True
            halt_reason = entry.reason
        elif entry.type is EntryType.RESUME:
            halted = False
            halt_reason = ""
        # NOTE entries carry no state.

    return LedgerState(
        reservations=reservations,
        committed_by_period=by_period,
        committed_by_period_category=by_period_category,
        periods_seen=frozenset(periods_seen),
        halted=halted,
        halt_reason=halt_reason,
        last_seq=last_seq,
    )
