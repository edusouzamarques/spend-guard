"""Pricing: map a unit of work to a cost estimate.

The load-bearing decision in this module is what happens when a key is *not*
in the table. The answer is: the quote comes back unknown, and an unknown quote
is not a number. It cannot be added to a running total, it cannot be compared
to a ceiling, and it never becomes zero on the way through.

That is not defensive style, it is the failure this package exists to prevent.
A ceiling stops meaning anything the moment one unpriced call is treated as
free, because unpriced calls are exactly the new, unfamiliar, expensive ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Union

from .errors import PriceConfigError, UnknownPriceError
from .money import Money, Rounding

__all__ = [
    "OnMissing",
    "PriceRule",
    "Quote",
    "PriceTable",
    "parse_units",
]

Units = Union[int, str, Decimal]


def parse_units(value: Units) -> Decimal:
    """Parse a quantity of work exactly.

    Floats are rejected for the same reason they are rejected for money: a
    quantity of 0.1 hours that is really 0.1000000000000000055 turns a ceiling
    comparison into a coin flip at the boundary.
    """
    if isinstance(value, bool):
        raise PriceConfigError("bool is not a quantity")
    if isinstance(value, float):
        raise PriceConfigError(
            "float is not accepted as a quantity; pass a string or Decimal, "
            f'e.g. "{value!r}"'
        )
    if isinstance(value, Decimal):
        units = value
    elif isinstance(value, int):
        units = Decimal(value)
    elif isinstance(value, str):
        try:
            units = Decimal(value.strip().replace("_", ""))
        except InvalidOperation:
            raise PriceConfigError(f"cannot parse {value!r} as a quantity") from None
    else:
        raise PriceConfigError(
            f"cannot parse {type(value).__name__} as a quantity"
        )
    if not units.is_finite():
        raise PriceConfigError("a quantity must be finite")
    if units < 0:
        raise PriceConfigError(f"a quantity cannot be negative, got {units}")
    return units


class OnMissing(str, Enum):
    """What a table does with a key it does not know."""

    BLOCK = "block"
    """Return an unknown quote. The guard refuses the spend. The default."""

    ESTIMATE = "estimate"
    """Use the table's declared ``fallback_price``, flagged as an estimate.

    There is no "treat as zero" option. An operator who genuinely wants
    unpriced work to pass must name a price for it, and every ledger entry
    produced that way is marked ``estimated`` so it is visible in an audit.
    """


@dataclass(frozen=True)
class PriceRule:
    """A cost estimate for one kind of work.

    ``unit_price`` is the price of ``per`` units, so a rate quoted as
    "$3.00 per million tokens" is expressed exactly rather than as a
    sub-micro-unit per-token price that would have to be rounded first.
    """

    key: str
    unit_price: Money
    unit: str = "call"
    per: int = 1
    minimum: Optional[Money] = None

    def __post_init__(self) -> None:
        if not self.key:
            raise PriceConfigError("a price rule needs a key")
        price = Money.parse(self.unit_price, rounding=Rounding.UP)
        if price.is_negative:
            raise PriceConfigError(f"{self.key}: unit_price cannot be negative")
        object.__setattr__(self, "unit_price", price)
        if isinstance(self.per, bool) or not isinstance(self.per, int) or self.per < 1:
            raise PriceConfigError(f"{self.key}: 'per' must be a positive integer")
        if self.minimum is not None:
            minimum = Money.parse(self.minimum, rounding=Rounding.UP)
            if minimum.is_negative:
                raise PriceConfigError(f"{self.key}: minimum cannot be negative")
            object.__setattr__(self, "minimum", minimum)
        if not self.unit:
            raise PriceConfigError(f"{self.key}: unit must be a non-empty label")

    @property
    def is_wildcard(self) -> bool:
        return self.key.endswith("*")

    @property
    def prefix(self) -> str:
        return self.key[:-1] if self.is_wildcard else self.key

    def cost(self, units: Decimal) -> Money:
        """Cost of ``units``, rounded **up** to the nearest micro-unit.

        Rounding up is deliberate. Half a million calls each rounded down by a
        third of a micro-unit is a real number of dollars that the ledger would
        never see.
        """
        amount = self.unit_price.scale(units / Decimal(self.per), rounding=Rounding.UP)
        if self.minimum is not None and amount.micros < self.minimum.micros:
            return self.minimum
        return amount

    @classmethod
    def from_mapping(cls, key: str, data) -> "PriceRule":
        if isinstance(data, PriceRule):
            return data if data.key == key else PriceRule(
                key=key,
                unit_price=data.unit_price,
                unit=data.unit,
                per=data.per,
                minimum=data.minimum,
            )
        if isinstance(data, (str, int, Decimal, Money)):
            return cls(key=key, unit_price=Money.parse(data, rounding=Rounding.UP))
        if not isinstance(data, Mapping):
            raise PriceConfigError(
                f"{key}: cannot read a price rule from {type(data).__name__}"
            )
        unknown = set(data) - {"unit_price", "price", "unit", "per", "minimum"}
        if unknown:
            raise PriceConfigError(
                f"{key}: unknown price rule keys: " + ", ".join(sorted(unknown))
            )
        if "unit_price" in data:
            price = data["unit_price"]
        elif "price" in data:
            price = data["price"]
        else:
            raise PriceConfigError(f"{key}: price rule is missing 'unit_price'")
        return cls(
            key=key,
            unit_price=Money.parse(price, rounding=Rounding.UP),
            unit=str(data.get("unit", "call")),
            per=int(data.get("per", 1)),
            minimum=(
                None if data.get("minimum") is None
                else Money.parse(data["minimum"], rounding=Rounding.UP)
            ),
        )

    def to_mapping(self) -> dict:
        out: Dict[str, object] = {"unit_price": self.unit_price.format(), "unit": self.unit}
        if self.per != 1:
            out["per"] = self.per
        if self.minimum is not None:
            out["minimum"] = self.minimum.format()
        return out


@dataclass(frozen=True)
class Quote:
    """The outcome of pricing one unit of work.

    ``amount`` is ``None`` exactly when ``known`` is false. There is no
    sentinel zero to accidentally sum.
    """

    key: str
    units: Decimal
    amount: Optional[Money] = None
    rule_key: Optional[str] = None
    unit: str = ""
    estimated: bool = False

    @property
    def known(self) -> bool:
        return self.amount is not None

    def require(self) -> Money:
        """Return the amount, or raise :class:`UnknownPriceError`."""
        if self.amount is None:
            raise UnknownPriceError(
                f"no price is declared for {self.key!r}; refusing to treat an "
                "unpriced unit of work as free"
            )
        return self.amount

    def __str__(self) -> str:
        if self.amount is None:
            return f"{self.key} x{self.units}: UNKNOWN PRICE"
        tag = " (estimated)" if self.estimated else ""
        return f"{self.key} x{self.units}: {self.amount.format()}{tag}"


@dataclass(frozen=True)
class PriceTable:
    """A set of rules plus an explicit policy for keys it does not cover."""

    rules: Mapping[str, PriceRule] = field(default_factory=dict)
    on_missing: OnMissing = OnMissing.BLOCK
    fallback_price: Optional[Money] = None

    def __post_init__(self) -> None:
        if not isinstance(self.on_missing, OnMissing):
            try:
                object.__setattr__(
                    self, "on_missing", OnMissing(str(self.on_missing).lower())
                )
            except ValueError:
                valid = ", ".join(o.value for o in OnMissing)
                raise PriceConfigError(
                    f"unknown on_missing {self.on_missing!r}; valid: {valid}"
                ) from None
        normalised: Dict[str, PriceRule] = {}
        for key, value in dict(self.rules).items():
            normalised[key] = PriceRule.from_mapping(key, value)
        object.__setattr__(self, "rules", normalised)
        if self.fallback_price is not None:
            price = Money.parse(self.fallback_price, rounding=Rounding.UP)
            if price.is_negative:
                raise PriceConfigError("fallback_price cannot be negative")
            object.__setattr__(self, "fallback_price", price)
        if self.on_missing is OnMissing.ESTIMATE and self.fallback_price is None:
            raise PriceConfigError(
                "on_missing='estimate' requires a fallback_price; the point of "
                "the setting is to name the number, not to hide it"
            )

    # -- lookup ------------------------------------------------------------

    def resolve(self, key: str) -> Optional[PriceRule]:
        """Find the rule for ``key``: exact match first, then longest wildcard.

        Longest-prefix wins so ``vendor.family.*`` beats ``vendor.*`` for a key
        under both, which is what an operator means when they write the more
        specific pattern.
        """
        exact = self.rules.get(key)
        if exact is not None and not exact.is_wildcard:
            return exact
        best: Optional[PriceRule] = None
        for rule in self.rules.values():
            if not rule.is_wildcard:
                continue
            if key.startswith(rule.prefix):
                if best is None or len(rule.prefix) > len(best.prefix):
                    best = rule
        return best

    def quote(self, key: str, units: Units = 1) -> Quote:
        """Price a unit of work. Never raises for an unknown key."""
        quantity = parse_units(units)
        rule = self.resolve(key)
        if rule is not None:
            return Quote(
                key=key,
                units=quantity,
                amount=rule.cost(quantity),
                rule_key=rule.key,
                unit=rule.unit,
                estimated=False,
            )
        if self.on_missing is OnMissing.ESTIMATE:
            assert self.fallback_price is not None  # guarded in __post_init__
            return Quote(
                key=key,
                units=quantity,
                amount=self.fallback_price.scale(quantity, rounding=Rounding.UP),
                rule_key=None,
                unit="call",
                estimated=True,
            )
        return Quote(key=key, units=quantity, amount=None, rule_key=None)

    def quote_or_raise(self, key: str, units: Units = 1) -> Money:
        return self.quote(key, units).require()

    def knows(self, key: str) -> bool:
        return self.resolve(key) is not None

    # -- composition -------------------------------------------------------

    def with_overrides(self, overrides: Mapping[str, object]) -> "PriceTable":
        """A copy with extra or replaced rules.

        Overrides are how an operator pins a price the shipped table gets wrong
        without editing the shipped table.
        """
        merged: Dict[str, PriceRule] = dict(self.rules)
        for key, value in overrides.items():
            merged[key] = PriceRule.from_mapping(key, value)
        return PriceTable(
            rules=merged,
            on_missing=self.on_missing,
            fallback_price=self.fallback_price,
        )

    def merged(self, other: "PriceTable") -> "PriceTable":
        """A copy with ``other``'s rules and missing-policy taking precedence."""
        merged: Dict[str, PriceRule] = dict(self.rules)
        merged.update(other.rules)
        return PriceTable(
            rules=merged,
            on_missing=other.on_missing,
            fallback_price=other.fallback_price,
        )

    def keys(self) -> Iterable[str]:
        return sorted(self.rules)

    # -- serialisation -----------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Mapping) -> "PriceTable":
        if isinstance(data, PriceTable):
            return data
        if not isinstance(data, Mapping):
            raise PriceConfigError(
                f"a price table must be a mapping, got {type(data).__name__}"
            )
        if "rules" in data:
            unknown = set(data) - {"rules", "on_missing", "fallback_price"}
            if unknown:
                raise PriceConfigError(
                    "unknown price table keys: " + ", ".join(sorted(unknown))
                )
            rules = data.get("rules") or {}
            on_missing = data.get("on_missing", OnMissing.BLOCK)
            fallback = data.get("fallback_price")
        else:
            # A bare mapping of key -> rule is accepted as a convenience.
            rules = data
            on_missing = OnMissing.BLOCK
            fallback = None
        if not isinstance(rules, Mapping):
            raise PriceConfigError("price table 'rules' must be a mapping")
        return cls(
            rules={key: PriceRule.from_mapping(key, value) for key, value in rules.items()},
            on_missing=on_missing,
            fallback_price=fallback,
        )

    def to_mapping(self) -> dict:
        out: Dict[str, object] = {
            "on_missing": self.on_missing.value,
            "rules": {key: rule.to_mapping() for key, rule in sorted(self.rules.items())},
        }
        if self.fallback_price is not None:
            out["fallback_price"] = self.fallback_price.format()
        return out

    def __len__(self) -> int:
        return len(self.rules)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.knows(key)
