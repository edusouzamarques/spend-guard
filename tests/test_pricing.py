"""The price table.

A price table turns "8000 tokens" into "0.024". The interesting cases are the
edges: a rate quoted per million units, a minimum charge, a wildcard for a
model family nobody has added yet, and above all a key that is simply not there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spend_guard import Money, OnMissing, PriceRule, PriceTable
from spend_guard.errors import PriceConfigError, UnknownPriceError


def table(**kwargs):
    return PriceTable.from_mapping(
        {
            "rules": {
                "text.generate": {"unit": "token", "per": 1000000, "unit_price": "3.00"},
                "image.render": {"unit": "image", "unit_price": "0.04"},
                "speech.say": {
                    "unit": "character",
                    "per": 1000,
                    "unit_price": "0.30",
                    "minimum": "0.01",
                },
            },
            **kwargs,
        }
    )


def test_per_unit_price_multiplies_by_quantity():
    """The simplest case, and the one every other behaviour is measured against."""
    assert table().quote("image.render", 25).require() == Money.parse("1.00")


def test_rate_quoted_per_million_units_is_exact():
    """"$3 per million tokens" must not be pre-divided into a rounded per-token rate.

    Dividing first would round 0.000003 into the table and multiply the error
    by every call; dividing last keeps the arithmetic exact.
    """
    assert table().quote("text.generate", 8000).require() == Money.parse("0.024")


def test_a_single_token_costs_a_representable_amount_rounded_up():
    """One token at $3/M is 3 micro-units; anything smaller still charges one."""
    assert table().quote("text.generate", 1).require() == Money.parse("0.000003")


def test_minimum_charge_applies_below_the_threshold():
    """Providers bill a floor per call; a table that ignores it under-reports."""
    assert table().quote("speech.say", 5).require() == Money.parse("0.01")


def test_minimum_charge_does_not_apply_above_the_threshold():
    """The floor is a minimum, not a surcharge."""
    assert table().quote("speech.say", 2000).require() == Money.parse("0.60")


def test_zero_units_still_pays_the_minimum():
    """A call that sends nothing is still a call, and the provider still bills it."""
    assert table().quote("speech.say", 0).require() == Money.parse("0.01")


def test_zero_units_without_a_minimum_costs_nothing():
    """Without a declared floor there is nothing to charge, and that is a known zero."""
    quote = table().quote("image.render", 0)
    assert quote.known
    assert quote.require() == Money.zero()


def test_an_unknown_key_comes_back_unknown_not_zero():
    """The headline property, at the table layer.

    ``amount`` is None rather than zero so an unpriced call cannot be summed
    into a total by accident anywhere downstream.
    """
    quote = table().quote("video.render", 600)
    assert not quote.known
    assert quote.amount is None


def test_requiring_an_unknown_quote_raises():
    """Any caller that wants a number must handle the case where there isn't one."""
    with pytest.raises(UnknownPriceError):
        table().quote("video.render", 600).require()


def test_estimate_mode_requires_a_declared_fallback_price():
    """There is deliberately no "treat unknown as zero" setting.

    An operator who wants unpriced work to pass has to name the number, which
    is a decision with a value attached instead of an invisible default.
    """
    with pytest.raises(PriceConfigError) as excinfo:
        PriceTable(rules={}, on_missing=OnMissing.ESTIMATE)
    assert "fallback_price" in str(excinfo.value)


def test_estimate_mode_prices_unknown_work_and_flags_it():
    """When the operator opts in, the estimate is visible, not silent."""
    estimating = table(on_missing="estimate", fallback_price="0.10")
    quote = estimating.quote("video.render", 3)
    assert quote.require() == Money.parse("0.30")
    assert quote.estimated is True


def test_known_prices_are_never_flagged_as_estimates():
    """The flag has to mean something, so it must not be set on real rules."""
    assert table().quote("image.render", 1).estimated is False


def test_wildcard_rule_covers_a_family_of_keys():
    """New model names appear constantly; a family rule keeps them priced.

    Without this, the first call to a newly released model in a known family
    is blocked, and the operator's only fix is an emergency config change.
    """
    wildcard = PriceTable.from_mapping({"rules": {"gpu.*": {"unit_price": "0.90"}}})
    assert wildcard.quote("gpu.large", 2).require() == Money.parse("1.80")


def test_the_longest_matching_wildcard_wins():
    """A more specific pattern is a deliberate override of a broader one."""
    nested = PriceTable.from_mapping(
        {"rules": {"gpu.*": {"unit_price": "0.90"}, "gpu.large.*": {"unit_price": "2.50"}}}
    )
    assert nested.quote("gpu.large.x2", 1).require() == Money.parse("2.50")


def test_an_exact_rule_beats_a_wildcard():
    """Pinning one key must not be defeated by a family rule that also matches."""
    mixed = PriceTable.from_mapping(
        {"rules": {"gpu.*": {"unit_price": "0.90"}, "gpu.large": {"unit_price": "2.50"}}}
    )
    assert mixed.quote("gpu.large", 1).require() == Money.parse("2.50")


def test_a_wildcard_does_not_match_an_unrelated_prefix():
    """Prefix matching must not leak across namespaces."""
    wildcard = PriceTable.from_mapping({"rules": {"gpu.*": {"unit_price": "0.90"}}})
    assert not wildcard.quote("cpu.small", 1).known


def test_overrides_replace_a_shipped_price_without_editing_the_table():
    """An operator whose contract price differs needs a seam, not a fork."""
    pinned = table().with_overrides({"image.render": {"unit_price": "0.01"}})
    assert pinned.quote("image.render", 10).require() == Money.parse("0.10")
    assert table().quote("image.render", 10).require() == Money.parse("0.40")


def test_overrides_can_add_a_missing_key():
    """The fastest fix for a blocked call is to price it on the command line."""
    extended = table().with_overrides({"video.render": "0.50"})
    assert extended.quote("video.render", 2).require() == Money.parse("1.00")


def test_merging_lets_the_later_table_win():
    """Layering a local table over a shared one is the normal deployment shape."""
    base = PriceTable.from_mapping({"rules": {"a": "1.00", "b": "2.00"}})
    local = PriceTable.from_mapping({"rules": {"b": "5.00"}})
    merged = base.merged(local)
    assert merged.quote("a").require() == Money.parse("1.00")
    assert merged.quote("b").require() == Money.parse("5.00")


def test_negative_prices_are_rejected():
    """A negative price would credit the budget on every call."""
    with pytest.raises(PriceConfigError):
        PriceRule(key="x", unit_price=Money.parse("-1"))


def test_per_must_be_a_positive_integer():
    """``per=0`` would divide by zero at the first quote."""
    with pytest.raises(PriceConfigError):
        PriceRule(key="x", unit_price=Money.parse("1"), per=0)


def test_negative_quantities_are_rejected():
    """A negative quantity would produce a negative charge and refund the budget."""
    with pytest.raises(PriceConfigError):
        table().quote("image.render", -1)


def test_float_quantities_are_rejected():
    """0.1 hours as a float is not 0.1, and the boundary comparison becomes a coin flip."""
    with pytest.raises(PriceConfigError):
        table().quote("image.render", 1.5)


def test_decimal_quantities_are_accepted():
    """Fractional work is real: 2.5 GPU-hours has to be expressible."""
    hourly = PriceTable.from_mapping({"rules": {"gpu": {"unit_price": "0.90"}}})
    assert hourly.quote("gpu", Decimal("2.5")).require() == Money.parse("2.25")


def test_a_price_rule_missing_its_price_is_rejected():
    """A rule with a unit but no price would quote nothing and read as configured."""
    with pytest.raises(PriceConfigError):
        PriceTable.from_mapping({"rules": {"x": {"unit": "token"}}})


def test_unknown_rule_keys_are_rejected():
    """A misspelled "minimun" would silently drop the floor charge."""
    with pytest.raises(PriceConfigError) as excinfo:
        PriceTable.from_mapping({"rules": {"x": {"unit_price": "1", "minimun": "1"}}})
    assert "minimun" in str(excinfo.value)


def test_a_bare_mapping_of_keys_is_accepted_as_a_table():
    """The shortest useful price file is ``{"image.render": "0.04"}``."""
    bare = PriceTable.from_mapping({"image.render": "0.04"})
    assert bare.quote("image.render", 2).require() == Money.parse("0.08")


def test_table_round_trips_through_a_mapping():
    """A table written back out must price identically."""
    original = table()
    assert PriceTable.from_mapping(original.to_mapping()).quote(
        "speech.say", 2000
    ).require() == original.quote("speech.say", 2000).require()


def test_contains_and_knows_agree_including_wildcards():
    """Membership is used to report coverage; it must follow the same resolution."""
    wildcard = PriceTable.from_mapping({"rules": {"gpu.*": "0.90"}})
    assert "gpu.any" in wildcard
    assert wildcard.knows("gpu.any")
    assert "cpu" not in wildcard
