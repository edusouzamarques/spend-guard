"""Headline behaviour 2: a crash between reserve and commit does not leak budget.

Holding budget before the call is the only way to refuse it. But a process that
dies between reserving and committing leaves a hold nobody will ever close, and
a guard that leaks a little on every crash eventually refuses everything - which
looks exactly like a working budget until someone checks the invoice and finds
it half spent.

The fix is that a hold is *leased*. The arithmetic asks the clock, so an
expired hold stops counting the instant its lease lapses, whether or not any
cleanup ever runs, whether or not the original process comes back.

Sweeping only writes the fact into the ledger. Several of these tests never
sweep at all, on purpose: recovery must not depend on anything running.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from spend_guard import (
    BudgetSpec,
    FixedClock,
    MemoryLedger,
    Money,
    ReservationStatus,
    SpendGuard,
)
from spend_guard.errors import (
    BudgetExceededError,
    ConfigError,
    ReservationStateError,
)


@pytest.fixture
def tight(budget_mapping, prices, ledger, clock, ids):
    """A budget with room for exactly two 40.00 calls, to make a leak obvious."""
    spec = BudgetSpec.from_mapping(dict(budget_mapping, ceiling="100.00", categories={}))
    return SpendGuard(
        spec,
        prices,
        ledger,
        clock=clock,
        id_factory=ids,
        default_lease=timedelta(minutes=15),
    )


def _reserve_40(guard):
    return guard.reserve("image.render", 1000)  # 1000 x 0.04 = 40.00


def test_an_open_reservation_holds_budget_while_its_lease_is_alive(tight):
    """Without the hold, two concurrent callers both pass the same ceiling check."""
    _reserve_40(tight)
    assert tight.snapshot().available == Money.parse("60.00")


def test_holds_accumulate_and_the_ceiling_refuses_the_call_that_breaks_it(tight):
    """Three 40.00 calls do not fit in 100.00, and the third is the one refused."""
    _reserve_40(tight)
    _reserve_40(tight)
    with pytest.raises(BudgetExceededError):
        _reserve_40(tight)


def test_an_abandoned_reservation_stops_counting_when_its_lease_lapses(tight, clock):
    """The core property, with nothing at all running to clean up.

    Simulating the crash by simply never committing, then moving the clock past
    the lease. No sweep, no restart, no background job: the budget is free
    because the arithmetic says so.
    """
    _reserve_40(tight)
    assert tight.snapshot().available == Money.parse("60.00")
    clock.advance(timedelta(minutes=16))
    assert tight.snapshot().available == Money.parse("100.00")


def test_budget_freed_by_an_expired_lease_can_actually_be_spent_again(tight, clock):
    """Reporting the budget as free is not enough; the next call has to succeed."""
    _reserve_40(tight)
    _reserve_40(tight)
    clock.advance(timedelta(minutes=16))
    assert _reserve_40(tight).estimate == Money.parse("40.00")


def test_recovery_needs_no_sweep_and_no_surviving_process(
    budget_mapping, prices, clock, ids
):
    """A brand new guard reading the same ledger sees the same freed budget.

    This is the crash case as it really happens: the process that reserved is
    gone, and a different one starts up later and reads the file.
    """
    spec = BudgetSpec.from_mapping(dict(budget_mapping, ceiling="100.00", categories={}))
    shared = MemoryLedger()
    first = SpendGuard(spec, prices, shared, clock=clock, id_factory=ids)
    first.reserve("image.render", 1000)
    del first  # the process dies here

    clock.advance(timedelta(minutes=16))
    second = SpendGuard(spec, prices, shared, clock=FixedClock(clock.now()))
    assert second.snapshot().available == Money.parse("100.00")


def test_a_leaked_hold_is_still_visible_as_an_open_reservation(tight, clock):
    """Freeing the budget must not hide the evidence that a call went missing."""
    reservation = tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    state = tight.state()
    assert state.get(reservation.id).status is ReservationStatus.OPEN
    assert [item.id for item in state.expired(clock.now())] == [reservation.id]


def test_sweeping_records_the_expiry_without_changing_any_total(tight, clock):
    """Sweeping is bookkeeping. If it moved a number, it would be doing recovery.

    Asserting the totals are identical before and after is what pins that the
    lease, not the sweep, is what frees the budget.
    """
    tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    before = tight.snapshot()
    swept = tight.sweep()
    after = tight.snapshot()
    assert len(swept) == 1
    assert swept[0].status is ReservationStatus.EXPIRED
    assert (after.available, after.committed) == (before.available, before.committed)


def test_sweeping_is_idempotent(tight, clock):
    """A cron job that runs every minute must not append an entry every minute."""
    tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    assert len(tight.sweep()) == 1
    assert tight.sweep() == []


def test_sweep_without_apply_reports_but_writes_nothing(tight, clock, ledger):
    """Looking at the damage must not be a write, so an operator can look safely."""
    tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    entries_before = len(ledger)
    stale = tight.sweep(apply=False)
    assert len(stale) == 1
    assert len(ledger) == entries_before


def test_a_late_commit_after_expiry_still_charges_the_money(tight, clock):
    """The lease freed the budget, but the API call may have run anyway.

    Dropping this commit would mean real spend never reaches the ledger, and
    the next period would start from a number that is quietly too low.
    """
    reservation = tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    tight.sweep()
    committed = tight.commit(reservation.id, Money.parse("41.00"))
    assert committed.status is ReservationStatus.COMMITTED
    assert tight.snapshot().committed == Money.parse("41.00")


def test_a_late_commit_is_marked_late_in_the_ledger(tight, clock, ledger):
    """An operator reading the history should see which charges arrived after expiry."""
    reservation = tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    tight.commit(reservation.id)
    commit_entry = [item for item in ledger if item.type.value == "commit"][0]
    assert commit_entry.reason == "late"


def test_a_timely_commit_is_not_marked_late(tight, clock, ledger):
    """The marker has to be selective or it means nothing."""
    reservation = tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=1))
    tight.commit(reservation.id)
    commit_entry = [item for item in ledger if item.type.value == "commit"][0]
    assert commit_entry.reason == ""


def test_a_custom_lease_overrides_the_default_for_a_long_call(tight, clock):
    """A ninety-minute render must not have its budget reclaimed at minute fifteen."""
    tight.reserve("image.render", 1000, lease=timedelta(hours=2))
    clock.advance(timedelta(minutes=30))
    assert tight.snapshot().available == Money.parse("60.00")


def test_a_short_lease_frees_budget_sooner(tight, clock):
    """Fast calls should return their headroom quickly under a busy ceiling."""
    tight.reserve("image.render", 1000, lease=timedelta(seconds=30))
    clock.advance(timedelta(seconds=31))
    assert tight.snapshot().available == Money.parse("100.00")


def test_a_zero_or_negative_lease_is_rejected(budget, prices, ledger, clock):
    """A hold that expires the instant it is taken protects nothing at all."""
    with pytest.raises(ConfigError):
        SpendGuard(budget, prices, ledger, clock=clock, default_lease=timedelta(0))


def test_an_expired_reservation_can_still_be_released(tight, clock):
    """A caller that recovers and knows the call never ran should be able to say so."""
    reservation = tight.reserve("image.render", 1000)
    clock.advance(timedelta(minutes=16))
    released = tight.release(reservation.id, reason="call never ran")
    assert released.status is ReservationStatus.RELEASED


def test_a_released_reservation_cannot_be_committed_afterwards(tight):
    """Once a caller declares the call did not happen, a later charge is a bug."""
    reservation = tight.reserve("image.render", 1000)
    tight.release(reservation.id)
    with pytest.raises(ReservationStateError):
        tight.commit(reservation.id)


def test_the_lease_is_written_to_the_ledger_so_recovery_survives_a_restart(
    tight, ledger
):
    """Recovery reads the file; a lease held only in memory would die with the process."""
    tight.reserve("image.render", 1000)
    reserve_entry = list(ledger)[0]
    assert reserve_entry.lease_until is not None
    assert reserve_entry.lease_until > reserve_entry.ts
