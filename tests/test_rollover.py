"""Rollover arithmetic.

Rollover is where a budget quietly stops being a ceiling. Carry the unused
balance without a cap and an unattended system accumulates months of headroom;
forget to carry a deficit and an overspent month is forgiven for free.
"""

from __future__ import annotations

from datetime import datetime, timezone

from spend_guard import (
    BudgetSpec,
    FixedClock,
    MemoryLedger,
    Money,
    RolloverPolicy,
    RolloverSpec,
    SpendGuard,
    compute_carry,
    effective_ceiling,
)

CEILING = Money.parse("100.00")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_no_rollover_carries_nothing_in_either_direction():
    """The default must be the conservative one: each period starts clean."""
    spec = RolloverSpec()
    assert compute_carry(CEILING, Money.parse("10"), spec) == Money.zero()
    assert compute_carry(CEILING, Money.parse("150"), spec) == Money.zero()


def test_carry_unused_moves_the_remainder_forward():
    """Under-spending a month is the case operators most often want to keep."""
    carry = compute_carry(CEILING, Money.parse("60"), RolloverSpec(RolloverPolicy.CARRY_UNUSED))
    assert carry == Money.parse("40.00")


def test_carry_unused_ignores_an_overspend():
    """This policy is one-directional; a deficit under it must not reduce next month."""
    carry = compute_carry(CEILING, Money.parse("130"), RolloverSpec(RolloverPolicy.CARRY_UNUSED))
    assert carry == Money.zero()


def test_carry_deficit_moves_only_the_overspend():
    """Repaying an overrun is the fail-closed direction and must work alone."""
    carry = compute_carry(CEILING, Money.parse("130"), RolloverSpec(RolloverPolicy.CARRY_DEFICIT))
    assert carry == Money.parse("-30.00")


def test_carry_deficit_ignores_an_unused_balance():
    """An operator choosing only the strict direction must not get the loose one."""
    carry = compute_carry(CEILING, Money.parse("60"), RolloverSpec(RolloverPolicy.CARRY_DEFICIT))
    assert carry == Money.zero()


def test_carry_both_moves_the_balance_whichever_way_it_points():
    """The symmetric policy is the one that makes a quarter's total meaningful."""
    spec = RolloverSpec(RolloverPolicy.CARRY_BOTH)
    assert compute_carry(CEILING, Money.parse("60"), spec) == Money.parse("40.00")
    assert compute_carry(CEILING, Money.parse("130"), spec) == Money.parse("-30.00")


def test_the_cap_bounds_carry_in_both_directions():
    """An uncapped carry turns a monthly ceiling into an annual one by accident."""
    spec = RolloverSpec(RolloverPolicy.CARRY_BOTH, cap=Money.parse("10"))
    assert compute_carry(CEILING, Money.parse("0"), spec) == Money.parse("10.00")
    assert compute_carry(CEILING, Money.parse("500"), spec) == Money.parse("-10.00")


def test_effective_ceiling_never_goes_negative():
    """A deficit larger than next month's budget blocks everything, at zero.

    A negative ceiling would make every comparison in the guard read strangely
    and invites an off-by-one that authorises spend.
    """
    assert effective_ceiling(CEILING, Money.parse("-500")) == Money.zero()


def test_effective_ceiling_adds_a_positive_carry():
    """The normal case: last month's remainder raises this month's cap."""
    assert effective_ceiling(CEILING, Money.parse("25")) == Money.parse("125.00")


def _guard_with(policy, committed_periods, *, cap=None, lookback=3):
    """Build a guard whose ledger already contains committed spend per period.

    Spend is seeded by reserving nothing and committing the real figure, which
    is also how an overrun looks in production: the reservation was an
    estimate and the invoice was larger.
    """
    budget = BudgetSpec(
        ceiling=CEILING,
        rollover=RolloverSpec(policy, cap=cap, max_lookback=lookback),
    )
    ledger = MemoryLedger()
    clock = FixedClock(utc(2026, 4, 10))
    guard = SpendGuard(budget, None, ledger, clock=clock)
    for when, amount in committed_periods:
        reservation = guard.reserve("work", amount=Money.zero(), now=when)
        guard.commit(reservation.id, Money.parse(amount), now=when)
    return guard


def test_carry_flows_from_the_previous_period_into_this_one():
    """The end-to-end path: last month's remainder shows up in this month's ceiling."""
    guard = _guard_with(RolloverPolicy.CARRY_UNUSED, [(utc(2026, 3, 5), "70.00")])
    snap = guard.snapshot(utc(2026, 4, 10))
    assert snap.carry_in == Money.parse("30.00")
    assert snap.ceiling == Money.parse("130.00")


def test_carry_compounds_across_several_periods():
    """Two quiet months in a row carry more than one, up to the lookback bound."""
    guard = _guard_with(
        RolloverPolicy.CARRY_UNUSED,
        [(utc(2026, 2, 5), "90.00"), (utc(2026, 3, 5), "80.00")],
    )
    snap = guard.snapshot(utc(2026, 4, 10))
    # February leaves 10, so March's ceiling is 110 and it spent 80 -> 30 carried.
    assert snap.carry_in == Money.parse("30.00")


def test_an_idle_period_does_not_manufacture_budget():
    """A system switched off for months must not wake with months of headroom.

    This is the difference between a budget and an allowance that accrues. A
    period with no ledger activity at all contributes nothing and breaks the
    chain, so an outage cannot fund a spending spree.
    """
    guard = _guard_with(RolloverPolicy.CARRY_UNUSED, [(utc(2026, 1, 5), "0.00")])
    snap = guard.snapshot(utc(2026, 4, 10))
    assert snap.carry_in == Money.zero()
    assert snap.ceiling == CEILING


def test_an_overspent_previous_period_reduces_this_ceiling():
    """The whole point of carrying a deficit: last month's overrun costs this month."""
    guard = _guard_with(
        RolloverPolicy.CARRY_DEFICIT, [(utc(2026, 3, 5), "140.00")]
    )
    snap = guard.snapshot(utc(2026, 4, 10))
    assert snap.carry_in == Money.parse("-40.00")
    assert snap.ceiling == Money.parse("60.00")


def test_a_deficit_survives_an_idle_period_instead_of_being_forgiven():
    """Silence is not repayment, and the silence here is caused by the debt itself.

    March overspends 300 against a 100 ceiling. April's ceiling is correctly
    zero, so every April call is refused — and a refusal writes nothing, which
    made April look idle, which broke the carry chain and wiped the whole
    200.00 of unrepaid overspend. The forgiving direction breaks on an idle
    period; the strict one must not.
    """
    guard = _guard_with(RolloverPolicy.CARRY_DEFICIT, [(utc(2026, 3, 5), "300.00")])
    april = guard.snapshot(utc(2026, 4, 10))
    assert april.carry_in == Money.parse("-200.00")
    assert april.ceiling == Money.zero()

    # April recorded nothing, so it repaid nothing: the debt arrives in May
    # undiminished rather than reset to zero.
    may = guard.snapshot(utc(2026, 5, 10))
    assert may.carry_in == Money.parse("-200.00")
    assert may.ceiling == Money.zero()
    assert guard.try_reserve("work", amount=Money.parse("1.00"), now=utc(2026, 5, 10))[1] is None


def test_an_idle_period_still_refuses_to_manufacture_budget_under_carry_both():
    """The symmetric policy must break on silence in the generous direction only."""
    guard = _guard_with(RolloverPolicy.CARRY_BOTH, [(utc(2026, 1, 5), "0.00")])
    assert guard.snapshot(utc(2026, 4, 10)).carry_in == Money.zero()


def test_a_debt_larger_than_one_period_is_repaid_across_periods_not_erased():
    """A ceiling floored at zero is what a period may spend, not what it repaid.

    Computing the next carry from the floored ceiling made an overspend of more
    than one period's budget vanish after a single quiet-but-active period.
    """
    guard = _guard_with(
        RolloverPolicy.CARRY_DEFICIT,
        [(utc(2026, 2, 5), "250.00"), (utc(2026, 3, 5), "0.00")],
    )
    # February overran by 150. March could absorb 100 of it, so 50 is still owed.
    snap = guard.snapshot(utc(2026, 4, 10))
    assert snap.carry_in == Money.parse("-50.00")
    assert snap.ceiling == Money.parse("50.00")


def test_zero_lookback_disables_the_chain_without_changing_the_policy():
    """An operator can keep the policy declared while switching the effect off."""
    guard = _guard_with(
        RolloverPolicy.CARRY_UNUSED, [(utc(2026, 3, 5), "10.00")], lookback=0
    )
    assert guard.snapshot(utc(2026, 4, 10)).carry_in == Money.zero()


def test_rollover_spec_round_trips_through_a_mapping():
    """Rollover settings survive being written to a file and read back."""
    spec = RolloverSpec(RolloverPolicy.CARRY_BOTH, cap=Money.parse("50"), max_lookback=6)
    assert RolloverSpec.from_mapping(spec.to_mapping()) == spec
