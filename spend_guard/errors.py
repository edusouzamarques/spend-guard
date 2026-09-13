"""Exception hierarchy.

Every failure mode a caller may reasonably want to catch separately gets its own
type. The base is :class:`SpendGuardError` so an integrator can wrap the whole
library in one ``except`` when it wants to fail the surrounding job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .guard import Decision


class SpendGuardError(Exception):
    """Base class for everything this package raises."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigError(SpendGuardError):
    """A declared budget or price table is malformed."""


class BudgetConfigError(ConfigError):
    """The budget declaration is internally inconsistent."""


class PriceConfigError(ConfigError):
    """The price table declaration is internally inconsistent."""


class MissingDependencyError(ConfigError):
    """An optional format needs a dependency that is not installed.

    The core package has zero runtime dependencies; TOML on Python < 3.11 and
    YAML in any version are optional extras. Failing with a named, actionable
    error beats an ``ImportError`` from three frames down.
    """


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class MoneyError(SpendGuardError, ValueError):
    """A monetary value could not be represented exactly or at all."""


class PeriodError(SpendGuardError, ValueError):
    """A budget period is not well defined."""


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


class DeniedError(SpendGuardError):
    """Base class for a refused spend. Carries the decision that refused it."""

    def __init__(self, message: str, decision: "Optional[Decision]" = None) -> None:
        super().__init__(message)
        self.decision = decision


class UnknownPriceError(DeniedError):
    """No price is known for the requested unit of work.

    This is deliberately an error and not a zero. A unit of work whose price
    nobody declared is the single most common way a budget ceiling silently
    stops meaning anything.
    """


class BudgetExceededError(DeniedError):
    """The requested spend does not fit under the remaining ceiling."""


class HaltedError(DeniedError):
    """The guard is halted; no spend of any size is permitted."""


# --------------------------------------------------------------------------
# Reservations and the ledger
# --------------------------------------------------------------------------


class ReservationError(SpendGuardError):
    """Base class for reservation lifecycle problems."""


class UnknownReservationError(ReservationError, KeyError):
    """The reservation id is not present in the ledger."""

    def __str__(self) -> str:  # KeyError repr-quotes its argument; undo that.
        return ReservationError.__str__(self)


class ReservationStateError(ReservationError):
    """The reservation cannot make that transition from its current state."""


class ReservationConflictError(ReservationError):
    """A repeated commit disagrees with the amount already recorded."""

    def __init__(self, message: str, recorded: Any = None, offered: Any = None) -> None:
        super().__init__(message)
        self.recorded = recorded
        self.offered = offered


class LedgerCorruptError(SpendGuardError):
    """The ledger cannot be replayed into a consistent state."""

    def __init__(self, message: str, line_number: Optional[int] = None) -> None:
        super().__init__(message)
        self.line_number = line_number
