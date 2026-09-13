"""spend-guard: hard budget ceilings that fail closed.

A per-call price estimate is useless if nothing refuses the call once the
month's budget is gone. This package is the thing that refuses.

    from spend_guard import BudgetSpec, PriceTable, SpendGuard, FileLedger, Money

    guard = SpendGuard(
        BudgetSpec.from_mapping({"ceiling": "1200.00", "categories": {...}}),
        PriceTable.from_mapping({"rules": {...}}),
        FileLedger("var/spend.ndjson"),
    )

    with guard.charge("text.generate", units=8000, category="text") as ticket:
        response = call_the_api()
        ticket.actual(Money.parse("0.031"))

Two properties are load-bearing and are pinned by the test suite:

* An **unknown price blocks**. It is never quietly treated as zero.
* A **crash between reserve and commit does not leak** the reservation: the
  hold is leased, and the arithmetic stops counting it the moment the lease
  lapses, with or without anything running to clean up.
"""

from __future__ import annotations

from .budget import (
    BudgetSpec,
    CategoryBudget,
    RolloverPolicy,
    RolloverSpec,
    UNCATEGORISED,
    compute_carry,
    effective_ceiling,
)
from .clock import Clock, FixedClock, SystemClock, ensure_utc
from .config import (
    build_guard,
    guard_from_env,
    load_budget,
    load_mapping,
    load_prices,
)
from .errors import (
    BudgetConfigError,
    BudgetExceededError,
    ConfigError,
    DeniedError,
    HaltedError,
    LedgerCorruptError,
    MissingDependencyError,
    MoneyError,
    PeriodError,
    PriceConfigError,
    ReservationConflictError,
    ReservationError,
    ReservationStateError,
    SpendGuardError,
    UnknownPriceError,
    UnknownReservationError,
)
from .guard import (
    DEFAULT_LEASE,
    CategorySnapshot,
    Decision,
    DenialReason,
    Overlay,
    Snapshot,
    SpendGuard,
    SpendRequest,
    Ticket,
)
from .ledger import Entry, EntryType, FileLedger, Ledger, LedgerReport, MemoryLedger
from .money import MICROS_PER_UNIT, Money, Rounding
from .periods import Period, PeriodKind, PeriodWindow
from .pricing import OnMissing, PriceRule, PriceTable, Quote, parse_units
from .projection import Projection, project
from .simulate import SimulationResult, SimulationStep, simulate
from .state import LedgerState, Reservation, ReservationStatus, replay

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # money and time
    "MICROS_PER_UNIT",
    "Money",
    "Rounding",
    "Clock",
    "FixedClock",
    "SystemClock",
    "ensure_utc",
    # periods
    "Period",
    "PeriodKind",
    "PeriodWindow",
    # budget
    "BudgetSpec",
    "CategoryBudget",
    "RolloverPolicy",
    "RolloverSpec",
    "UNCATEGORISED",
    "compute_carry",
    "effective_ceiling",
    # pricing
    "OnMissing",
    "PriceRule",
    "PriceTable",
    "Quote",
    "parse_units",
    # ledger and state
    "Entry",
    "EntryType",
    "FileLedger",
    "Ledger",
    "LedgerReport",
    "MemoryLedger",
    "LedgerState",
    "Reservation",
    "ReservationStatus",
    "replay",
    # enforcement
    "DEFAULT_LEASE",
    "CategorySnapshot",
    "Decision",
    "DenialReason",
    "Overlay",
    "Snapshot",
    "SpendGuard",
    "SpendRequest",
    "Ticket",
    # analysis
    "Projection",
    "project",
    "SimulationResult",
    "SimulationStep",
    "simulate",
    # configuration
    "build_guard",
    "guard_from_env",
    "load_budget",
    "load_mapping",
    "load_prices",
    # errors
    "SpendGuardError",
    "ConfigError",
    "BudgetConfigError",
    "PriceConfigError",
    "MissingDependencyError",
    "MoneyError",
    "PeriodError",
    "DeniedError",
    "UnknownPriceError",
    "BudgetExceededError",
    "HaltedError",
    "ReservationError",
    "UnknownReservationError",
    "ReservationStateError",
    "ReservationConflictError",
    "LedgerCorruptError",
]
