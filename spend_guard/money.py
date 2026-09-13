"""Exact money arithmetic.

Money is stored as an integer number of *micro-units* (one millionth of the
currency unit). Two reasons:

* Binary floats cannot represent 0.10 exactly, and a budget ledger that drifts
  by a rounding error per call is worse than no ledger.
* Per-token AI pricing lives in the fifth and sixth decimal place. Cents are
  too coarse; micro-units carry rates like ``$0.000003 / token`` exactly.

Rounding is never "nearest". When a value cannot be represented exactly the
caller states which way it must go, and the library always chooses the
direction that protects the budget: costs round **up**, ceilings round **down**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import Union

from .errors import MoneyError

__all__ = ["MICROS_PER_UNIT", "Money", "Rounding", "MoneyLike"]

#: Micro-units in one currency unit. Six decimal places.
MICROS_PER_UNIT = 1_000_000

_QUANTUM = Decimal(1).scaleb(-6)

#: The only shape in which a comma is accepted: a thousands separator in a
#: number that uses ``.`` for its decimal point. Stripping commas without this
#: check turns the European ``"1,50"`` into ``150`` — a ceiling a hundred times
#: larger than the one the operator declared, with no error anywhere.
_GROUPED_DIGITS = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


class Rounding(str, Enum):
    """Direction to take when a value needs more precision than we store.

    ``UP`` is toward positive infinity and ``DOWN`` toward negative infinity,
    so the choice stays meaningful for negative amounts (a carried deficit).
    """

    UP = "up"
    DOWN = "down"


MoneyLike = Union["Money", int, str, Decimal]


@dataclass(frozen=True, order=True)
class Money:
    """An exact amount, held as signed integer micro-units.

    Negative values are legal: they represent a deficit carried into a period.
    Validation that a *ceiling* is non-negative happens where ceilings are
    declared, not here.
    """

    micros: int

    def __post_init__(self) -> None:
        if isinstance(self.micros, bool) or not isinstance(self.micros, int):
            raise MoneyError(
                "Money(micros=...) takes an int; use Money.parse() for decimal text"
            )

    # -- construction ------------------------------------------------------

    @classmethod
    def zero(cls) -> "Money":
        return cls(0)

    @classmethod
    def from_micros(cls, micros: int) -> "Money":
        return cls(int(micros))

    @classmethod
    def parse(cls, value: MoneyLike, *, rounding: Rounding = Rounding.UP) -> "Money":
        """Parse ``value`` into an exact amount.

        Accepts :class:`Money`, ``int``, ``str`` and :class:`decimal.Decimal`.
        ``float`` is **rejected**: ``0.1 + 0.2`` is not ``0.3`` and a budget
        that inherits that is not a budget. Pass the literal as a string.
        """
        if isinstance(value, Money):
            return value
        if isinstance(value, bool):
            raise MoneyError("bool is not a monetary amount")
        if isinstance(value, float):
            raise MoneyError(
                "float is not accepted as money because it cannot hold decimal "
                "fractions exactly; pass the amount as a string, e.g. "
                f'Money.parse("{value!r}")'
            )
        if isinstance(value, int):
            return cls(value * MICROS_PER_UNIT)
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, str):
            text = value.strip().replace("_", "")
            if text.startswith("+"):
                text = text[1:]
            if not text:
                raise MoneyError("empty string is not a monetary amount")
            if "," in text:
                if not _GROUPED_DIGITS.match(text):
                    raise MoneyError(
                        f"cannot parse {value!r} as a monetary amount: a comma is "
                        "only accepted as a thousands separator in groups of "
                        'three (e.g. "1,200" or "1,234,567.89"). Write the '
                        'decimal point as "." — "1,50" is ambiguous and would '
                        "otherwise silently parse as 150.00"
                    )
                text = text.replace(",", "")
            try:
                dec = Decimal(text)
            except InvalidOperation:
                raise MoneyError(f"cannot parse {value!r} as a monetary amount") from None
        else:
            raise MoneyError(
                f"cannot parse {type(value).__name__} as a monetary amount"
            )
        return cls.from_decimal(dec, rounding)

    @classmethod
    def from_decimal(
        cls, dec: Decimal, rounding: Rounding = Rounding.UP
    ) -> "Money":
        """Quantise an exact :class:`~decimal.Decimal` to micro-units."""
        if not dec.is_finite():
            raise MoneyError(f"{dec} is not a finite monetary amount")
        mode = ROUND_CEILING if rounding is Rounding.UP else ROUND_FLOOR
        try:
            quantised = dec.quantize(_QUANTUM, rounding=mode)
        except (InvalidOperation, ArithmeticError):
            raise MoneyError(f"{dec} is too large to represent as money") from None
        return cls(int(quantised.scaleb(6)))

    # -- arithmetic --------------------------------------------------------

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.micros + other.micros)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.micros - other.micros)

    def __neg__(self) -> "Money":
        return Money(-self.micros)

    def __abs__(self) -> "Money":
        return Money(abs(self.micros))

    def __mul__(self, factor: int) -> "Money":
        if isinstance(factor, bool) or not isinstance(factor, int):
            return NotImplemented
        return Money(self.micros * factor)

    __rmul__ = __mul__

    def scale(self, factor: Decimal, *, rounding: Rounding = Rounding.UP) -> "Money":
        """Multiply by an exact ratio, rounding in the stated direction."""
        if isinstance(factor, float):
            raise MoneyError("scale() takes a Decimal, not a float")
        if not isinstance(factor, Decimal):
            factor = Decimal(factor)
        if not factor.is_finite():
            raise MoneyError("cannot scale money by a non-finite factor")
        return Money.from_decimal(self.as_decimal() * factor, rounding)

    def clamp_min(self, floor: "Money") -> "Money":
        return self if self.micros >= floor.micros else floor

    def clamp_max(self, cap: "Money") -> "Money":
        return self if self.micros <= cap.micros else cap

    # -- predicates --------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.micros == 0

    @property
    def is_negative(self) -> bool:
        return self.micros < 0

    @property
    def is_positive(self) -> bool:
        return self.micros > 0

    def __bool__(self) -> bool:
        return self.micros != 0

    # -- rendering ---------------------------------------------------------

    def as_decimal(self) -> Decimal:
        return Decimal(self.micros).scaleb(-6)

    def format(self, places: int = None) -> str:  # type: ignore[assignment]
        """Render as plain decimal text.

        With ``places=None`` the amount keeps two decimals unless it needs more
        to stay exact, so ``1200`` reads as ``1200.00`` and a per-token rate
        still reads as ``0.000003``.
        """
        dec = self.as_decimal()
        if places is not None:
            return f"{dec:.{int(places)}f}"
        text = f"{dec:.6f}"
        whole, _, frac = text.partition(".")
        frac = frac.rstrip("0")
        if len(frac) < 2:
            frac = frac.ljust(2, "0")
        return f"{whole}.{frac}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.format()})"
