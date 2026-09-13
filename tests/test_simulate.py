"""Simulation.

"Will this batch fit in what's left of the month" has to be answerable without
finding out the expensive way. Simulation therefore has one absolute
requirement: it writes nothing.
"""

from __future__ import annotations

import pytest

from spend_guard import DenialReason, Money, SpendRequest, simulate


def test_a_batch_that_fits_is_reported_as_allowed(guard):
    """The base case, and the one an operator will act on."""
    result = simulate(guard, [SpendRequest("image.render", 10)] * 5)
    assert result.all_allowed
    assert result.total == Money.parse("2.00")


def test_simulation_writes_nothing_to_the_ledger(guard, ledger):
    """A dry run that leaves a reservation behind is not a dry run."""
    simulate(guard, [SpendRequest("image.render", 1000)] * 10)
    assert len(ledger) == 0


def test_simulation_does_not_move_the_reported_budget(guard):
    """The snapshot before and after must be identical, including reservations."""
    before = guard.snapshot()
    simulate(guard, [SpendRequest("image.render", 1000)] * 10)
    after = guard.snapshot()
    assert (before.available, before.reserved) == (after.available, after.reserved)


def test_each_step_sees_the_budget_the_previous_ones_would_have_used(guard):
    """Without an overlay every step would pass, which is the useless answer.

    Three 40.00 calls against a 100.00 ceiling: the first two fit and the third
    cannot, and only a running total can tell you that.
    """
    result = simulate(guard, [SpendRequest("image.render", 1000)] * 3)
    assert result.allowed_count == 2
    assert result.first_denied.index == 2


def test_the_first_denial_explains_itself(guard):
    """The reason drives the fix: raise the ceiling, or wait for the next period."""
    result = simulate(guard, [SpendRequest("image.render", 1000)] * 3)
    assert result.first_denied.decision.reason is DenialReason.TOTAL_EXCEEDED


def test_a_denied_step_consumes_nothing_so_a_cheaper_one_can_still_fit(guard):
    """Planning a mixed batch needs the truth about every item, not just the first.

    A refused call spends nothing, so a small call after a large refusal really
    would go through.
    """
    result = simulate(
        guard,
        [
            SpendRequest("image.render", 1000),  # 40.00
            SpendRequest("image.render", 1000),  # 40.00
            SpendRequest("image.render", 1000),  # 40.00 - refused
            SpendRequest("image.render", 10),  # 0.40 - still fits
        ],
    )
    assert [step.allowed for step in result.steps] == [True, True, False, True]


def test_stop_on_denial_halts_the_walk(guard):
    """For a sequential pipeline, everything after the first failure is moot."""
    result = simulate(
        guard, [SpendRequest("image.render", 1000)] * 5, stop_on_denial=True
    )
    assert len(result.steps) == 3


def test_simulation_respects_category_ceilings(guard):
    """A batch can fit the global budget and still break one category."""
    result = simulate(guard, [SpendRequest("image.render", 100, "images")] * 10)
    assert result.first_denied.decision.reason is DenialReason.CATEGORY_EXCEEDED


def test_an_unpriced_step_is_denied_in_a_simulation_too(guard):
    """Planning must surface a missing price before the pipeline hits it."""
    result = simulate(guard, [SpendRequest("video.transcode", 1)])
    assert result.first_denied.decision.reason is DenialReason.UNKNOWN_PRICE


def test_simulation_accounts_for_spend_already_committed(guard):
    """The question is about the remaining budget, not a fresh one."""
    guard.commit(guard.reserve("image.render", 2000).id, Money.parse("80.00"))
    result = simulate(guard, [SpendRequest("image.render", 1000)])
    assert not result.all_allowed


def test_simulation_accounts_for_budget_already_reserved(guard):
    """In-flight work has to count, or two planners both think there is room."""
    guard.reserve("image.render", 2000)  # 80.00 held
    result = simulate(guard, [SpendRequest("image.render", 1000)])
    assert not result.all_allowed


def test_requests_can_be_given_as_plain_strings_and_tuples(guard):
    """The planning caller is often a script; it should not need to import types."""
    result = simulate(guard, ["image.render", ("image.render", 10, "images")])
    assert result.all_allowed
    assert result.total == Money.parse("0.44")


def test_requests_can_be_given_as_mappings(guard):
    """A batch plan read from JSON is a list of dicts, not of dataclasses."""
    result = simulate(guard, [{"key": "image.render", "units": 10, "category": "images"}])
    assert result.total == Money.parse("0.40")


def test_an_unreadable_request_raises_rather_than_being_skipped(guard):
    """Silently dropping a step would report a batch as affordable when it is not."""
    with pytest.raises(TypeError):
        simulate(guard, [object()])


def test_the_result_serialises_to_plain_data(guard):
    """The CLI prints this, and a planner may post it somewhere."""
    payload = simulate(guard, [SpendRequest("image.render", 1000)] * 3).to_mapping()
    assert payload["allowed_count"] == 2
    assert payload["denied_count"] == 1
    assert payload["steps"][2]["allowed"] is False


def test_an_empty_batch_is_trivially_allowed(guard):
    """Edge case that a planner will hit on its first run with nothing queued."""
    result = simulate(guard, [])
    assert result.all_allowed
    assert result.total == Money.zero()
