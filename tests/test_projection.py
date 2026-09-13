"""Burn projection.

Projection is the part most likely to be believed without being checked, so
its refusal to guess matters more than its arithmetic. A confident zero on the
first day of a runaway job is worse than no projection at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from spend_guard import BudgetSpec, FixedClock, MemoryLedger, Money, SpendGuard, project


def march(day, hour=0):
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def spender(prices, ids):
    """A 100.00 monthly budget on a clock that starts at midnight on the 1st."""
    budget = BudgetSpec.from_mapping({"ceiling": "100.00"})
    clock = FixedClock(march(1))
    return SpendGuard(budget, prices, MemoryLedger(), clock=clock, id_factory=ids)


def spend(guard, amount, *, now=None):
    reservation = guard.reserve("image.render", amount=Money.zero(), now=now)
    guard.commit(reservation.id, Money.parse(amount), now=now)


def test_no_elapsed_time_yields_no_projection_rather_than_zero(spender):
    """At the first instant of a period there is no rate, and saying so is the point.

    Reporting 0.00/day here would read as "all clear" on the exact day a
    runaway job starts.
    """
    forecast = project(spender.snapshot(march(1)))
    assert forecast.burn_per_day is None
    assert forecast.projected_total is None


def test_on_track_is_none_when_there_is_no_rate(spender):
    """Callers that treat None as falsey get an alert, not a false reassurance."""
    assert project(spender.snapshot(march(1))).on_track is None


def test_no_spend_yields_no_projection(spender):
    """Half a month with nothing spent still has no rate to extend."""
    forecast = project(spender.snapshot(march(15)))
    assert forecast.burn_per_day is None
    assert forecast.on_track is None


def test_a_steady_rate_projects_to_the_end_of_the_period(spender):
    """The base case: 10.00 over 10 days of a 31-day month projects to 31.00."""
    spend(spender, "10.00", now=march(5))
    forecast = project(spender.snapshot(march(11)))
    assert forecast.burn_per_day == Money.parse("1.00")
    assert forecast.projected_total == Money.parse("31.00")


def test_a_projection_under_the_ceiling_is_on_track(spender):
    """The healthy case has to be reported as healthy or the alert is meaningless."""
    spend(spender, "10.00", now=march(5))
    assert project(spender.snapshot(march(11))).on_track is True


def test_a_projection_over_the_ceiling_is_not_on_track(spender):
    """Spending 50.00 in the first five days of a 100.00 month is not fine."""
    spend(spender, "50.00", now=march(2))
    forecast = project(spender.snapshot(march(6)))
    assert forecast.on_track is False
    assert forecast.projected_overage.is_positive


def test_the_overage_is_zero_rather_than_negative_when_on_track(spender):
    """"Projected overage" should read as an amount over, never as slack."""
    spend(spender, "1.00", now=march(2))
    assert project(spender.snapshot(march(11))).projected_overage == Money.zero()


def test_the_exhaustion_moment_is_reported_when_it_falls_inside_the_period(spender):
    """A date is what an operator acts on; a percentage is not."""
    spend(spender, "50.00", now=march(2))
    forecast = project(spender.snapshot(march(6)))
    assert forecast.exhausts_before_period_end
    assert forecast.exhaustion_at < forecast.window.end


def test_a_slow_burn_does_not_claim_exhaustion_inside_the_period(spender):
    """The flag has to be selective, or every period looks like an emergency."""
    spend(spender, "1.00", now=march(2))
    assert not project(spender.snapshot(march(11))).exhausts_before_period_end


def test_including_reservations_gives_the_pessimistic_reading(spender):
    """Deciding whether to start more work means counting work already in flight."""
    spend(spender, "10.00", now=march(5))
    spender.reserve("image.render", amount=Money.parse("20.00"), now=march(11))
    optimistic = project(spender.snapshot(march(11)))
    pessimistic = project(spender.snapshot(march(11)), include_reserved=True)
    assert pessimistic.projected_total > optimistic.projected_total


def test_elapsed_fraction_matches_the_position_in_the_period(spender):
    """Everything downstream divides by this, so it has to mean what it says."""
    forecast = project(spender.snapshot(march(16, 12)))
    assert forecast.elapsed_fraction == Decimal("0.5")


def test_remaining_is_the_ceiling_minus_committed(spender):
    """The simplest number in the report is also the one operators quote."""
    spend(spender, "30.00", now=march(5))
    assert project(spender.snapshot(march(6))).remaining == Money.parse("70.00")


def test_a_projection_after_the_period_ended_does_not_extrapolate_past_full(spender):
    """A stale clock must not divide by a fraction greater than one.

    Without clamping, a projection run after the period closed reports a burn
    rate lower than the real one, which is the direction that hides a problem.
    """
    spend(spender, "90.00", now=march(5))
    forecast = project(spender.snapshot(datetime(2026, 4, 15, tzinfo=timezone.utc)))
    assert forecast.elapsed_fraction <= Decimal(1)


def test_the_projection_serialises_to_plain_data(spender):
    """Monitoring integrations read this; None must survive as null, not as 0."""
    payload = project(spender.snapshot(march(1))).to_mapping()
    assert payload["burn_per_day"] is None
    assert payload["on_track"] is None
    assert payload["ceiling"] == "100.00"
