"""Replaying the ledger into state.

``replay`` is pure: entries in, state out, no clock and no files. It is also
the last line of defence against a ledger that has been edited by hand, so it
refuses to produce a plausible-looking total from an impossible history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from spend_guard import Entry, EntryType, Money, ReservationStatus, replay
from spend_guard.errors import LedgerCorruptError

T = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
LEASE = T + timedelta(minutes=15)


def reserve(seq=1, rid="r1", amount="1.00", category="text", lease=LEASE, ts=T):
    return Entry(
        seq=seq,
        ts=ts,
        type=EntryType.RESERVE,
        reservation_id=rid,
        period_key="2026-03",
        category=category,
        work_key="text.generate",
        amount=Money.parse(amount),
        lease_until=lease,
    )


def finish(seq, rid, kind, amount="1.00", ts=T):
    return Entry(
        seq=seq,
        ts=ts,
        type=kind,
        reservation_id=rid,
        period_key="2026-03",
        category="text",
        amount=Money.parse(amount),
    )


def test_an_empty_ledger_is_a_clean_state():
    """First run has no history and must not look like an overspend."""
    state = replay([])
    assert state.committed("2026-03") == Money.zero()
    assert state.reserved("2026-03", T) == Money.zero()
    assert not state.halted


def test_a_reservation_holds_budget_until_its_lease_ends():
    """The hold is what makes a pre-flight check able to refuse anything."""
    state = replay([reserve()])
    assert state.reserved("2026-03", T) == Money.parse("1.00")
    assert state.reserved("2026-03", LEASE) == Money.zero()


def test_a_commit_moves_a_hold_into_committed_spend():
    """Reserved and committed must never double count the same call."""
    state = replay([reserve(), finish(2, "r1", EntryType.COMMIT, "1.20")])
    assert state.committed("2026-03") == Money.parse("1.20")
    assert state.reserved("2026-03", T) == Money.zero()


def test_a_release_returns_the_hold_and_charges_nothing():
    """A call that never ran must cost nothing, immediately."""
    state = replay([reserve(), finish(2, "r1", EntryType.RELEASE)])
    assert state.committed("2026-03") == Money.zero()
    assert state.reserved("2026-03", T) == Money.zero()
    assert state.get("r1").status is ReservationStatus.RELEASED


def test_committed_totals_are_tracked_per_category():
    """A category ceiling needs its own total, not a share of the global one."""
    entries = [
        reserve(1, "r1", "1.00", "text"),
        finish(2, "r1", EntryType.COMMIT, "1.00"),
        Entry(
            seq=3,
            ts=T,
            type=EntryType.RESERVE,
            reservation_id="r2",
            period_key="2026-03",
            category="images",
            amount=Money.parse("2.00"),
            lease_until=LEASE,
        ),
        Entry(
            seq=4,
            ts=T,
            type=EntryType.COMMIT,
            reservation_id="r2",
            period_key="2026-03",
            category="images",
            amount=Money.parse("2.00"),
        ),
    ]
    state = replay(entries)
    assert state.committed_in_category("2026-03", "text") == Money.parse("1.00")
    assert state.committed_in_category("2026-03", "images") == Money.parse("2.00")
    assert state.committed("2026-03") == Money.parse("3.00")


def test_spend_is_attributed_to_the_period_recorded_on_the_reservation():
    """A call that starts on the 31st and commits on the 1st belongs to the 31st.

    Attributing by commit time would move end-of-period spend into the next
    budget, exactly when the ceiling is under pressure.
    """
    late = T.replace(day=31)
    entries = [
        reserve(1, "r1", "5.00", ts=late),
        finish(2, "r1", EntryType.COMMIT, "5.00", ts=datetime(2026, 4, 1, tzinfo=timezone.utc)),
    ]
    state = replay(entries)
    assert state.committed("2026-03") == Money.parse("5.00")
    assert state.committed("2026-04") == Money.zero()


def test_expired_reservations_are_listed_for_sweeping():
    """An operator needs to see the calls that vanished, not just the freed budget."""
    state = replay([reserve()])
    assert [item.id for item in state.expired(LEASE)] == ["r1"]
    assert state.expired(T) == []


def test_a_duplicate_reservation_id_is_corruption():
    """Two opens for one id means two histories were merged or one was replayed."""
    with pytest.raises(LedgerCorruptError):
        replay([reserve(1, "r1"), reserve(2, "r1")])


def test_a_reservation_without_a_lease_is_corruption():
    """An unbounded hold can never be reclaimed after a crash, so it cannot exist.

    Refusing it at replay stops an older or hand-written entry from creating a
    permanent leak that no sweep can clear.
    """
    entry = Entry(
        seq=1,
        ts=T,
        type=EntryType.RESERVE,
        reservation_id="r1",
        period_key="2026-03",
        amount=Money.parse("1.00"),
    )
    with pytest.raises(LedgerCorruptError) as excinfo:
        replay([entry])
    assert "lease" in str(excinfo.value)


def test_committing_an_unknown_reservation_is_corruption():
    """A commit with no matching reserve means the hold was never counted."""
    with pytest.raises(LedgerCorruptError):
        replay([finish(1, "ghost", EntryType.COMMIT)])


def test_two_commits_for_one_reservation_are_corruption():
    """Charging twice for one call is the error this ledger exists to make visible."""
    with pytest.raises(LedgerCorruptError):
        replay(
            [
                reserve(),
                finish(2, "r1", EntryType.COMMIT),
                finish(3, "r1", EntryType.COMMIT),
            ]
        )


def test_releasing_a_committed_reservation_is_corruption():
    """Money that left the account cannot be given back by a ledger entry."""
    with pytest.raises(LedgerCorruptError):
        replay(
            [
                reserve(),
                finish(2, "r1", EntryType.COMMIT),
                finish(3, "r1", EntryType.RELEASE),
            ]
        )


def test_an_expired_reservation_can_still_be_committed():
    """The lease freed the budget, but the call may still have run and billed.

    Refusing this commit would lose real spend; the ledger's job is to record
    what happened, including when it happened late.
    """
    state = replay(
        [
            reserve(),
            Entry(
                seq=2,
                ts=LEASE,
                type=EntryType.EXPIRE,
                reservation_id="r1",
                period_key="2026-03",
                category="text",
            ),
            finish(3, "r1", EntryType.COMMIT, "1.00", ts=LEASE),
        ]
    )
    assert state.committed("2026-03") == Money.parse("1.00")
    assert state.get("r1").status is ReservationStatus.COMMITTED


def test_halt_and_resume_toggle_the_flag():
    """The circuit breaker lives in the same append-only history as the spend."""
    halted = replay([Entry(seq=1, ts=T, type=EntryType.HALT, reason="incident")])
    assert halted.halted and halted.halt_reason == "incident"
    resumed = replay(
        [
            Entry(seq=1, ts=T, type=EntryType.HALT, reason="incident"),
            Entry(seq=2, ts=T, type=EntryType.RESUME),
        ]
    )
    assert not resumed.halted


def test_notes_do_not_change_any_total():
    """Annotation must stay annotation, or an operator comment becomes spend."""
    state = replay([reserve(), Entry(seq=2, ts=T, type=EntryType.NOTE, reason="hello")])
    assert state.reserved("2026-03", T) == Money.parse("1.00")
    assert state.committed("2026-03") == Money.zero()


def test_charged_is_zero_until_a_reservation_commits():
    """An open hold is not spend; reporting it as spend double counts with reserved."""
    state = replay([reserve()])
    assert state.get("r1").charged == Money.zero()


def test_periods_seen_records_every_period_with_activity():
    """Rollover breaks its chain on an idle period, so it needs this set to be exact."""
    state = replay([reserve()])
    assert "2026-03" in state.periods_seen
    assert "2026-02" not in state.periods_seen
