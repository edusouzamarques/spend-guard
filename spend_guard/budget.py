"""The budget declaration: ceilings, categories and rollover.

A budget is *data*. It says what the ceiling is, how the period is cut, which
sub-budgets exist and what happens to an unused or overdrawn balance at the
boundary. It contains no vendor names and no knowledge of what is being bought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional

from .errors import BudgetConfigError
from .money import Money, Rounding
from .periods import Period

__all__ = [
    "RolloverPolicy",
    "RolloverSpec",
    "CategoryBudget",
    "BudgetSpec",
    "compute_carry",
    "effective_ceiling",
    "UNCATEGORISED",
]

#: Bucket name used when a caller does not name a category.
UNCATEGORISED = ""

_DEFAULT_WARN = Decimal("0.8")


class RolloverPolicy(str, Enum):
    """What crosses a period boundary."""

    NONE = "none"
    """Unused budget evaporates and an overspend is forgiven. The default."""

    CARRY_UNUSED = "carry_unused"
    """Unused budget is added to the next period, up to ``cap``."""

    CARRY_DEFICIT = "carry_deficit"
    """An overspend is subtracted from the next period, down to ``-cap``."""

    CARRY_BOTH = "carry_both"
    """Both directions carry, bounded by ``cap`` in magnitude."""


@dataclass(frozen=True)
class RolloverSpec:
    """How a balance moves between periods.

    ``max_lookback`` bounds how far back the carry chain is recomputed. Carry
    compounds across periods, but a ledger does not necessarily go back to the
    beginning of time, so the chain is bounded and the bound is explicit.
    """

    policy: RolloverPolicy = RolloverPolicy.NONE
    cap: Optional[Money] = None
    max_lookback: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.policy, RolloverPolicy):
            object.__setattr__(self, "policy", RolloverPolicy(str(self.policy).lower()))
        if self.cap is not None:
            cap = Money.parse(self.cap, rounding=Rounding.DOWN)
            if cap.is_negative:
                raise BudgetConfigError("rollover cap cannot be negative")
            object.__setattr__(self, "cap", cap)
        if self.max_lookback < 0:
            raise BudgetConfigError("rollover max_lookback cannot be negative")

    @property
    def carries(self) -> bool:
        return self.policy is not RolloverPolicy.NONE

    @classmethod
    def from_mapping(cls, data) -> "RolloverSpec":
        if data is None:
            return cls()
        if isinstance(data, RolloverSpec):
            return data
        if isinstance(data, str):
            return cls(policy=_rollover_policy(data))
        if not isinstance(data, dict):
            raise BudgetConfigError(
                f"cannot read a rollover spec from {type(data).__name__}"
            )
        cap = data.get("cap")
        return cls(
            policy=_rollover_policy(data.get("policy", "none")),
            cap=None if cap is None else Money.parse(cap, rounding=Rounding.DOWN),
            max_lookback=int(data.get("max_lookback", 3)),
        )

    def to_mapping(self) -> dict:
        out: Dict[str, object] = {"policy": self.policy.value}
        if self.cap is not None:
            out["cap"] = self.cap.format()
        if self.max_lookback != 3:
            out["max_lookback"] = self.max_lookback
        return out


def _rollover_policy(value) -> RolloverPolicy:
    if isinstance(value, RolloverPolicy):
        return value
    try:
        return RolloverPolicy(str(value).lower())
    except ValueError:
        valid = ", ".join(p.value for p in RolloverPolicy)
        raise BudgetConfigError(
            f"unknown rollover policy {value!r}; valid: {valid}"
        ) from None


@dataclass(frozen=True)
class CategoryBudget:
    """A sub-ceiling inside the total."""

    name: str
    ceiling: Money
    warn_threshold: Optional[Decimal] = None

    def __post_init__(self) -> None:
        ceiling = Money.parse(self.ceiling, rounding=Rounding.DOWN)
        if ceiling.is_negative:
            raise BudgetConfigError(
                f"category {self.name!r} has a negative ceiling"
            )
        object.__setattr__(self, "ceiling", ceiling)
        if self.warn_threshold is not None:
            object.__setattr__(
                self, "warn_threshold", _validate_threshold(self.warn_threshold, self.name)
            )


def _validate_threshold(value, where: str) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except Exception:
        raise BudgetConfigError(
            f"{where}: warn threshold {value!r} is not a number"
        ) from None
    if not (Decimal(0) < threshold <= Decimal(1)):
        raise BudgetConfigError(
            f"{where}: warn threshold must be in (0, 1], got {threshold}"
        )
    return threshold


def _category_mapping(entry: CategoryBudget) -> Dict[str, object]:
    """Serialise one category, keeping a declared threshold.

    Dropping ``warn_threshold`` here made writing a budget back out a lossy
    operation: the setting an operator tuned for one noisy category vanished on
    the next round trip, silently.
    """
    out: Dict[str, object] = {"ceiling": entry.ceiling.format()}
    if entry.warn_threshold is not None:
        out["warn_threshold"] = str(entry.warn_threshold)
    return out


@dataclass(frozen=True)
class BudgetSpec:
    """A complete budget declaration."""

    ceiling: Money
    period: Period = field(default_factory=Period.monthly)
    warn_threshold: Decimal = _DEFAULT_WARN
    categories: Mapping[str, CategoryBudget] = field(default_factory=dict)
    rollover: RolloverSpec = field(default_factory=RolloverSpec)
    currency: str = "USD"
    allow_uncategorised: bool = True

    def __post_init__(self) -> None:
        ceiling = Money.parse(self.ceiling, rounding=Rounding.DOWN)
        if ceiling.is_negative:
            raise BudgetConfigError("the total ceiling cannot be negative")
        object.__setattr__(self, "ceiling", ceiling)
        object.__setattr__(
            self, "warn_threshold", _validate_threshold(self.warn_threshold, "budget")
        )
        normalised: Dict[str, CategoryBudget] = {}
        for name, value in dict(self.categories).items():
            if isinstance(value, CategoryBudget):
                entry = value if value.name == name else CategoryBudget(
                    name=name, ceiling=value.ceiling, warn_threshold=value.warn_threshold
                )
            elif isinstance(value, dict):
                entry = CategoryBudget(
                    name=name,
                    ceiling=value.get("ceiling", value.get("limit", 0)),
                    warn_threshold=value.get("warn_threshold"),
                )
            else:
                entry = CategoryBudget(name=name, ceiling=value)
            if not name:
                raise BudgetConfigError("a category cannot have an empty name")
            normalised[name] = entry
        object.__setattr__(self, "categories", normalised)
        total = Money.zero()
        for entry in normalised.values():
            total = total + entry.ceiling
        if total.micros > ceiling.micros:
            raise BudgetConfigError(
                "category ceilings sum to "
                f"{total.format()} which exceeds the total ceiling {ceiling.format()}; "
                "a sub-budget that cannot be spent is a declaration bug, not a cap"
            )
        if not self.currency or not str(self.currency).strip():
            raise BudgetConfigError("currency must be a non-empty label")
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        if not self.allow_uncategorised and not normalised:
            raise BudgetConfigError(
                "allow_uncategorised is false but no categories are declared, "
                "so no spend of any kind could ever be authorised"
            )

    # -- queries -----------------------------------------------------------

    def category(self, name: str) -> Optional[CategoryBudget]:
        return self.categories.get(name)

    def knows_category(self, name: str) -> bool:
        if name == UNCATEGORISED:
            return self.allow_uncategorised
        return name in self.categories

    def warn_threshold_for(self, name: str) -> Decimal:
        entry = self.categories.get(name)
        if entry is not None and entry.warn_threshold is not None:
            return entry.warn_threshold
        return self.warn_threshold

    @property
    def unallocated(self) -> Money:
        """Total ceiling not claimed by any declared category."""
        claimed = Money.zero()
        for entry in self.categories.values():
            claimed = claimed + entry.ceiling
        return self.ceiling - claimed

    # -- serialisation -----------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Mapping) -> "BudgetSpec":
        if isinstance(data, BudgetSpec):
            return data
        if not isinstance(data, Mapping):
            raise BudgetConfigError(
                f"a budget must be a mapping, got {type(data).__name__}"
            )
        if "ceiling" not in data:
            raise BudgetConfigError(
                "budget is missing the required 'ceiling' key; a budget without a "
                "hard ceiling is a dashboard, not a guard"
            )
        unknown = set(data) - {
            "ceiling",
            "period",
            "warn_threshold",
            "categories",
            "rollover",
            "currency",
            "allow_uncategorised",
        }
        if unknown:
            raise BudgetConfigError(
                "unknown budget keys: " + ", ".join(sorted(unknown))
            )
        return cls(
            ceiling=Money.parse(data["ceiling"], rounding=Rounding.DOWN),
            period=Period.from_mapping(data.get("period", {})),
            warn_threshold=data.get("warn_threshold", _DEFAULT_WARN),
            categories=data.get("categories", {}) or {},
            rollover=RolloverSpec.from_mapping(data.get("rollover")),
            currency=data.get("currency", "USD"),
            allow_uncategorised=bool(data.get("allow_uncategorised", True)),
        )

    def to_mapping(self) -> dict:
        return {
            "currency": self.currency,
            "period": self.period.to_mapping(),
            "ceiling": self.ceiling.format(),
            "warn_threshold": str(self.warn_threshold),
            "allow_uncategorised": self.allow_uncategorised,
            "rollover": self.rollover.to_mapping(),
            "categories": {
                name: _category_mapping(entry)
                for name, entry in sorted(self.categories.items())
            },
        }


# --------------------------------------------------------------------------
# Rollover arithmetic (pure)
# --------------------------------------------------------------------------


def compute_carry(ceiling: Money, spent: Money, spec: RolloverSpec) -> Money:
    """What a period hands to the next one.

    Positive is unused budget, negative is a deficit. The cap bounds the
    magnitude in both directions, so an unattended system cannot accumulate a
    year of unused budget into one enormous month.
    """
    balance = ceiling - spent
    policy = spec.policy
    if policy is RolloverPolicy.NONE:
        return Money.zero()
    if policy is RolloverPolicy.CARRY_UNUSED:
        carry = balance.clamp_min(Money.zero())
    elif policy is RolloverPolicy.CARRY_DEFICIT:
        carry = balance.clamp_max(Money.zero())
    else:
        carry = balance
    if spec.cap is not None:
        carry = carry.clamp_max(spec.cap).clamp_min(-spec.cap)
    return carry


def effective_ceiling(base: Money, carry_in: Money) -> Money:
    """Apply a carry to a base ceiling, floored at zero.

    A deficit larger than the next period's budget produces a ceiling of zero,
    which blocks everything, rather than a negative ceiling whose comparisons
    would read strangely at every call site.
    """
    return (base + carry_in).clamp_min(Money.zero())
