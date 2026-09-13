"""Money arithmetic.

Money is the substrate of every other decision here. If a cent goes missing in
a rounding rule, the ceiling stops being a ceiling and nothing above this layer
can fix it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spend_guard import MICROS_PER_UNIT, Money, Rounding
from spend_guard.errors import MoneyError


def test_parse_int_is_exact_whole_units():
    """A plain integer is a whole currency unit, not a count of micro-units.

    Getting this backwards would make every declared ceiling a millionth of
    what the operator wrote.
    """
    assert Money.parse(5).micros == 5 * MICROS_PER_UNIT


def test_parse_decimal_string_keeps_six_places():
    """Per-token pricing lives in the sixth decimal place; cents would lose it."""
    assert Money.parse("0.000003").micros == 3


def test_parse_rejects_float_rather_than_absorbing_its_error():
    """A float ceiling silently drifts.

    0.1 + 0.2 != 0.3 in binary floating point. Accepting a float here would
    bake that error into every ledger entry, so the type is refused outright
    with a message telling the caller to pass a string.
    """
    with pytest.raises(MoneyError) as excinfo:
        Money.parse(1.1)
    assert "float" in str(excinfo.value)


def test_parse_rejects_bool():
    """True is an int in Python; it is not an amount of money."""
    with pytest.raises(MoneyError):
        Money.parse(True)


def test_parse_rejects_unparseable_text():
    """A typo in a declaration must fail loudly at load, not become zero."""
    with pytest.raises(MoneyError):
        Money.parse("twelve dollars")


def test_parse_rejects_empty_string():
    """An empty value in a config file is a missing value, not a free call."""
    with pytest.raises(MoneyError):
        Money.parse("")


def test_parse_rejects_non_finite():
    """Infinity is not a budget; it would make every comparison pass."""
    with pytest.raises(MoneyError):
        Money.parse(Decimal("Infinity"))


def test_parse_accepts_thousands_separators_and_leading_plus():
    """Hand-written declarations really do contain "1,200" and "+50"."""
    assert Money.parse("1,200") == Money.parse(1200)
    assert Money.parse("1,234,567.89") == Money.parse("1234567.89")
    assert Money.parse("+50.5") == Money.parse("50.50")


def test_parse_rejects_a_comma_that_is_not_a_thousands_separator():
    """A European "1,50" must not silently become 150.00.

    Stripping every comma made a declared ceiling of "1,50" into a cap a
    hundred times larger than the operator wrote, with no error anywhere — the
    exact opposite of the rule that a ceiling never rounds *up*. The ambiguous
    cases are rejected; only real grouping is stripped.
    """
    for text in ("1,50", "0,05", "1.200,50", "1,2345", "12,34,567"):
        with pytest.raises(MoneyError) as excinfo:
            Money.parse(text)
        assert "thousands separator" in str(excinfo.value)


def test_a_ceiling_declared_with_a_decimal_comma_is_refused_not_inflated():
    """End to end: the budget loader must not build a 100x cap from a typo."""
    from spend_guard.budget import BudgetSpec

    with pytest.raises(MoneyError):
        BudgetSpec.from_mapping({"ceiling": "1,50", "currency": "EUR"})


def test_sub_micro_cost_rounds_up_not_down():
    """A cost too small to represent rounds up, so the ledger never under-charges.

    Rounding down here is invisible per call and material across a million
    calls: the money leaves the account either way, but the ledger would not
    show it.
    """
    assert Money.parse("0.0000004", rounding=Rounding.UP).micros == 1


def test_sub_micro_ceiling_rounds_down_not_up():
    """A ceiling rounds down, so the enforced cap is never above the declared one.

    Costs and ceilings round in opposite directions on purpose: both choices
    protect the budget.
    """
    assert Money.parse("10.9999994", rounding=Rounding.DOWN).micros == 10_999_999


def test_negative_amounts_are_representable():
    """A carried deficit is a negative balance; refusing it would break rollover."""
    assert Money.parse("-5").micros == -5_000_000
    assert Money.parse("-5").is_negative


def test_addition_and_subtraction_are_exact_over_many_terms():
    """Ten additions of 0.1 must equal exactly 1.00, which floats cannot promise."""
    total = Money.zero()
    for _ in range(10):
        total = total + Money.parse("0.1")
    assert total == Money.parse("1.00")


def test_scale_rejects_float_factors():
    """A float multiplier reintroduces exactly the error the type avoids."""
    with pytest.raises(MoneyError):
        Money.parse("1.00").scale(0.5)  # type: ignore[arg-type]


def test_scale_rounds_up_by_default():
    """Scaling is used to price work, so its default direction must be the safe one."""
    assert Money.parse("1.00").scale(Decimal("0.0000001")).micros == 1


def test_scale_down_floors():
    """The caller can ask for the other direction explicitly when it is a cap."""
    result = Money.parse("1.00").scale(Decimal("0.00000019"), rounding=Rounding.DOWN)
    assert result.micros == 0


def test_comparison_orders_by_value():
    """Ceiling checks are comparisons; they must order by amount, not by identity."""
    assert Money.parse("1.00") < Money.parse("2.00")
    assert max(Money.parse("1.00"), Money.parse("3.50")) == Money.parse("3.50")


def test_clamp_min_and_max_bound_a_value():
    """Rollover caps are expressed as clamps; they must not move a value in range."""
    five = Money.parse("5")
    assert five.clamp_min(Money.parse("1")) == five
    assert five.clamp_max(Money.parse("3")) == Money.parse("3")


def test_zero_is_falsey_but_still_a_real_amount():
    """A declared zero price is a fact; code must be able to tell it from None."""
    assert not Money.zero()
    assert Money.zero() is not None
    assert Money.zero().is_zero


def test_format_pads_to_two_places_but_keeps_precision_when_needed():
    """Money is read by humans in reports and by tests in assertions."""
    assert Money.parse("1200").format() == "1200.00"
    assert Money.parse("0.000003").format() == "0.000003"
    assert Money.parse("1.5").format() == "1.50"


def test_format_with_explicit_places():
    """Reports sometimes need a fixed column width regardless of precision."""
    assert Money.parse("1.239").format(places=2) == "1.24"


def test_multiplication_by_int_is_exact():
    """Charging N identical calls must not drift from N times one call."""
    assert Money.parse("0.04") * 25 == Money.parse("1.00")


def test_micros_constructor_rejects_non_int():
    """Money(micros=...) is the raw constructor; a decimal there means a bug."""
    with pytest.raises(MoneyError):
        Money(1.5)  # type: ignore[arg-type]


def test_from_decimal_is_public_and_honours_the_requested_direction():
    """Callers doing their own exact arithmetic need a supported way back in.

    Without this, code outside the package reaches for a private helper or,
    worse, converts through float on the way.
    """
    assert Money.from_decimal(Decimal("0.0000004")).micros == 1
    assert Money.from_decimal(Decimal("0.0000004"), Rounding.DOWN).micros == 0


def test_format_keeps_the_sign_of_a_negative_amount():
    """A carried deficit is shown to operators; dropping the sign inverts it."""
    assert Money.parse("-30").format() == "-30.00"
