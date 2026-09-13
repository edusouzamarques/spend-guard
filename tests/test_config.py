"""Loading declarations from files and the environment.

The core has no runtime dependencies, so JSON must work everywhere and the
optional formats must fail with a message that names the package to install
rather than an ImportError from inside a loader.
"""

from __future__ import annotations

import json
import sys

import pytest

from spend_guard import (
    BudgetSpec,
    MemoryLedger,
    Money,
    PriceTable,
    build_guard,
    guard_from_env,
)
from spend_guard.config import (
    ENV_BUDGET,
    ENV_LEASE_SECONDS,
    ENV_LEDGER,
    ENV_PRICES,
    load_budget,
    load_mapping,
    load_prices,
    parse_price_override,
)
from spend_guard.errors import ConfigError, MissingDependencyError

BUDGET = {
    "ceiling": "100.00",
    "categories": {"images": {"ceiling": "30.00"}},
}
PRICES = {"rules": {"image.render": {"unit_price": "0.04"}}}


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_budget_loads_from_json(tmp_path):
    """JSON is the format that works with no extras installed anywhere."""
    spec = load_budget(write(tmp_path, "budget.json", BUDGET))
    assert spec.ceiling == Money.parse("100.00")


def test_a_price_table_loads_from_json(tmp_path):
    """Same guarantee for the other half of the declaration."""
    table = load_prices(write(tmp_path, "prices.json", PRICES))
    assert table.quote("image.render", 2).require() == Money.parse("0.08")


def test_a_combined_file_can_nest_both_declarations(tmp_path):
    """One file is easier to version than two, and people will try it."""
    path = write(tmp_path, "config.json", {"budget": BUDGET, "prices": PRICES})
    assert load_budget(path).ceiling == Money.parse("100.00")
    assert load_prices(path).knows("image.render")


def test_a_missing_file_says_so_plainly(tmp_path):
    """A typo'd path must not fall back to an empty, permissive budget."""
    with pytest.raises(ConfigError) as excinfo:
        load_mapping(tmp_path / "absent.json")
    assert "no such file" in str(excinfo.value)


def test_invalid_json_names_the_file(tmp_path):
    """The operator needs to know which of several config files is broken."""
    path = tmp_path / "budget.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_mapping(path)
    assert "budget.json" in str(excinfo.value)


def test_an_unsupported_extension_is_rejected(tmp_path):
    """Guessing the format of a .ini would produce a silently wrong budget."""
    path = tmp_path / "budget.ini"
    path.write_text("ceiling = 100", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_mapping(path)


def test_a_top_level_list_is_rejected(tmp_path):
    """A YAML or JSON file that is a list is a different document than expected."""
    path = tmp_path / "budget.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_mapping(path)


def test_toml_loads_when_a_parser_is_available(tmp_path):
    """TOML support must actually work where the parser exists.

    ``tomllib`` is standard from 3.11; older versions need the ``toml`` extra,
    so this test skips rather than importing something that is not there.
    """
    if sys.version_info < (3, 11):
        pytest.importorskip("tomli")
    path = tmp_path / "budget.toml"
    path.write_text('ceiling = "100.00"\n', encoding="utf-8")
    assert load_budget(path).ceiling == Money.parse("100.00")


def test_toml_without_a_parser_names_the_extra_to_install(tmp_path, monkeypatch):
    """Degrading gracefully means telling the operator the exact fix."""
    if sys.version_info >= (3, 11):
        pytest.skip("tomllib is in the standard library from 3.11")
    monkeypatch.setitem(sys.modules, "tomli", None)
    path = tmp_path / "budget.toml"
    path.write_text('ceiling = "100.00"\n', encoding="utf-8")
    with pytest.raises(MissingDependencyError) as excinfo:
        load_budget(path)
    assert "spend-guard[toml]" in str(excinfo.value)


def test_yaml_loads_when_pyyaml_is_installed(tmp_path):
    """YAML is the format most operations config is already written in."""
    pytest.importorskip("yaml")
    path = tmp_path / "budget.yaml"
    path.write_text('ceiling: "100.00"\n', encoding="utf-8")
    assert load_budget(path).ceiling == Money.parse("100.00")


def test_yaml_without_pyyaml_names_the_extra_to_install(tmp_path, monkeypatch):
    """The package must not hard-depend on PyYAML to explain that it needs it."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    path = tmp_path / "budget.yaml"
    path.write_text('ceiling: "100.00"\n', encoding="utf-8")
    with pytest.raises(MissingDependencyError) as excinfo:
        load_budget(path)
    assert "spend-guard[yaml]" in str(excinfo.value)


def test_build_guard_defaults_to_an_in_memory_ledger(tmp_path):
    """Convenient for simulation, and explicitly wrong for anything that spends.

    Making it the default for a path-less call keeps a test from accidentally
    writing to a real ledger.
    """
    guard = build_guard(BUDGET, PRICES)
    guard.reserve("image.render", 1)
    assert guard.snapshot().reserved == Money.parse("0.04")


def test_build_guard_writes_to_a_file_ledger_when_given_a_path(tmp_path):
    """Anything that really spends money needs history that survives the process."""
    path = tmp_path / "spend.ndjson"
    guard = build_guard(BUDGET, PRICES, path)
    guard.reserve("image.render", 1)
    assert path.exists()
    assert build_guard(BUDGET, PRICES, path).snapshot().reserved == Money.parse("0.04")


def test_price_overrides_apply_on_top_of_the_loaded_table():
    """An operator with a negotiated rate should not have to fork the table."""
    guard = build_guard(BUDGET, PRICES, overrides={"image.render": "0.01"})
    assert guard.quote("image.render", 10).require() == Money.parse("0.10")


def test_parse_price_override_requires_both_halves():
    """A malformed --price flag must not be half-applied."""
    assert parse_price_override("image.render=0.04") == ("image.render", "0.04")
    with pytest.raises(ConfigError):
        parse_price_override("image.render")
    with pytest.raises(ConfigError):
        parse_price_override("=0.04")


def test_guard_from_env_reads_the_documented_variables(tmp_path):
    """The environment is the integration seam; it has to work end to end."""
    env = {
        ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET)),
        ENV_PRICES: str(write(tmp_path, "prices.json", PRICES)),
        ENV_LEDGER: str(tmp_path / "spend.ndjson"),
    }
    guard = guard_from_env(env)
    assert guard.budget.ceiling == Money.parse("100.00")
    assert guard.quote("image.render", 1).require() == Money.parse("0.04")


def test_an_empty_ledger_variable_is_an_error_not_a_directory(tmp_path):
    """``export SPEND_GUARD_LEDGER=$UNSET`` produces "", which is not "no ledger".

    An empty string is not None, so it reached ``Path("")`` -> ``Path(".")``
    and the first read or write died with a raw OSError about a directory,
    which is neither a clean config error nor the in-memory fallback.
    """
    env = {
        ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET)),
        ENV_LEDGER: "",
    }
    with pytest.raises(ConfigError) as excinfo:
        guard_from_env(env)
    assert ENV_LEDGER in str(excinfo.value)


def test_an_unset_ledger_variable_warns_instead_of_silently_forgetting(tmp_path):
    """The in-memory fallback grants the whole ceiling again on every run.

    That is right for a simulation and catastrophic for a process that spends,
    and ``guard_from_env`` is the integration seam where the distinction is
    easiest to miss. It must not be silent.
    """
    env = {ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET))}
    with pytest.warns(RuntimeWarning, match=ENV_LEDGER):
        guard = guard_from_env(env)
    assert isinstance(guard.ledger, MemoryLedger)


def test_build_guard_refuses_an_empty_ledger_path():
    """Same trap one layer down, for a caller assembling a guard in code."""
    with pytest.raises(ConfigError):
        build_guard(BUDGET, PRICES, "")


def test_guard_from_env_without_a_budget_refuses_to_guess():
    """Defaulting to an unlimited budget would be the worst possible fallback."""
    with pytest.raises(ConfigError) as excinfo:
        guard_from_env({})
    assert ENV_BUDGET in str(excinfo.value)


def test_the_lease_can_be_set_from_the_environment(tmp_path):
    """Long-running jobs need a longer hold without a code change."""
    env = {
        ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET)),
        ENV_LEDGER: str(tmp_path / "spend.ndjson"),
        ENV_LEASE_SECONDS: "3600",
    }
    assert guard_from_env(env).default_lease.total_seconds() == 3600


def test_a_nonsense_lease_in_the_environment_is_rejected(tmp_path):
    """A typo'd variable must not silently fall back to the default."""
    env = {
        ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET)),
        ENV_LEASE_SECONDS: "soon",
    }
    with pytest.raises(ConfigError):
        guard_from_env(env)


def test_a_zero_lease_in_the_environment_is_rejected(tmp_path):
    """Zero would release every hold instantly and disable the protection."""
    env = {
        ENV_BUDGET: str(write(tmp_path, "budget.json", BUDGET)),
        ENV_LEASE_SECONDS: "0",
    }
    with pytest.raises(ConfigError):
        guard_from_env(env)


def test_already_built_objects_pass_through_unchanged():
    """Callers building specs in code should not be forced through serialisation."""
    spec = BudgetSpec.from_mapping(BUDGET)
    table = PriceTable.from_mapping(PRICES)
    assert load_budget(spec) is spec
    assert load_prices(table) is table


def test_the_shipped_example_declarations_are_valid():
    """The files in examples/ are the first thing a reader tries; they must load."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "examples"
    spec = load_budget(root / "budget.json")
    table = load_prices(root / "prices.json")
    assert spec.ceiling == Money.parse("1200.00")
    assert table.quote("text.generate", 1000000).require() == Money.parse("3.00")
