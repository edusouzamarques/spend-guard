"""Headline behaviour 1: an unknown price blocks. It is never treated as free.

This is the failure that makes a budget ceiling stop meaning anything. The
calls nobody priced are the new ones, the experimental ones, the ones a
teammate added last week - in other words, exactly the expensive ones. A guard
that lets those through as zero reports a healthy budget right up to the
invoice.

Every test here would still pass if the guard merely *warned* about an unknown
price, so each one asserts the refusal itself: no reservation is created, no
ledger entry is written, and the total does not move.
"""

from __future__ import annotations

import pytest

from spend_guard import (
    BudgetSpec,
    DenialReason,
    Money,
    OnMissing,
    PriceTable,
    SpendGuard,
    SpendRequest,
)
from spend_guard.errors import UnknownPriceError


def test_reserving_an_unpriced_unit_of_work_raises(guard):
    """The fail-closed entry point refuses rather than charging zero."""
    with pytest.raises(UnknownPriceError):
        guard.reserve("video.transcode", 10)


def test_the_denial_names_the_reason_so_callers_can_branch(guard):
    """"Unknown price" and "over budget" need different operator responses."""
    decision, reservation = guard.try_reserve("video.transcode", 10)
    assert reservation is None
    assert decision.reason is DenialReason.UNKNOWN_PRICE


def test_a_refused_unknown_price_writes_nothing_to_the_ledger(guard, ledger):
    """A refusal is not an event to reconcile; the history must stay clean."""
    guard.try_reserve("video.transcode", 10)
    assert len(ledger) == 0


def test_an_unknown_price_does_not_move_the_committed_total(guard):
    """The number a dashboard shows must not drift because of a refused call."""
    before = guard.snapshot().committed
    guard.try_reserve("video.transcode", 10)
    assert guard.snapshot().committed == before


def test_an_unknown_price_does_not_consume_headroom(guard):
    """Zero would be invisible; anything else would be a guess. Neither is charged."""
    before = guard.snapshot().available
    guard.try_reserve("video.transcode", 10)
    assert guard.snapshot().available == before


def test_the_decision_carries_no_amount_at_all(guard):
    """``None`` rather than zero, so no caller can sum it into a total by mistake."""
    decision, _ = guard.try_reserve("video.transcode", 10)
    assert decision.amount is None
    assert not decision.quote.known


def test_an_unknown_price_is_refused_even_with_an_empty_budget_full_of_room(
    budget, ledger, clock
):
    """Plenty of headroom is not a reason to allow an unmeasurable call.

    The refusal is about not knowing the cost, not about the balance. Tying it
    to the balance would let every unpriced call through on the 1st of the
    month and none of them on the 28th.
    """
    generous = BudgetSpec.from_mapping({"ceiling": "1000000.00"})
    guard = SpendGuard(generous, PriceTable(), ledger, clock=clock)
    with pytest.raises(UnknownPriceError):
        guard.reserve("anything")


def test_an_unknown_price_is_checked_before_the_ceiling(budget, ledger, clock):
    """Ordering matters: an exhausted budget must not mask an unpriced call.

    If the ceiling were checked first, the operator would be told to raise the
    budget when the real problem is a missing price rule.
    """
    tiny = BudgetSpec.from_mapping({"ceiling": "0.00"})
    guard = SpendGuard(tiny, PriceTable(), ledger, clock=clock)
    decision, _ = guard.try_reserve("anything")
    assert decision.reason is DenialReason.UNKNOWN_PRICE


def test_a_declared_zero_price_is_allowed_because_it_is_a_decision(guard):
    """Zero and unknown are different facts and must behave differently.

    Somebody wrote ``"free.healthcheck": 0`` on purpose. That is information.
    A missing key is the absence of information, and only the absence blocks.
    """
    reservation = guard.reserve("free.healthcheck")
    assert reservation.estimate == Money.zero()


def test_a_declared_zero_price_is_still_allowed_when_the_budget_is_exhausted(
    ledger, clock
):
    """A call that costs nothing cannot break a ceiling, so it is not refused.

    The contrast with an unknown price is the point: unknown stays refused
    here, because unknown is not zero.
    """
    budget = BudgetSpec.from_mapping({"ceiling": "0.00"})
    prices = PriceTable.from_mapping({"rules": {"free.ping": "0"}})
    guard = SpendGuard(budget, prices, ledger, clock=clock)
    assert guard.reserve("free.ping").estimate == Money.zero()
    with pytest.raises(UnknownPriceError):
        guard.reserve("not.declared")


def test_an_explicit_amount_bypasses_the_table_for_a_known_cost(guard):
    """A caller that already has the invoice line should not be blocked by a table."""
    reservation = guard.reserve("video.transcode", amount=Money.parse("2.00"))
    assert reservation.estimate == Money.parse("2.00")


def test_opting_in_to_estimates_allows_unpriced_work_but_flags_it(
    budget, ledger, clock
):
    """The escape hatch exists, costs a declared number, and leaves a trail.

    ``estimated`` on the ledger entry is what lets an audit separate measured
    spend from guessed spend afterwards.
    """
    prices = PriceTable(
        rules={}, on_missing=OnMissing.ESTIMATE, fallback_price=Money.parse("0.25")
    )
    guard = SpendGuard(budget, prices, ledger, clock=clock)
    reservation = guard.reserve("brand.new.thing")
    assert reservation.estimate == Money.parse("0.25")
    assert reservation.estimated_price
    assert list(ledger)[0].estimated is True


def test_there_is_no_setting_that_makes_unknown_work_free():
    """The only way past a missing price is to name a number. There is no zero mode."""
    assert {option.value for option in OnMissing} == {"block", "estimate"}


def test_a_wildcard_rule_is_enough_to_stop_the_block(budget, ledger, clock):
    """The intended fix is a family rule, not disabling the check."""
    prices = PriceTable.from_mapping({"rules": {"video.*": {"unit_price": "0.10"}}})
    guard = SpendGuard(budget, prices, ledger, clock=clock)
    assert guard.reserve("video.transcode", 3).estimate == Money.parse("0.30")


def test_evaluate_reports_the_unknown_price_without_touching_state(guard, ledger):
    """Planning tools ask this question constantly; asking must be free of effects."""
    decision = guard.evaluate(SpendRequest(key="video.transcode", units=1))
    assert not decision.allowed
    assert decision.reason is DenialReason.UNKNOWN_PRICE
    assert len(ledger) == 0


def test_the_message_tells_the_operator_the_three_ways_out(guard):
    """A blocked pipeline at 2am needs the fix in the error, not in the manual."""
    decision, _ = guard.try_reserve("video.transcode")
    message = decision.message
    assert "rule" in message and "explicit amount" in message and "estimate" in message
