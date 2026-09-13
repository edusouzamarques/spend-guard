"""The enforcement point.

Everything else in this package exists to make this one call trustworthy::

    with guard.charge("text.generate", units=8000, category="text") as ticket:
        response = call_the_api()
        ticket.actual(Money.parse("0.031"))

Budget is held *before* the call runs, because a ceiling that is checked
afterwards cannot refuse anything, and reconciled with the real cost after,
because an estimate is not a receipt.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .budget import BudgetSpec, UNCATEGORISED, compute_carry, effective_ceiling
from .clock import Clock, SystemClock, ensure_utc
from .errors import (
    BudgetExceededError,
    ConfigError,
    DeniedError,
    HaltedError,
    MoneyError,
    ReservationConflictError,
    ReservationStateError,
    UnknownPriceError,
    UnknownReservationError,
)
from .ledger import Entry, EntryType, Ledger, MemoryLedger
from .money import Money, MoneyLike, Rounding
from .periods import PeriodWindow
from .pricing import PriceTable, Quote, Units, parse_units
from .state import LedgerState, Reservation, ReservationStatus, replay

__all__ = [
    "DenialReason",
    "Decision",
    "SpendRequest",
    "CategorySnapshot",
    "Snapshot",
    "Overlay",
    "Ticket",
    "SpendGuard",
    "DEFAULT_LEASE",
]

#: How long a reservation holds budget before the arithmetic stops counting it.
DEFAULT_LEASE = timedelta(minutes=15)

#: Fallback soft threshold, used only when a snapshot is built by hand.
DEFAULT_WARN_THRESHOLD = Decimal("0.8")


class DenialReason(str, Enum):
    """Why a spend was refused. Ordered by the sequence they are checked."""

    HALTED = "halted"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_CATEGORY = "unknown_category"
    UNKNOWN_PRICE = "unknown_price"
    CATEGORY_EXCEEDED = "category_exceeded"
    TOTAL_EXCEEDED = "total_exceeded"


_REASON_ERRORS = {
    DenialReason.HALTED: HaltedError,
    DenialReason.INVALID_REQUEST: DeniedError,
    DenialReason.UNKNOWN_CATEGORY: DeniedError,
    DenialReason.UNKNOWN_PRICE: UnknownPriceError,
    DenialReason.CATEGORY_EXCEEDED: BudgetExceededError,
    DenialReason.TOTAL_EXCEEDED: BudgetExceededError,
}


@dataclass(frozen=True)
class SpendRequest:
    """A proposed unit of work."""

    key: str
    units: Units = 1
    category: str = UNCATEGORISED
    amount: Optional[Money] = None
    note: str = ""


@dataclass(frozen=True)
class CategorySnapshot:
    name: str
    ceiling: Money
    committed: Money
    reserved: Money
    #: The threshold this category is judged against: its own declared
    #: ``warn_threshold`` when it has one, otherwise the budget-wide default.
    warn_threshold: Decimal = DEFAULT_WARN_THRESHOLD

    @property
    def available(self) -> Money:
        return self.ceiling - self.committed - self.reserved

    @property
    def utilisation(self) -> Decimal:
        if self.ceiling.is_zero:
            return Decimal(0) if (self.committed + self.reserved).is_zero else Decimal(1)
        used = (self.committed + self.reserved).as_decimal()
        return used / self.ceiling.as_decimal()

    @property
    def warn_tripped(self) -> bool:
        """True once this category crosses *its own* soft threshold.

        Tuning one noisy category is the whole reason a per-category
        ``warn_threshold`` can be declared; without this it was parsed,
        validated and then read by nothing.
        """
        return self.utilisation >= self.warn_threshold


@dataclass(frozen=True)
class Snapshot:
    """A complete, self-contained view of one period."""

    window: PeriodWindow
    currency: str
    base_ceiling: Money
    carry_in: Money
    ceiling: Money
    committed: Money
    reserved: Money
    categories: Mapping[str, CategorySnapshot]
    warn_threshold: Decimal
    halted: bool
    halt_reason: str
    now: datetime
    open_reservations: int = 0

    @property
    def used(self) -> Money:
        return self.committed + self.reserved

    @property
    def available(self) -> Money:
        return self.ceiling - self.used

    @property
    def utilisation(self) -> Decimal:
        if self.ceiling.is_zero:
            return Decimal(0) if self.used.is_zero else Decimal(1)
        return self.used.as_decimal() / self.ceiling.as_decimal()

    @property
    def warn_tripped(self) -> bool:
        """True once the soft threshold is crossed, whether or not it is over."""
        return self.utilisation >= self.warn_threshold

    @property
    def exhausted(self) -> bool:
        return self.available.micros <= 0

    def to_mapping(self) -> dict:
        return {
            "period": self.window.key,
            "period_start": self.window.start.isoformat(),
            "period_end": self.window.end.isoformat(),
            "now": self.now.isoformat(),
            "currency": self.currency,
            "base_ceiling": self.base_ceiling.format(),
            "carry_in": self.carry_in.format(),
            "ceiling": self.ceiling.format(),
            "committed": self.committed.format(),
            "reserved": self.reserved.format(),
            "available": self.available.format(),
            "utilisation": str(self.utilisation.quantize(Decimal("0.0001"))),
            "warn_threshold": str(self.warn_threshold),
            "warn_tripped": self.warn_tripped,
            "exhausted": self.exhausted,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "open_reservations": self.open_reservations,
            "categories": {
                name: {
                    "ceiling": entry.ceiling.format(),
                    "committed": entry.committed.format(),
                    "reserved": entry.reserved.format(),
                    "available": entry.available.format(),
                    "utilisation": str(entry.utilisation.quantize(Decimal("0.0001"))),
                    "warn_threshold": str(entry.warn_threshold),
                    "warn_tripped": entry.warn_tripped,
                }
                for name, entry in sorted(self.categories.items())
            },
        }


@dataclass(frozen=True)
class Overlay:
    """Hypothetical spend layered on top of real state, for simulation."""

    total: Money = field(default_factory=Money.zero)
    by_category: Mapping[str, Money] = field(default_factory=dict)

    def plus(self, category: str, amount: Money) -> "Overlay":
        merged = dict(self.by_category)
        merged[category] = merged.get(category, Money.zero()) + amount
        return Overlay(total=self.total + amount, by_category=merged)

    def category(self, name: str) -> Money:
        return self.by_category.get(name, Money.zero())


EMPTY_OVERLAY = Overlay()


@dataclass(frozen=True)
class Decision:
    """The result of evaluating a request. Always answerable, never an opinion."""

    allowed: bool
    request: SpendRequest
    quote: Quote
    snapshot: Snapshot
    reason: Optional[DenialReason] = None
    message: str = ""
    headroom: Optional[Money] = None

    @property
    def amount(self) -> Optional[Money]:
        return self.quote.amount

    def raise_if_denied(self) -> "Decision":
        if self.allowed:
            return self
        error_type = _REASON_ERRORS.get(self.reason, DeniedError)
        raise error_type(self.message, self)

    def to_mapping(self) -> dict:
        return {
            "allowed": self.allowed,
            "key": self.request.key,
            "units": str(self.quote.units),
            "category": self.request.category,
            "amount": None if self.amount is None else self.amount.format(),
            "estimated": self.quote.estimated,
            "reason": None if self.reason is None else self.reason.value,
            "message": self.message,
            "headroom": None if self.headroom is None else self.headroom.format(),
            "period": self.snapshot.window.key,
        }

    def __str__(self) -> str:
        verdict = "ALLOW" if self.allowed else f"DENY[{self.reason.value}]"
        return f"{verdict} {self.request.key}: {self.message}"


class Ticket:
    """Handle to an in-flight reservation, yielded by :meth:`SpendGuard.charge`."""

    __slots__ = ("reservation", "_actual", "_actual_set")

    def __init__(self, reservation: Reservation) -> None:
        self.reservation = reservation
        self._actual: Optional[Money] = None
        self._actual_set = False

    @property
    def id(self) -> str:
        return self.reservation.id

    @property
    def estimate(self) -> Money:
        return self.reservation.estimate

    @property
    def actual_amount(self) -> Optional[Money]:
        return self._actual

    @property
    def reconciled(self) -> bool:
        return self._actual_set

    def actual(self, amount: MoneyLike) -> "Ticket":
        """Record what the call really cost.

        Calling this also means "the spend happened". If the block then raises,
        the guard commits the stated amount instead of releasing it, because
        money that left the account does not come back when Python unwinds.
        """
        self._actual = Money.parse(amount, rounding=Rounding.UP)
        self._actual_set = True
        return self


class SpendGuard:
    """Reserve, commit, release. The single point every caller wraps."""

    def __init__(
        self,
        budget: BudgetSpec,
        prices: Optional[PriceTable] = None,
        ledger: Optional[Ledger] = None,
        *,
        clock: Optional[Clock] = None,
        default_lease: timedelta = DEFAULT_LEASE,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if not isinstance(budget, BudgetSpec):
            budget = BudgetSpec.from_mapping(budget)
        if default_lease.total_seconds() <= 0:
            raise ConfigError(
                "default_lease must be positive; a zero lease would release "
                "every reservation the instant it was taken"
            )
        self.budget = budget
        self.prices = prices if prices is not None else PriceTable()
        self.ledger: Ledger = ledger if ledger is not None else MemoryLedger()
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.default_lease = default_lease
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    # -- time and state ----------------------------------------------------

    def _now(self, now: Optional[datetime] = None) -> datetime:
        return ensure_utc(now) if now is not None else ensure_utc(self.clock.now())

    def state(self) -> LedgerState:
        """Replay the ledger.

        Re-read every time rather than caching: a second process appending to
        the same file must be visible immediately, and a stale in-memory total
        is precisely how a ceiling gets overrun.
        """
        return replay(self.ledger)

    def window(self, now: Optional[datetime] = None) -> PeriodWindow:
        return self.budget.period.window(self._now(now))

    # -- carry -------------------------------------------------------------

    def carry_in(
        self,
        window: PeriodWindow,
        state: Optional[LedgerState] = None,
    ) -> Money:
        """Balance carried into ``window`` from earlier periods.

        A period with no ledger activity at all contributes no *unused* budget
        and breaks the chain in that direction. Otherwise a system that was
        simply switched off for six months would wake up with six months of
        "unused" budget, which is the opposite of a ceiling.

        A **deficit** is not forgiven by the same silence. Silence is not
        repayment, and an overspend large enough to zero the next ceiling
        guarantees that every call in that period is refused — which writes
        nothing, which would make the period look idle and wipe the debt. The
        forgiving direction breaks the chain; the strict direction survives it.

        For the same reason the chain compounds on the *unfloored* balance:
        :func:`~spend_guard.budget.effective_ceiling` floors the ceiling a
        period can actually spend at zero, but a debt bigger than one period's
        budget is only repaid by the part that period could absorb.
        """
        spec = self.budget.rollover
        if not spec.carries or spec.max_lookback <= 0:
            return Money.zero()
        state = state if state is not None else self.state()
        previous: List[PeriodWindow] = []
        cursor = window
        for _ in range(spec.max_lookback):
            cursor = self.budget.period.previous_window(cursor)
            previous.append(cursor)
        previous.reverse()
        carry = Money.zero()
        for earlier in previous:
            if earlier.key not in state.periods_seen:
                # Drop any unused balance, keep any debt.
                carry = carry.clamp_max(Money.zero())
                continue
            carry = compute_carry(
                self.budget.ceiling + carry, state.committed(earlier.key), spec
            )
        return carry

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        now: Optional[datetime] = None,
        *,
        state: Optional[LedgerState] = None,
        overlay: Overlay = EMPTY_OVERLAY,
    ) -> Snapshot:
        moment = self._now(now)
        state = state if state is not None else self.state()
        window = self.budget.period.window(moment)
        carry = self.carry_in(window, state)
        ceiling = effective_ceiling(self.budget.ceiling, carry)
        committed = state.committed(window.key) + overlay.total
        reserved = state.reserved(window.key, moment)

        names = set(self.budget.categories)
        names.update(state.categories_used(window.key))
        names.update(overlay.by_category)
        categories: Dict[str, CategorySnapshot] = {}
        for name in names:
            declared = self.budget.category(name)
            categories[name] = CategorySnapshot(
                name=name,
                ceiling=declared.ceiling if declared is not None else ceiling,
                committed=state.committed_in_category(window.key, name)
                + overlay.category(name),
                reserved=state.reserved_in_category(window.key, name, moment),
                warn_threshold=self.budget.warn_threshold_for(name),
            )

        return Snapshot(
            window=window,
            currency=self.budget.currency,
            base_ceiling=self.budget.ceiling,
            carry_in=carry,
            ceiling=ceiling,
            committed=committed,
            reserved=reserved,
            categories=categories,
            warn_threshold=self.budget.warn_threshold,
            halted=state.halted,
            halt_reason=state.halt_reason,
            now=moment,
            open_reservations=len(state.open_reservations(window.key, moment)),
        )

    # -- evaluation --------------------------------------------------------

    def quote(self, key: str, units: Units = 1) -> Quote:
        return self.prices.quote(key, units)

    def evaluate(
        self,
        request: SpendRequest,
        *,
        now: Optional[datetime] = None,
        state: Optional[LedgerState] = None,
        overlay: Overlay = EMPTY_OVERLAY,
    ) -> Decision:
        """Decide, without writing anything.

        The order of the checks is the order in :class:`DenialReason`, and it
        is part of the contract. A halted guard says *halted* before anything
        else is even parsed — an operator staring at a frozen pipeline needs to
        be told the breaker is open, not that the request they sent to probe it
        was malformed. After that, an unpriced unit of work is refused *before*
        any budget arithmetic, so there is no path on which an unknown price
        reaches a comparison against a ceiling.
        """
        moment = self._now(now)
        state = state if state is not None else self.state()
        snap = self.snapshot(moment, state=state, overlay=overlay)

        if state.halted:
            # Deliberately before parsing and pricing: both can fail, and a
            # halted guard must answer with a Decision rather than an
            # exception, whatever the caller passed.
            try:
                halted_units = parse_units(request.units)
            except ConfigError:
                halted_units = Decimal(0)
            return Decision(
                allowed=False,
                request=request,
                quote=Quote(key=request.key, units=halted_units),
                snapshot=snap,
                reason=DenialReason.HALTED,
                message=(
                    "spending is halted"
                    + (f": {state.halt_reason}" if state.halt_reason else "")
                ),
            )

        try:
            units = parse_units(request.units)
        except ConfigError as exc:
            bad = Quote(key=request.key, units=Decimal(0))
            return Decision(
                allowed=False,
                request=request,
                quote=bad,
                snapshot=snap,
                reason=DenialReason.INVALID_REQUEST,
                message=str(exc),
            )

        if request.amount is not None:
            amount = Money.parse(request.amount, rounding=Rounding.UP)
            if amount.is_negative:
                # A negative cost is not a refund, it is a hole in the ceiling:
                # it would skip every comparison below and then *raise* the
                # available budget by its magnitude once held.
                return Decision(
                    allowed=False,
                    request=request,
                    quote=Quote(key=request.key, units=units),
                    snapshot=snap,
                    reason=DenialReason.INVALID_REQUEST,
                    message=(
                        f"an explicit amount cannot be negative, got "
                        f"{amount.format()}; record a correction in the ledger "
                        "instead of reserving a negative cost"
                    ),
                )
            quote = Quote(
                key=request.key,
                units=units,
                amount=amount,
                rule_key=None,
                unit="explicit",
                estimated=False,
            )
        else:
            try:
                quote = self.prices.quote(request.key, units)
            except (ConfigError, MoneyError) as exc:
                return Decision(
                    allowed=False,
                    request=request,
                    quote=Quote(key=request.key, units=units),
                    snapshot=snap,
                    reason=DenialReason.INVALID_REQUEST,
                    message=str(exc),
                )

        if not request.key:
            return Decision(
                allowed=False,
                request=request,
                quote=quote,
                snapshot=snap,
                reason=DenialReason.INVALID_REQUEST,
                message="a spend request needs a work key",
            )

        if not self.budget.knows_category(request.category):
            if request.category == UNCATEGORISED:
                message = (
                    "this budget refuses uncategorised spend; name one of: "
                    + ", ".join(sorted(self.budget.categories))
                )
            else:
                message = (
                    f"category {request.category!r} is not declared in the budget; "
                    "declared: " + ", ".join(sorted(self.budget.categories))
                )
            return Decision(
                allowed=False,
                request=request,
                quote=quote,
                snapshot=snap,
                reason=DenialReason.UNKNOWN_CATEGORY,
                message=message,
            )

        if not quote.known:
            return Decision(
                allowed=False,
                request=request,
                quote=quote,
                snapshot=snap,
                reason=DenialReason.UNKNOWN_PRICE,
                message=(
                    f"no price is declared for {request.key!r}; refusing to treat "
                    "an unpriced unit of work as free. Add a rule, pass an "
                    "explicit amount, or set on_missing='estimate' with a "
                    "fallback_price."
                ),
            )

        amount = quote.require()

        # A declared cost of zero spends nothing, so an exhausted ceiling does
        # not need to refuse it. An *unknown* price is not a zero, and was
        # already refused above.
        if amount.is_positive:
            declared = self.budget.category(request.category)
            if declared is not None:
                entry = snap.categories[request.category]
                if amount.micros > entry.available.micros:
                    return Decision(
                        allowed=False,
                        request=request,
                        quote=quote,
                        snapshot=snap,
                        reason=DenialReason.CATEGORY_EXCEEDED,
                        message=(
                            f"category {request.category!r} has "
                            f"{entry.available.format()} left of "
                            f"{entry.ceiling.format()}; this call needs "
                            f"{amount.format()}"
                        ),
                        headroom=entry.available,
                    )
            if amount.micros > snap.available.micros:
                return Decision(
                    allowed=False,
                    request=request,
                    quote=quote,
                    snapshot=snap,
                    reason=DenialReason.TOTAL_EXCEEDED,
                    message=(
                        f"period {snap.window.key} has {snap.available.format()} "
                        f"left of {snap.ceiling.format()}; this call needs "
                        f"{amount.format()}"
                    ),
                    headroom=snap.available,
                )

        return Decision(
            allowed=True,
            request=request,
            quote=quote,
            snapshot=snap,
            message=f"{amount.format()} fits under {snap.available.format()} remaining",
            headroom=snap.available,
        )

    # -- the protocol ------------------------------------------------------

    def try_reserve(
        self,
        key: str,
        units: Units = 1,
        *,
        category: str = UNCATEGORISED,
        amount: Optional[MoneyLike] = None,
        lease: Optional[timedelta] = None,
        note: str = "",
        now: Optional[datetime] = None,
    ) -> Tuple[Decision, Optional[Reservation]]:
        """Evaluate and, if allowed, hold the budget. Never raises on denial."""
        request = SpendRequest(
            key=key,
            units=units,
            category=category,
            amount=None if amount is None else Money.parse(amount, rounding=Rounding.UP),
            note=note,
        )
        moment = self._now(now)
        decision = self.evaluate(request, now=moment)
        if not decision.allowed:
            return decision, None

        hold = lease if lease is not None else self.default_lease
        if hold.total_seconds() <= 0:
            raise ConfigError("lease must be positive")
        reservation_id = self._id_factory()
        entry = self.ledger.append(
            Entry(
                seq=0,
                ts=moment,
                type=EntryType.RESERVE,
                reservation_id=reservation_id,
                period_key=decision.snapshot.window.key,
                category=category,
                work_key=key,
                units=decision.quote.units,
                amount=decision.quote.require(),
                estimated=decision.quote.estimated,
                reason=note,
                lease_until=moment + hold,
            )
        )
        reservation = Reservation(
            id=reservation_id,
            period_key=entry.period_key,
            work_key=key,
            units=entry.units,
            estimate=entry.amount,
            created_at=entry.ts,
            lease_until=entry.lease_until,
            category=category,
            status=ReservationStatus.OPEN,
            estimated_price=entry.estimated,
            note=note,
        )
        return decision, reservation

    def reserve(
        self,
        key: str,
        units: Units = 1,
        *,
        category: str = UNCATEGORISED,
        amount: Optional[MoneyLike] = None,
        lease: Optional[timedelta] = None,
        note: str = "",
        now: Optional[datetime] = None,
    ) -> Reservation:
        """Hold budget or raise. The fail-closed entry point."""
        decision, reservation = self.try_reserve(
            key,
            units,
            category=category,
            amount=amount,
            lease=lease,
            note=note,
            now=now,
        )
        if reservation is None:
            decision.raise_if_denied()
        assert reservation is not None  # raise_if_denied never returns here
        return reservation

    def commit(
        self,
        reservation_id: str,
        actual: Optional[MoneyLike] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Reservation:
        """Reconcile a reservation with what the call really cost.

        Committing more than was reserved is allowed and is *not* an error: the
        money already left the account and the ledger's job is to say so. The
        overrun shows up as a smaller (or negative) headroom, and the next
        reserve is what refuses.
        """
        moment = self._now(now)
        state = self.state()
        reservation = state.get(reservation_id)
        if reservation is None:
            raise UnknownReservationError(f"no reservation {reservation_id!r}")

        resolved = (
            reservation.estimate
            if actual is None
            else Money.parse(actual, rounding=Rounding.UP)
        )
        if resolved.is_negative:
            raise ReservationStateError(
                "an actual cost cannot be negative; release the reservation "
                "instead, or record a separate correction"
            )

        if reservation.status is ReservationStatus.COMMITTED:
            recorded = reservation.actual or Money.zero()
            if actual is None or recorded == resolved:
                # A retried commit is the normal shape of an at-least-once
                # caller. Return what is already recorded; do not charge twice.
                return reservation
            raise ReservationConflictError(
                f"reservation {reservation_id} is already committed at "
                f"{recorded.format()}; refusing to overwrite it with "
                f"{resolved.format()}",
                recorded=recorded,
                offered=resolved,
            )
        if reservation.status is ReservationStatus.RELEASED:
            raise ReservationStateError(
                f"reservation {reservation_id} was released; it cannot be "
                "committed afterwards"
            )

        late = reservation.status is ReservationStatus.EXPIRED or (
            moment >= reservation.lease_until
        )
        self.ledger.append(
            Entry(
                seq=0,
                ts=moment,
                type=EntryType.COMMIT,
                reservation_id=reservation_id,
                period_key=reservation.period_key,
                category=reservation.category,
                work_key=reservation.work_key,
                units=reservation.units,
                amount=resolved,
                estimated=reservation.estimated_price and actual is None,
                reason="late" if late else "",
            )
        )
        return self.state().reservations[reservation_id]

    def release(
        self,
        reservation_id: str,
        *,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> Reservation:
        """Give the budget back because the call did not happen."""
        moment = self._now(now)
        state = self.state()
        reservation = state.get(reservation_id)
        if reservation is None:
            raise UnknownReservationError(f"no reservation {reservation_id!r}")
        if reservation.status is ReservationStatus.COMMITTED:
            raise ReservationStateError(
                f"reservation {reservation_id} is committed; a spend that "
                "happened cannot be released"
            )
        if reservation.status is ReservationStatus.RELEASED:
            raise ReservationStateError(
                f"reservation {reservation_id} is already released"
            )
        self.ledger.append(
            Entry(
                seq=0,
                ts=moment,
                type=EntryType.RELEASE,
                reservation_id=reservation_id,
                period_key=reservation.period_key,
                category=reservation.category,
                work_key=reservation.work_key,
                amount=reservation.estimate,
                reason=reason,
            )
        )
        return self.state().reservations[reservation_id]

    def sweep(
        self,
        *,
        now: Optional[datetime] = None,
        apply: bool = True,
    ) -> List[Reservation]:
        """Record expiry for reservations whose lease has run out.

        This is bookkeeping, not recovery. The budget is already free: the
        arithmetic in :class:`~spend_guard.state.LedgerState` ignores an
        expired hold whether or not this ever runs. Sweeping writes the fact
        down so an operator reading the ledger can see the call that vanished.
        """
        moment = self._now(now)
        state = self.state()
        stale = state.expired(moment)
        if not apply:
            return stale
        for reservation in stale:
            self.ledger.append(
                Entry(
                    seq=0,
                    ts=moment,
                    type=EntryType.EXPIRE,
                    reservation_id=reservation.id,
                    period_key=reservation.period_key,
                    category=reservation.category,
                    work_key=reservation.work_key,
                    amount=reservation.estimate,
                    reason="lease expired",
                )
            )
        final = self.state()
        return [final.reservations[item.id] for item in stale]

    # -- circuit breaker ---------------------------------------------------

    def halt(self, reason: str = "", *, now: Optional[datetime] = None) -> None:
        """Refuse everything until explicitly resumed."""
        self.ledger.append(
            Entry(
                seq=0,
                ts=self._now(now),
                type=EntryType.HALT,
                reason=reason,
            )
        )

    def resume(self, reason: str = "", *, now: Optional[datetime] = None) -> None:
        """Lift a halt. Deliberately a separate, explicit action."""
        self.ledger.append(
            Entry(
                seq=0,
                ts=self._now(now),
                type=EntryType.RESUME,
                reason=reason,
            )
        )

    def note(self, text: str, *, now: Optional[datetime] = None, **meta) -> None:
        """Append a human annotation to the ledger."""
        self.ledger.append(
            Entry(
                seq=0,
                ts=self._now(now),
                type=EntryType.NOTE,
                reason=text,
                meta=dict(meta),
            )
        )

    # -- ergonomics --------------------------------------------------------

    @contextmanager
    def charge(
        self,
        key: str,
        units: Units = 1,
        *,
        category: str = UNCATEGORISED,
        amount: Optional[MoneyLike] = None,
        lease: Optional[timedelta] = None,
        note: str = "",
    ) -> Iterator[Ticket]:
        """Reserve, run the body, then commit or release.

        Leaving the block normally commits: the estimate if the caller said
        nothing, the reconciled figure if it called :meth:`Ticket.actual`.
        Raising releases, *unless* an actual cost was recorded first, in which
        case the spend is committed because it really happened.
        """
        reservation = self.reserve(
            key, units, category=category, amount=amount, lease=lease, note=note
        )
        ticket = Ticket(reservation)
        try:
            yield ticket
        except BaseException as exc:
            if ticket.reconciled:
                self.commit(reservation.id, ticket.actual_amount)
            else:
                self.release(
                    reservation.id,
                    reason=f"caller raised {type(exc).__name__}",
                )
            raise
        self.commit(reservation.id, ticket.actual_amount)
