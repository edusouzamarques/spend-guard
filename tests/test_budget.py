"""Budget declarations and their validation.

A budget is data an operator writes by hand. Every mistake that can be caught
at load time must be caught at load time, because the alternative is finding
out during the incident the budget existed to prevent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spend_guard import BudgetSpec, CategoryBudget, Money, Period, RolloverPolicy
from spend_guard.errors import BudgetConfigError


def test_a_budget_requires_a_ceiling():
    """Without a hard number this is a reporting tool, not a guard."""
    with pytest.raises(BudgetConfigError) as excinfo:
        BudgetSpec.from_mapping({"categories": {"text": {"ceiling": "10"}}})
    assert "ceiling" in str(excinfo.value)


def test_category_ceilings_may_not_exceed_the_total():
    """Sub-budgets that cannot all be spent are a declaration bug.

    Left unchecked, an operator reads the file and believes each category is
    available when in fact the first two exhaust the total.
    """
    with pytest.raises(BudgetConfigError) as excinfo:
        BudgetSpec.from_mapping(
            {
                "ceiling": "100.00",
                "categories": {"a": {"ceiling": "80"}, "b": {"ceiling": "40"}},
            }
        )
    assert "exceeds the total ceiling" in str(excinfo.value)


def test_category_ceilings_may_sum_to_less_than_the_total():
    """Deliberately leaving an unallocated reserve is normal practice."""
    spec = BudgetSpec.from_mapping(
        {
            "ceiling": "100.00",
            "categories": {"a": {"ceiling": "30"}, "b": {"ceiling": "20"}},
        }
    )
    assert spec.unallocated == Money.parse("50.00")


def test_negative_ceilings_are_rejected():
    """A negative cap would make every comparison behave backwards."""
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "-1"})
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "10", "categories": {"a": {"ceiling": "-1"}}})


def test_warn_threshold_must_be_a_fraction():
    """"80" meaning 80% is the obvious mistake; it must not silently disable the warning."""
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "10", "warn_threshold": 80})
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "10", "warn_threshold": 0})


def test_unknown_top_level_keys_are_rejected():
    """A misspelled key would otherwise be ignored and the setting never applied."""
    with pytest.raises(BudgetConfigError) as excinfo:
        BudgetSpec.from_mapping({"ceiling": "10", "catagories": {}})
    assert "catagories" in str(excinfo.value)


def test_refusing_uncategorised_spend_with_no_categories_is_rejected():
    """That combination authorises nothing at all; it is never what was meant."""
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "10", "allow_uncategorised": False})


def test_shorthand_category_value_is_read_as_a_ceiling():
    """Declarations in the wild write ``{"text": "40.00"}``; accept it."""
    spec = BudgetSpec.from_mapping({"ceiling": "100", "categories": {"text": "40.00"}})
    assert spec.categories["text"].ceiling == Money.parse("40.00")


def test_category_inherits_the_budget_warn_threshold_unless_it_sets_one():
    """One noisy category should be tunable without changing the global default."""
    spec = BudgetSpec.from_mapping(
        {
            "ceiling": "100",
            "warn_threshold": "0.8",
            "categories": {"a": {"ceiling": "10"}, "b": {"ceiling": "10", "warn_threshold": "0.95"}},
        }
    )
    assert spec.warn_threshold_for("a") == Decimal("0.8")
    assert spec.warn_threshold_for("b") == Decimal("0.95")


def test_currency_is_normalised_and_required():
    """A blank currency label makes every report ambiguous."""
    assert BudgetSpec.from_mapping({"ceiling": "1", "currency": "eur"}).currency == "EUR"
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "1", "currency": "  "})


def test_ceiling_text_rounds_down_when_over_precise():
    """The enforced cap must never exceed what the operator wrote."""
    spec = BudgetSpec.from_mapping({"ceiling": "10.9999999"})
    assert spec.ceiling.micros == 10_999_999


def test_knows_category_reflects_the_uncategorised_policy():
    """The guard asks this before pricing; it has to answer for the empty name."""
    permissive = BudgetSpec.from_mapping({"ceiling": "10"})
    strict = BudgetSpec.from_mapping(
        {"ceiling": "10", "allow_uncategorised": False, "categories": {"a": "5"}}
    )
    assert permissive.knows_category("")
    assert not strict.knows_category("")
    assert strict.knows_category("a")


def test_budget_round_trips_through_a_mapping():
    """A budget written back out must reload identically, or config drifts."""
    original = BudgetSpec.from_mapping(
        {
            "ceiling": "1200.00",
            "period": {"kind": "monthly", "anchor_day": 5},
            "rollover": {"policy": "carry_unused", "cap": "200.00"},
            "categories": {"text": {"ceiling": "100.00"}},
        }
    )
    assert BudgetSpec.from_mapping(original.to_mapping()) == original


def test_a_round_trip_keeps_a_category_warn_threshold():
    """Serialisation dropped it, so writing a budget back out lost the setting.

    The example budget ships a tuned threshold on one category; any tool that
    read that file and wrote it again silently reverted it to the default.
    """
    original = BudgetSpec.from_mapping(
        {
            "ceiling": "100.00",
            "categories": {"a": {"ceiling": "10.00", "warn_threshold": "0.95"}},
        }
    )
    written = original.to_mapping()
    assert written["categories"]["a"]["warn_threshold"] == "0.95"
    reloaded = BudgetSpec.from_mapping(written)
    assert reloaded.warn_threshold_for("a") == Decimal("0.95")
    assert reloaded == original


def test_category_budget_objects_are_accepted_directly():
    """Building a spec in code should not require going through a mapping."""
    spec = BudgetSpec(
        ceiling=Money.parse("50"),
        categories={"text": CategoryBudget(name="text", ceiling=Money.parse("20"))},
        period=Period.monthly(),
    )
    assert spec.categories["text"].ceiling == Money.parse("20")


def test_an_empty_category_name_is_rejected():
    """The empty name is reserved for uncategorised spend and cannot be declared."""
    with pytest.raises(BudgetConfigError):
        BudgetSpec(ceiling=Money.parse("10"), categories={"": Money.parse("1")})


def test_unknown_rollover_policy_lists_the_valid_ones():
    """Rollover is the subtlest setting here; a typo must not fall back to none."""
    with pytest.raises(BudgetConfigError) as excinfo:
        BudgetSpec.from_mapping({"ceiling": "10", "rollover": {"policy": "roll"}})
    assert "carry_unused" in str(excinfo.value)


def test_rollover_policy_can_be_given_as_a_bare_string():
    """``"rollover": "carry_unused"`` is the shape people write first."""
    spec = BudgetSpec.from_mapping({"ceiling": "10", "rollover": "carry_unused"})
    assert spec.rollover.policy is RolloverPolicy.CARRY_UNUSED


def test_negative_rollover_cap_is_rejected():
    """The cap bounds magnitude in both directions; a negative one is meaningless."""
    with pytest.raises(BudgetConfigError):
        BudgetSpec.from_mapping({"ceiling": "10", "rollover": {"policy": "carry_both", "cap": "-5"}})
