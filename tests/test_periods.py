"""Period boundaries.

"Spend this month" is only a number if every instant belongs to exactly one
month. Gaps let spend disappear from both sides of a boundary; overlaps let it
be counted twice. Both make the ceiling wrong on the days it matters most.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from spend_guard import Period, PeriodKind
from spend_guard.errors import PeriodError


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_monthly_window_covers_the_calendar_month():
    """The default period is a calendar month, half-open at the far end."""
    window = Period.monthly().window(utc(2026, 3, 10, 12))
    assert window.start == utc(2026, 3, 1)
    assert window.end == utc(2026, 4, 1)
    assert window.key == "2026-03"


def test_month_end_instant_belongs_to_that_month_not_the_next():
    """The last microsecond of the month must still be inside it.

    A half-open interval that got this wrong would move end-of-month spend into
    the next budget, which is exactly when a ceiling is being tested.
    """
    window = Period.monthly().window(utc(2026, 3, 31, 23, 59, 59))
    assert window.key == "2026-03"


def test_month_boundary_instant_belongs_to_the_new_month():
    """Midnight on the 1st starts the new period; the interval is [start, end)."""
    window = Period.monthly().window(utc(2026, 4, 1, 0, 0, 0))
    assert window.key == "2026-04"


def test_anchored_month_follows_a_billing_cycle_not_the_calendar():
    """A provider whose cycle starts on the 5th needs a budget that agrees.

    Aligning the budget to the calendar when billing runs 5th-to-5th means the
    guard's "remaining" and the invoice never match.
    """
    period = Period.monthly(anchor_day=5)
    window = period.window(utc(2026, 3, 4, 23))
    assert window.start == utc(2026, 2, 5)
    assert window.end == utc(2026, 3, 5)


def test_anchor_day_31_clamps_into_february_without_leaving_a_gap():
    """An anchor past the end of a short month must still tile time.

    January 31st to February 28th, then February 28th onward: consecutive
    windows touch exactly, so no day falls outside every period.
    """
    period = Period.monthly(anchor_day=31)
    january = period.window(utc(2026, 2, 10))
    assert january.start == utc(2026, 1, 31)
    assert january.end == utc(2026, 2, 28)
    february = period.next_window(january)
    assert february.start == january.end
    assert february.end == utc(2026, 3, 31)


def test_anchor_day_31_lands_on_the_29th_in_a_leap_february():
    """The clamp reads the real length of the month, including leap years."""
    period = Period.monthly(anchor_day=31)
    window = period.window(utc(2028, 3, 1))
    assert window.start == utc(2028, 2, 29)


def test_consecutive_windows_tile_a_whole_year_with_no_gap_or_overlap():
    """The tiling property, checked end to end rather than at one boundary."""
    period = Period.monthly(anchor_day=15)
    window = period.window(utc(2026, 1, 20))
    for _ in range(24):
        following = period.next_window(window)
        assert following.start == window.end
        assert period.window(window.end).key == following.key
        window = following


def test_previous_window_is_the_inverse_of_next():
    """Rollover walks backwards; the walk must land where it started."""
    period = Period.monthly()
    window = period.window(utc(2026, 6, 15))
    assert period.previous_window(period.next_window(window)).key == window.key


def test_daily_period_keys_are_iso_dates():
    """A daily circuit breaker needs a key an operator can read in a log."""
    window = Period.daily().window(utc(2026, 3, 10, 23, 59))
    assert window.key == "2026-03-10"
    assert window.duration == timedelta(days=1)


def test_weekly_period_starts_on_monday_by_default():
    """Reporting weeks start on Monday unless the operator says otherwise."""
    window = Period.weekly().window(utc(2026, 3, 12))  # a Thursday
    assert window.start.weekday() == 0
    assert window.start == utc(2026, 3, 9)


def test_weekly_period_honours_a_different_start_day():
    """Some operations run Sunday to Sunday; the key must follow the real start."""
    period = Period.weekly(week_starts_on=6)  # Sunday
    window = period.window(utc(2026, 3, 12))
    assert window.start.weekday() == 6
    assert window.key.startswith("W")


def test_quarterly_period_groups_three_months():
    """A quarterly commitment is one budget, not three that each reset."""
    window = Period(kind=PeriodKind.QUARTERLY).window(utc(2026, 8, 4))
    assert window.start == utc(2026, 7, 1)
    assert window.end == utc(2026, 10, 1)
    assert window.key == "2026-Q3"


def test_yearly_period_covers_the_calendar_year():
    """An annual cap is the simplest real budget and must not need a workaround."""
    window = Period(kind=PeriodKind.YEARLY).window(utc(2026, 8, 4))
    assert window.start == utc(2026, 1, 1)
    assert window.end == utc(2027, 1, 1)
    assert window.key == "2026"


def test_fixed_days_period_tiles_from_an_epoch():
    """A 14-day sprint budget does not align to any calendar unit."""
    period = Period.fixed_days(14, epoch=date(2026, 1, 1))
    window = period.window(utc(2026, 1, 20))
    assert window.start == utc(2026, 1, 15)
    assert window.end == utc(2026, 1, 29)


def test_fixed_days_period_handles_instants_before_the_epoch():
    """Floor division must round toward the past, not toward zero.

    Otherwise a date before the epoch lands in the wrong window and its spend
    is attributed to a period that has not started.
    """
    period = Period.fixed_days(10, epoch=date(2026, 1, 1))
    window = period.window(utc(2025, 12, 28))
    assert window.start <= utc(2025, 12, 28) < window.end


def test_fixed_days_requires_a_length_and_an_epoch():
    """An underspecified rolling period cannot produce windows; fail at construction."""
    with pytest.raises(PeriodError):
        Period(kind=PeriodKind.FIXED_DAYS)
    with pytest.raises(PeriodError):
        Period(kind=PeriodKind.FIXED_DAYS, length_days=7)


def test_length_days_on_a_calendar_period_is_rejected():
    """A monthly period with length_days set means the author expected something else."""
    with pytest.raises(PeriodError):
        Period(kind=PeriodKind.MONTHLY, length_days=30)


def test_invalid_anchor_day_is_rejected_at_construction():
    """A bad declaration fails at load, not on the first spend of the month."""
    with pytest.raises(PeriodError):
        Period.monthly(anchor_day=0)
    with pytest.raises(PeriodError):
        Period.monthly(anchor_day=32)


def test_unknown_period_kind_lists_the_valid_ones():
    """A typo in a config file should tell the operator what to write instead."""
    with pytest.raises(PeriodError) as excinfo:
        Period.from_mapping({"kind": "fortnightly"})
    assert "monthly" in str(excinfo.value)


def test_the_bare_string_form_of_a_period_fails_the_same_way():
    """``"period": "fortnightly"`` is the shorter spelling and a likelier typo.

    It short-circuited before the friendly handler and escaped as a raw
    ``ValueError`` from the enum, which the CLI does not catch: the operator
    got a traceback instead of the one-line message every other bad
    declaration produces.
    """
    with pytest.raises(PeriodError) as excinfo:
        Period.from_mapping("fortnightly")
    assert "monthly" in str(excinfo.value)


def test_a_period_constructed_with_a_bad_kind_also_raises_period_error():
    """Same contract when a spec is built in code rather than loaded."""
    with pytest.raises(PeriodError):
        Period(kind="fortnightly")


def test_contains_is_half_open():
    """Membership must match the arithmetic used to sum spend."""
    window = Period.monthly().window(utc(2026, 3, 10))
    assert window.contains(window.start)
    assert not window.contains(window.end)


def test_elapsed_fraction_is_clamped_to_the_window():
    """A clock that has run past the period end must not report 140% elapsed.

    Projection divides by this; an unclamped value produces a burn rate that
    looks safe precisely because the period already ended.
    """
    window = Period.monthly().window(utc(2026, 3, 10))
    assert window.elapsed_fraction(utc(2026, 2, 1)) == Decimal(0)
    assert window.elapsed_fraction(utc(2026, 5, 1)) == Decimal(1)


def test_remaining_never_goes_negative():
    """"Time left" is used for messaging; a negative would read as nonsense."""
    window = Period.monthly().window(utc(2026, 3, 10))
    assert window.remaining(utc(2026, 5, 1)) == timedelta(0)


def test_naive_datetimes_are_treated_as_utc():
    """Callers pass naive datetimes constantly; guessing UTC beats crashing."""
    window = Period.monthly().window(datetime(2026, 3, 10, 12))
    assert window.key == "2026-03"


def test_period_round_trips_through_a_mapping():
    """A period written to a config file must reload as the same rule."""
    period = Period.monthly(anchor_day=7)
    assert Period.from_mapping(period.to_mapping()) == period


def test_weekday_names_are_accepted_in_a_declaration():
    """Config files are written by people, who write "sunday", not 6."""
    period = Period.from_mapping({"kind": "weekly", "week_starts_on": "sunday"})
    assert period.week_starts_on == 6


def test_windows_between_yields_every_overlapping_window():
    """Reporting across a range must not skip a period with no activity."""
    period = Period.monthly()
    got = [w.key for w in period.windows_between(utc(2026, 1, 15), utc(2026, 4, 2))]
    assert got == ["2026-01", "2026-02", "2026-03", "2026-04"]
