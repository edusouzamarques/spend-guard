"""The reserve / commit / release protocol.

The order of the checks, what a denial says, what a commit does when the real
invoice is bigger than the estimate, and what happens when a caller retries.
These are the behaviours an integrator will lean on without reading the source.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from spend_guard import (
    BudgetSpec,
    DenialReason,
    Money,
    ReservationStatus,
    SpendGuard,
    SpendRequest,
)
from spend_guard.errors import (
    BudgetExceededError,
    DeniedError,
    HaltedError,
    ReservationConflictError,
    ReservationStateError,
    UnknownReservationError,
)


# -- the happy path --------------------------------------------------------


def test_a_reservation_holds_the_quoted_estimate(guard):
    """The held amount comes from the price table, not from the caller's optimism."""
    reservation = guard.reserve("image.render", 10, category="images")
    assert reservation.estimate == Money.parse("0.40")
    assert reservation.status is ReservationStatus.OPEN


def test_commit_without_an_actual_charges_the_estimate(guard):
    """Most callers never learn the real cost; the estimate has to be usable."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id)
    assert guard.snapshot().committed == Money.parse("0.40")


def test_commit_with_an_actual_replaces_the_estimate(guard):
    """An estimate is not a receipt. Reconciliation is the second half of the protocol."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("0.37"))
    assert guard.snapshot().committed == Money.parse("0.37")


def test_committing_releases_the_hold_so_it_is_not_counted_twice(guard):
    """Reserved plus committed must never double count one call."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("0.40"))
    snap = guard.snapshot()
    assert snap.reserved == Money.zero()
    assert snap.used == Money.parse("0.40")


def test_releasing_returns_the_whole_hold(guard):
    """A call that did not happen costs nothing, with no residue."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.release(reservation.id, reason="upstream 500")
    snap = guard.snapshot()
    assert snap.used == Money.zero()
    assert snap.available == Money.parse("100.00")


# -- overrun ---------------------------------------------------------------


def test_committing_more_than_reserved_is_recorded_not_rejected(guard):
    """The money already left the account; refusing the commit would lose it.

    An estimate that was too low is a normal event. The ledger's job is to say
    what happened, and the next reserve is what refuses.
    """
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("95.00"))
    assert guard.snapshot().committed == Money.parse("95.00")


def test_an_overrun_drives_headroom_down_and_blocks_the_next_call(guard):
    """The refusal happens at the next reserve, which is the only place it can."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("99.99"))
    with pytest.raises(BudgetExceededError):
        guard.reserve("image.render", 10, category="images")


def test_headroom_can_go_negative_and_is_reported_honestly(guard):
    """Clamping "available" at zero would hide the size of the overrun."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("130.00"))
    assert guard.snapshot().available == Money.parse("-30.00")


def test_a_negative_actual_cost_is_rejected(guard):
    """A refund is a separate event; letting it through here would credit the ceiling."""
    reservation = guard.reserve("image.render", 10, category="images")
    with pytest.raises(ReservationStateError):
        guard.commit(reservation.id, Money.parse("-1.00"))


def test_a_negative_explicit_amount_is_refused_at_the_reserve(guard):
    """A negative cost is not a refund, it is an unlimited bypass of the ceiling.

    The ceiling comparisons only run for a positive amount, so a negative one
    used to sail through untested and then be *held*, which subtracts from
    ``reserved`` and raises ``available`` by its magnitude. One call to
    ``reserve("anything", amount="-1000")`` reopened a fully spent budget.
    """
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("100.00"))
    assert guard.snapshot().available == Money.zero()

    decision, held = guard.try_reserve(
        "refund.hack", amount="-1000", category="images"
    )
    assert decision.reason is DenialReason.INVALID_REQUEST
    assert held is None
    assert guard.snapshot().available == Money.zero()
    assert guard.try_reserve("image.render", 2500, category="images")[1] is None


def test_a_negative_explicit_amount_never_reaches_the_ledger(guard, ledger):
    """The bypass has to be refused before anything is written, or it renews."""
    with pytest.raises(DeniedError):
        guard.reserve("refund.hack", amount=Money.parse("-5.00"), category="images")
    assert len(ledger) == 0


# -- idempotency -----------------------------------------------------------


def test_committing_twice_with_the_same_amount_charges_once(guard):
    """At-least-once callers retry. A retry must not double the invoice."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("0.40"))
    guard.commit(reservation.id, Money.parse("0.40"))
    assert guard.snapshot().committed == Money.parse("0.40")


def test_committing_twice_without_an_amount_charges_once(guard):
    """The same retry, through the path that defaults to the estimate."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id)
    guard.commit(reservation.id)
    assert guard.snapshot().committed == Money.parse("0.40")


def test_committing_twice_with_different_amounts_raises_rather_than_guessing(guard):
    """Two different numbers for one call means the caller is confused. Say so.

    Silently keeping the first or the last would make the ledger disagree with
    the invoice in a way nobody could reconstruct later.
    """
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("0.40"))
    with pytest.raises(ReservationConflictError) as excinfo:
        guard.commit(reservation.id, Money.parse("0.90"))
    assert excinfo.value.recorded == Money.parse("0.40")


def test_releasing_a_committed_reservation_raises(guard):
    """Spend that happened cannot be un-spent by an API call."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id)
    with pytest.raises(ReservationStateError):
        guard.release(reservation.id)


def test_releasing_twice_raises(guard):
    """A double release would be harmless today and a double credit tomorrow."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.release(reservation.id)
    with pytest.raises(ReservationStateError):
        guard.release(reservation.id)


def test_committing_an_unknown_id_raises_a_named_error(guard):
    """A typo'd id must not create a floating charge against nothing."""
    with pytest.raises(UnknownReservationError):
        guard.commit("nope")


# -- categories ------------------------------------------------------------


def test_a_category_ceiling_refuses_before_the_total_does(guard):
    """Sub-budgets only mean something if they bind before the global one.

    The images category holds 30.00 inside a 100.00 total, so the refusal has
    to come from the category with 70.00 of global headroom still free.
    """
    reservation = guard.reserve("image.render", 100, category="images")  # 4.00
    guard.commit(reservation.id, Money.parse("29.00"))
    decision, _ = guard.try_reserve("image.render", 100, category="images")
    assert decision.reason is DenialReason.CATEGORY_EXCEEDED


def test_category_spend_also_counts_against_the_total(guard):
    """Categories partition the budget; they do not create extra budget."""
    reservation = guard.reserve("image.render", 10, category="images")
    guard.commit(reservation.id, Money.parse("10.00"))
    assert guard.snapshot().available == Money.parse("90.00")


def test_one_category_does_not_consume_another(guard):
    """A noisy neighbour in one category must not starve a quiet one."""
    reservation = guard.reserve("image.render", 100, category="images")
    guard.commit(reservation.id, Money.parse("30.00"))
    assert guard.snapshot().categories["text"].available == Money.parse("40.00")


def test_an_undeclared_category_is_refused_by_name(guard):
    """A typo'd category would otherwise silently escape its sub-ceiling."""
    decision, _ = guard.try_reserve("image.render", 1, category="imgaes")
    assert decision.reason is DenialReason.UNKNOWN_CATEGORY
    assert "imgaes" in decision.message


def test_uncategorised_spend_can_be_forbidden_outright(prices, ledger, clock):
    """Some operations want every charge attributed; the budget can demand it."""
    spec = BudgetSpec.from_mapping(
        {
            "ceiling": "100.00",
            "allow_uncategorised": False,
            "categories": {"images": {"ceiling": "30.00"}},
        }
    )
    guard = SpendGuard(spec, prices, ledger, clock=clock)
    decision, _ = guard.try_reserve("image.render", 1)
    assert decision.reason is DenialReason.UNKNOWN_CATEGORY


def test_uncategorised_spend_is_bounded_by_the_total_when_allowed(guard):
    """Without a sub-ceiling the global one still applies; nothing is unbounded."""
    with pytest.raises(BudgetExceededError):
        guard.reserve("image.render", 10000)  # 400.00 against a 100.00 ceiling


# -- boundaries ------------------------------------------------------------


def test_a_spend_exactly_equal_to_the_headroom_is_allowed(guard):
    """The ceiling is inclusive: spending the last cent is not an overspend."""
    reservation = guard.reserve("image.render", 2500)  # exactly 100.00
    assert reservation.estimate == Money.parse("100.00")


def test_one_micro_unit_over_the_headroom_is_refused(guard):
    """The boundary has to be tested from both sides or it is not a boundary."""
    guard.commit(guard.reserve("image.render", 2500).id, Money.parse("100.00"))
    decision, _ = guard.try_reserve(
        "video.render", amount=Money.from_micros(1)
    )
    assert decision.reason is DenialReason.TOTAL_EXCEEDED


def test_the_denial_reports_the_headroom_it_measured(guard):
    """"Over budget" is useless; "you have 4.20 and need 40.00" is actionable."""
    guard.commit(guard.reserve("image.render", 2500).id, Money.parse("95.80"))
    decision, _ = guard.try_reserve("image.render", 1000)
    assert decision.headroom == Money.parse("4.20")
    assert "4.20" in decision.message


# -- the circuit breaker ---------------------------------------------------


def test_halting_refuses_every_spend_regardless_of_headroom(guard):
    """A kill switch that still allows cheap calls is not a kill switch."""
    guard.halt("incident 44")
    with pytest.raises(HaltedError):
        guard.reserve("image.render", 1)


def test_a_halt_denial_carries_its_reason(guard):
    """Whoever hits the wall at 3am should learn why without reading the ledger."""
    guard.halt("suspected runaway loop")
    decision, _ = guard.try_reserve("image.render", 1)
    assert decision.reason is DenialReason.HALTED
    assert "suspected runaway loop" in decision.message


def test_halt_beats_an_unknown_price_in_the_reported_reason(guard):
    """The check order is part of the contract: the outermost stop reports first."""
    guard.halt("frozen")
    decision, _ = guard.try_reserve("never.priced")
    assert decision.reason is DenialReason.HALTED


def test_halt_beats_a_malformed_request_in_the_reported_reason(guard):
    """The operator debugging a frozen pipeline must be told the breaker is open.

    Validating the request first meant a halted guard answered INVALID_REQUEST
    to anything slightly off, which sends whoever is on call looking for a bug
    in their own arguments instead of at the halt somebody else set.
    """
    guard.halt("frozen")
    decision, _ = guard.try_reserve("image.render", -5, category="images")
    assert decision.reason is DenialReason.HALTED


def test_a_halted_guard_answers_rather_than_raising_on_an_unpriceable_request(guard):
    """``try_reserve`` promises never to raise on a denial; pricing can raise.

    A quantity large enough to overflow the money type blew up inside the price
    table *before* the halt was ever checked, so the documented "returns a
    Decision" contract broke on exactly the input a halted system is most
    likely to be probed with.
    """
    guard.halt("frozen")
    decision, held = guard.try_reserve("image.render", "1e30", category="images")
    assert decision.reason is DenialReason.HALTED
    assert held is None


def test_an_unpriceable_quantity_is_an_invalid_request_not_a_crash(guard):
    """Same contract when nothing is halted: a denial, with a reason attached."""
    decision, held = guard.try_reserve("image.render", "1e30", category="images")
    assert decision.reason is DenialReason.INVALID_REQUEST
    assert held is None


def test_resuming_restores_normal_service(guard):
    """A halt is reversible, deliberately and explicitly."""
    guard.halt("incident")
    guard.resume("incident closed")
    assert guard.reserve("image.render", 1).estimate == Money.parse("0.04")


def test_a_halt_does_not_erase_committed_history(guard):
    """The breaker stops new spend; it is not a way to reset the month."""
    guard.commit(guard.reserve("image.render", 100).id, Money.parse("4.00"))
    guard.halt("stop")
    assert guard.snapshot().committed == Money.parse("4.00")


# -- the context manager ---------------------------------------------------


def test_charge_commits_the_estimate_on_a_clean_exit(guard):
    """The ergonomic path has to do the right thing when nothing goes wrong."""
    with guard.charge("image.render", 10, category="images"):
        pass
    assert guard.snapshot().committed == Money.parse("0.40")


def test_charge_commits_the_reconciled_amount_when_given_one(guard):
    """The whole reason for a ticket object is to carry the real number back."""
    with guard.charge("image.render", 10, category="images") as ticket:
        ticket.actual(Money.parse("0.31"))
    assert guard.snapshot().committed == Money.parse("0.31")


def test_charge_releases_the_hold_when_the_body_raises(guard):
    """A call that blew up before reaching the provider costs nothing."""
    with pytest.raises(RuntimeError):
        with guard.charge("image.render", 10, category="images"):
            raise RuntimeError("connection refused")
    assert guard.snapshot().used == Money.zero()


def test_charge_commits_when_the_body_raises_after_recording_a_cost(guard):
    """If the provider billed and then the parser crashed, the money still left.

    Releasing here would be the tidy-looking choice and the wrong one: the
    ledger would be short by exactly the amount that was charged.
    """
    with pytest.raises(ValueError):
        with guard.charge("image.render", 10, category="images") as ticket:
            ticket.actual(Money.parse("0.40"))
            raise ValueError("bad response body")
    assert guard.snapshot().committed == Money.parse("0.40")


def test_charge_refuses_to_enter_the_block_when_over_budget(guard):
    """The body must not run at all; that is the point of reserving first."""
    guard.commit(guard.reserve("image.render", 2500).id, Money.parse("100.00"))
    ran = []
    with pytest.raises(BudgetExceededError):
        with guard.charge("image.render", 10):
            ran.append(True)
    assert ran == []


def test_the_release_records_why_the_caller_raised(guard, ledger):
    """Post-incident, the difference between a timeout and a bug matters."""
    with pytest.raises(KeyError):
        with guard.charge("image.render", 10, category="images"):
            raise KeyError("missing field")
    release = [item for item in ledger if item.type.value == "release"][0]
    assert "KeyError" in release.reason


# -- evaluation is free of side effects ------------------------------------


def test_evaluate_writes_nothing(guard, ledger):
    """Planning code calls this in a loop; it must not accumulate history."""
    for _ in range(5):
        guard.evaluate(SpendRequest(key="image.render", units=10, category="images"))
    assert len(ledger) == 0


def test_a_denied_reserve_writes_nothing(guard, ledger):
    """A refusal is not an event; only real holds belong in the ledger."""
    guard.try_reserve("image.render", 100000)
    assert len(ledger) == 0


def test_an_empty_work_key_is_refused(guard):
    """An unnamed charge cannot be attributed, audited or priced."""
    decision, _ = guard.try_reserve("", amount=Money.parse("1.00"))
    assert decision.reason is DenialReason.INVALID_REQUEST


def test_a_negative_quantity_is_refused_as_an_invalid_request(guard):
    """Negative units would produce a negative charge and credit the budget."""
    decision, _ = guard.try_reserve("image.render", -5)
    assert decision.reason is DenialReason.INVALID_REQUEST


# -- snapshots -------------------------------------------------------------


def test_a_category_is_judged_against_its_own_declared_threshold(prices, ledger, clock):
    """A per-category ``warn_threshold`` has to reach a reader, or it is decoration.

    It was parsed and validated at load and then consulted by nothing: no
    snapshot carried it and the CLI warned only on the budget-wide figure, so
    an operator who tuned one noisy category saw no change of any kind.
    """
    budget = BudgetSpec.from_mapping(
        {
            "ceiling": "100.00",
            "warn_threshold": "0.80",
            "categories": {
                "images": {"ceiling": "10.00", "warn_threshold": "0.95"},
                "text": {"ceiling": "10.00"},
            },
        }
    )
    guard = SpendGuard(budget, prices, ledger, clock=clock)
    guard.commit(
        guard.reserve("image.render", 225, category="images").id,
        Money.parse("9.00"),
    )
    images = guard.snapshot().categories["images"]
    assert images.warn_threshold == Decimal("0.95")
    # 90% of its own ceiling: past the budget-wide 80%, under its own 95%.
    assert not images.warn_tripped
    assert guard.snapshot().categories["text"].warn_threshold == Decimal("0.80")


def test_a_category_snapshot_reports_its_threshold_in_plain_data(guard):
    """Monitoring reads the mapping, not the object."""
    payload = guard.snapshot().to_mapping()["categories"]["images"]
    assert Decimal(payload["warn_threshold"]) == Decimal("0.80")
    assert payload["warn_tripped"] is False


def test_the_warning_threshold_trips_before_the_ceiling_does(guard):
    """A soft threshold that only fires at 100% is a report, not a warning."""
    guard.commit(guard.reserve("image.render", 2500).id, Money.parse("81.00"))
    snap = guard.snapshot()
    assert snap.warn_tripped
    assert not snap.exhausted


def test_reservations_count_toward_the_warning_threshold(guard):
    """In-flight work is money about to be spent; ignoring it warns too late."""
    guard.reserve("image.render", 2100)  # 84.00 held, nothing committed
    assert guard.snapshot().warn_tripped


def test_the_snapshot_counts_open_reservations(guard):
    """Operators ask "what is in flight" far more often than "what is the total"."""
    guard.reserve("image.render", 1, category="images")
    guard.reserve("image.render", 1, category="images")
    assert guard.snapshot().open_reservations == 2


def test_the_snapshot_serialises_to_plain_data(guard):
    """The CLI and any monitoring integration need this without importing the types."""
    payload = guard.snapshot().to_mapping()
    assert payload["ceiling"] == "100.00"
    assert payload["categories"]["images"]["ceiling"] == "30.00"


def test_spend_in_one_period_does_not_reduce_the_next(guard, clock):
    """Without rollover declared, a new period starts clean. That is the default."""
    guard.commit(guard.reserve("image.render", 2000).id, Money.parse("80.00"))
    clock.advance(timedelta(days=40))
    assert guard.snapshot().available == Money.parse("100.00")


def test_an_explicit_now_overrides_the_clock_without_mutating_it(guard, clock):
    """Reporting tools ask about past periods; doing so must not move the guard."""
    past = clock.now() - timedelta(days=40)
    assert guard.snapshot(past).window.key == "2026-01"
    assert guard.snapshot().window.key == "2026-03"


def test_a_note_is_recorded_without_changing_any_total(guard, ledger):
    """Operators annotate incidents in place; annotation must never be spend."""
    guard.note("provider raised prices today", ticket="OPS-91")
    assert guard.snapshot().used == Money.zero()
    assert list(ledger)[0].meta["ticket"] == "OPS-91"


def test_a_zero_ceiling_reports_full_utilisation_rather_than_dividing_by_zero(
    prices, ledger, clock
):
    """A ceiling of zero is a valid way to freeze a category, not a crash.

    Utilisation is rendered in reports and compared to the warn threshold, so
    it has to return something meaningful when there is nothing to divide by.
    """
    spec = BudgetSpec.from_mapping({"ceiling": "0.00"})
    frozen = SpendGuard(spec, prices, ledger, clock=clock)
    snap = frozen.snapshot()
    assert snap.utilisation == 0
    assert snap.exhausted
