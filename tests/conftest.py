"""Shared fixtures.

Every guard built here uses a :class:`FixedClock` and a counter-based id
factory, so no test depends on wall time, on sleeping, or on a random id. A
suite that needs real time to pass cannot pin lease expiry behaviour, which is
half of what this package promises.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from spend_guard import (
    BudgetSpec,
    FixedClock,
    MemoryLedger,
    PriceTable,
    SpendGuard,
)

#: A Tuesday in the middle of a month, far from any boundary, so a test that
#: cares about boundaries has to say so.
T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def budget_mapping():
    return {
        "currency": "USD",
        "period": {"kind": "monthly"},
        "ceiling": "100.00",
        "warn_threshold": "0.80",
        "categories": {
            "text": {"ceiling": "40.00"},
            "images": {"ceiling": "30.00"},
        },
    }


@pytest.fixture
def budget(budget_mapping):
    return BudgetSpec.from_mapping(budget_mapping)


@pytest.fixture
def prices_mapping():
    return {
        "on_missing": "block",
        "rules": {
            "text.generate": {
                "unit": "token",
                "per": 1000000,
                "unit_price": "3.00",
            },
            "image.render": {"unit": "image", "unit_price": "0.04"},
            "video.render": {"unit": "second", "unit_price": "0.50"},
            "free.healthcheck": {"unit": "call", "unit_price": "0"},
        },
    }


@pytest.fixture
def prices(prices_mapping):
    return PriceTable.from_mapping(prices_mapping)


@pytest.fixture
def ids():
    counter = itertools.count(1)
    return lambda: "r%03d" % next(counter)


@pytest.fixture
def ledger():
    return MemoryLedger()


@pytest.fixture
def guard(budget, prices, ledger, clock, ids):
    return SpendGuard(budget, prices, ledger, clock=clock, id_factory=ids)
