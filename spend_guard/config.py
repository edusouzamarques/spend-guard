"""Loading declarations from files and the environment.

JSON works everywhere with no dependencies. TOML works on Python 3.11+ via
``tomllib`` and on older versions if ``tomli`` is installed. YAML needs
``PyYAML``. When a format is unavailable the error names the missing package
and the extra that provides it, instead of surfacing an ``ImportError`` from
somewhere the caller has never heard of.

The environment variables are the integration seam. Nothing in this package
knows the name of any vendor; an operator points ``SPEND_GUARD_BUDGET`` at
their declaration and wires their own calls through :class:`SpendGuard`.
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .budget import BudgetSpec
from .clock import Clock
from .errors import ConfigError, MissingDependencyError
from .guard import DEFAULT_LEASE, SpendGuard
from .ledger import FileLedger, Ledger, MemoryLedger
from .pricing import PriceTable

__all__ = [
    "ENV_BUDGET",
    "ENV_PRICES",
    "ENV_LEDGER",
    "ENV_LEASE_SECONDS",
    "load_mapping",
    "load_budget",
    "load_prices",
    "build_guard",
    "guard_from_env",
]

ENV_BUDGET = "SPEND_GUARD_BUDGET"
ENV_PRICES = "SPEND_GUARD_PRICES"
ENV_LEDGER = "SPEND_GUARD_LEDGER"
ENV_LEASE_SECONDS = "SPEND_GUARD_LEASE_SECONDS"


def _read_toml(text: str, path: Path) -> Mapping:
    try:  # Python 3.11+
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import-not-found]
    except ImportError:
        raise MissingDependencyError(
            f"{path}: reading TOML needs Python 3.11+ (stdlib tomllib) or the "
            "'tomli' package. Install it with: pip install 'spend-guard[toml]'"
        ) from None
    return tomli.loads(text)


def _read_yaml(text: str, path: Path) -> Mapping:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        raise MissingDependencyError(
            f"{path}: reading YAML needs the 'PyYAML' package. Install it with: "
            "pip install 'spend-guard[yaml]'"
        ) from None
    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    return loaded


def load_mapping(path) -> Mapping:
    """Read a JSON, TOML or YAML file into a plain mapping."""
    target = Path(path)
    if not target.exists():
        raise ConfigError(f"{target}: no such file")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{target}: cannot read ({exc})") from None
    suffix = target.suffix.lower()
    if suffix in (".json", ".jsonc", ""):
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"{target}: invalid JSON ({exc})") from None
    elif suffix == ".toml":
        loaded = _read_toml(text, target)
    elif suffix in (".yaml", ".yml"):
        loaded = _read_yaml(text, target)
    else:
        raise ConfigError(
            f"{target}: unsupported format {suffix!r}; use .json, .toml or .yaml"
        )
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"{target}: expected a mapping at the top level")
    return loaded


def load_budget(source) -> BudgetSpec:
    """Load a budget from a path or a mapping."""
    if isinstance(source, BudgetSpec):
        return source
    if isinstance(source, Mapping):
        return BudgetSpec.from_mapping(source)
    data = load_mapping(source)
    # Allow a combined file that nests both declarations.
    if "budget" in data and "ceiling" not in data:
        data = data["budget"]
    return BudgetSpec.from_mapping(data)


def load_prices(source, *, overrides: Optional[Mapping] = None) -> PriceTable:
    """Load a price table from a path or a mapping, then apply overrides."""
    if source is None:
        table = PriceTable()
    elif isinstance(source, PriceTable):
        table = source
    elif isinstance(source, Mapping):
        table = PriceTable.from_mapping(source)
    else:
        data = load_mapping(source)
        if "prices" in data and "rules" not in data:
            data = data["prices"]
        table = PriceTable.from_mapping(data)
    if overrides:
        table = table.with_overrides(overrides)
    return table


def parse_price_override(text: str) -> Tuple[str, str]:
    """Parse a ``key=amount`` override as accepted on the command line."""
    if "=" not in text:
        raise ConfigError(
            f"price override {text!r} must look like key=amount, e.g. "
            "image.render=0.04"
        )
    key, _, amount = text.partition("=")
    key = key.strip()
    amount = amount.strip()
    if not key or not amount:
        raise ConfigError(f"price override {text!r} needs both a key and an amount")
    return key, amount


def build_guard(
    budget,
    prices=None,
    ledger_path=None,
    *,
    overrides: Optional[Mapping] = None,
    clock: Optional[Clock] = None,
    lease: timedelta = DEFAULT_LEASE,
    fsync: bool = True,
    tolerate_corrupt: bool = False,
) -> SpendGuard:
    """Assemble a guard from declarations.

    With no ``ledger_path`` the guard keeps its ledger in memory, which is
    right for a simulation and wrong for anything that actually spends money.
    """
    spec = load_budget(budget)
    table = load_prices(prices, overrides=overrides)
    store: Ledger
    if ledger_path is not None and not str(ledger_path).strip():
        # An empty string is what `export X=$UNSET` produces. It is not "no
        # ledger": it reaches Path("") -> Path("."), and the first append dies
        # with a raw OSError on a directory.
        raise ConfigError(
            "the ledger path is empty; pass a file path, or pass None to keep "
            "the ledger in memory"
        )
    if ledger_path is None:
        store = MemoryLedger()
    else:
        store = FileLedger(
            ledger_path, fsync=fsync, tolerate_corrupt=tolerate_corrupt
        )
    return SpendGuard(
        spec, table, store, clock=clock, default_lease=lease
    )


def guard_from_env(
    environ: Optional[Mapping[str, str]] = None,
    *,
    clock: Optional[Clock] = None,
) -> SpendGuard:
    """Build a guard from ``SPEND_GUARD_*`` environment variables.

    ``SPEND_GUARD_LEDGER`` is optional but almost never optional in practice.
    With no ledger file the guard keeps its history in memory, so every process
    starts from a clean budget and a monthly ceiling is granted again on every
    run — a ceiling that can never bind. Leaving it unset is therefore a
    :class:`RuntimeWarning`, not a silent default, and setting it to an empty
    string (the usual result of exporting an unset variable) is an error.
    """
    env = os.environ if environ is None else environ
    budget_path = env.get(ENV_BUDGET)
    if not budget_path:
        raise ConfigError(
            f"{ENV_BUDGET} is not set; point it at a budget declaration file"
        )
    ledger_path = env.get(ENV_LEDGER)
    if ledger_path is not None and not ledger_path.strip():
        raise ConfigError(
            f"{ENV_LEDGER} is set to an empty value; point it at a ledger file "
            "or unset it entirely"
        )
    lease = DEFAULT_LEASE
    raw_lease = env.get(ENV_LEASE_SECONDS)
    if raw_lease:
        try:
            seconds = float(raw_lease)
        except ValueError:
            raise ConfigError(
                f"{ENV_LEASE_SECONDS}={raw_lease!r} is not a number of seconds"
            ) from None
        if seconds <= 0:
            raise ConfigError(f"{ENV_LEASE_SECONDS} must be positive")
        lease = timedelta(seconds=seconds)
    if ledger_path is None:
        warnings.warn(
            f"{ENV_LEDGER} is not set, so this guard keeps its ledger in "
            "memory: nothing survives the process, and a ceiling that is "
            "granted again on every run cannot refuse anything. Set "
            f"{ENV_LEDGER} for anything that really spends money.",
            RuntimeWarning,
            stacklevel=2,
        )
    return build_guard(
        budget_path,
        env.get(ENV_PRICES),
        ledger_path,
        clock=clock,
        lease=lease,
    )
