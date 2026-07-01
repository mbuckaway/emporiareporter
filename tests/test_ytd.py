# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.ytd - COMPLETE test suite written FIRST."""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.cost import bill_estimate, load_tariff
from emporia_hydro.models import BALANCE_CHANNEL, Channel, IntervalUsage
from emporia_hydro.ytd import DeviceYtd, ytd_summary

REPO_ROOT = Path(__file__).resolve().parents[1]


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build a timezone-aware UTC datetime for a given wall time."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _channel(device_gid: int, channel_num: str, name: str, role: str) -> Channel:
    """Build a Channel fixture with a fixed device_name for test brevity."""
    return Channel(
        device_gid=device_gid, device_name="Home", channel_num=channel_num, name=name, role=role
    )


def _usage(ts: datetime, device_gid: int, channel: str, kwh: float) -> IntervalUsage:
    """Build a single hourly IntervalUsage fixture."""
    return IntervalUsage(ts=ts, scale="1H", device_gid=device_gid, channel=channel, kwh=kwh)


def _noon_daily_usages(
    year: int, month: int, day: int, device_gid: int, channel: str, kwh: float, count: int
) -> list[IntervalUsage]:
    """Build ``count`` noon-local mains IntervalUsage records, one per day.

    Noon local (America/Toronto, EST/EDT) is always mid-day UTC, so every
    interval lands on its intended local calendar date regardless of DST.
    """
    return [
        _usage(_utc(year, month, day + offset, 16), device_gid, channel, kwh)
        for offset in range(count)
    ]


@pytest.fixture
def tariff():
    """Load the real repo config/tariff.json for use as test ground truth."""
    return load_tariff(REPO_ROOT / "config")


@pytest.fixture
def mains_channel() -> list[Channel]:
    """A single mains-role channel used by most ytd_summary tests."""
    return [_channel(1, "1", "Mains", "mains")]


@pytest.fixture
def multi_role_channels() -> list[Channel]:
    """Mains + branch + aux channels used by per-device tests."""
    return [
        _channel(1, "1", "Mains", "mains"),
        _channel(1, "10", "Dryer", "branch"),
        _channel(2, "1", "EV Charger", "aux"),
    ]


def _month_full_total(kwh: float, cost: float, on: date, tariff) -> float:
    """Compute the expected full-bill total for one month via bill_estimate."""
    return bill_estimate(
        whole_home_kwh=kwh, commodity_cost=cost, on=on, tariff=tariff, months=1.0
    ).total


# ---------------------------------------------------------------------------
# ytd_summary - happy path: single day, single month
# ---------------------------------------------------------------------------


def test_ytd_summary_singleday_january1_returnsonemonthrollup(config, tariff, mains_channel):
    usages = _noon_daily_usages(2026, 1, 1, 1, "1", 10.0, 1)

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 1))

    assert (result.year, result.through, result.plan) == (2026, date(2026, 1, 1), "tou")
    assert len(result.months) == 1
    assert result.months[0].label == "2026-01"


def test_ytd_summary_singleday_january1_wholehomekwhmatchesusage(config, tariff, mains_channel):
    usages = _noon_daily_usages(2026, 1, 1, 1, "1", 10.0, 1)

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 1))

    assert result.whole_home_kwh == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# ytd_summary - month range selection and rollup
# ---------------------------------------------------------------------------


def test_ytd_summary_spanningtwomonths_returnstwomonthrollup(config, tariff, mains_channel):
    jan_usages = _noon_daily_usages(2026, 1, 1, 1, "1", 5.0, 31)
    feb_usages = _noon_daily_usages(2026, 2, 1, 1, "1", 5.0, 10)
    usages = jan_usages + feb_usages

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 2, 10))

    assert [m.label for m in result.months] == ["2026-01", "2026-02"]


def test_ytd_summary_spanningtwomonths_monthlykwhmatchesperiod(config, tariff, mains_channel):
    jan_usages = _noon_daily_usages(2026, 1, 1, 1, "1", 5.0, 31)
    feb_usages = _noon_daily_usages(2026, 2, 1, 1, "1", 5.0, 10)
    usages = jan_usages + feb_usages

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 2, 10))

    jan_month, feb_month = result.months
    assert (jan_month.kwh, feb_month.kwh) == pytest.approx((155.0, 50.0))


def test_ytd_summary_usagebeforejanuary1_excludedfromrange(config, tariff, mains_channel):
    prior_year_usage = _usage(_utc(2025, 12, 31, 16), 1, "1", 99.0)
    jan_usage = _usage(_utc(2026, 1, 1, 16), 1, "1", 10.0)
    usages = [prior_year_usage, jan_usage]

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 1))

    assert result.whole_home_kwh == pytest.approx(10.0)


def test_ytd_summary_usageafteron_excludedfromrange(config, tariff, mains_channel):
    in_range_usage = _usage(_utc(2026, 1, 1, 16), 1, "1", 10.0)
    future_usage = _usage(_utc(2026, 1, 2, 16), 1, "1", 99.0)
    usages = [in_range_usage, future_usage]

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 1))

    assert result.whole_home_kwh == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# ytd_summary - boundary: on in the last covered month (12 months in rollup)
# ---------------------------------------------------------------------------
#
# NOTE: config/rates.json's TOU price rows for 2026 only run through
# 2026-10-31 (see the effective-dated schedule), so the "full year" boundary
# is exercised through October rather than December to stay within real,
# unmutated config coverage.


def test_ytd_summary_onoctober_returnstenmonthrollup(config, tariff, mains_channel):
    usages = [_usage(_utc(2026, month, 15, 16), 1, "1", 1.0) for month in range(1, 11)]

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 10, 31))

    assert [m.label for m in result.months] == [f"2026-{m:02d}" for m in range(1, 11)]


def test_ytd_summary_onoctober_lastmonthendsonon(config, tariff, mains_channel):
    usages = [_usage(_utc(2026, 10, 15, 16), 1, "1", 1.0)]

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 10, 20))

    assert result.months[-1].month == date(2026, 10, 1)


# ---------------------------------------------------------------------------
# ytd_summary - boundary: empty usages
# ---------------------------------------------------------------------------


def test_ytd_summary_emptyusages_januarymonthstillpresent(config, tariff, mains_channel):
    result = ytd_summary([], mains_channel, config, tariff, on=date(2026, 1, 15))

    assert len(result.months) == 1
    assert result.months[0].kwh == 0.0


def test_ytd_summary_emptyusages_wholehomekwhiszero(config, tariff, mains_channel):
    result = ytd_summary([], mains_channel, config, tariff, on=date(2026, 3, 15))

    assert (result.whole_home_kwh, result.whole_home_commodity) == (0.0, 0.0)


def test_ytd_summary_emptyusages_bydeviceonlyhasbalancezero(config, tariff, mains_channel):
    result = ytd_summary([], mains_channel, config, tariff, on=date(2026, 1, 15))

    assert result.by_device == [DeviceYtd(BALANCE_CHANNEL, BALANCE_CHANNEL, "balance", 0.0, 0.0)]


# ---------------------------------------------------------------------------
# ytd_summary - per-device rollup: mains, branch, aux, balance; sort order
# ---------------------------------------------------------------------------


def test_ytd_summary_multipledevices_includesbalanceandauxentries(
    config, tariff, multi_role_channels
):
    usages = [
        _usage(_utc(2026, 1, 5, 16), 1, "1", 10.0),
        _usage(_utc(2026, 1, 5, 16), 1, "10", 3.0),
        _usage(_utc(2026, 1, 5, 16), 2, "1", 2.0),
    ]

    result = ytd_summary(usages, multi_role_channels, config, tariff, on=date(2026, 1, 5))

    device_nums = {d.channel_num for d in result.by_device}
    assert device_nums == {"1", "10", BALANCE_CHANNEL}


def test_ytd_summary_multipledevices_sortedbycostdescendingthenchannelnum(
    config, tariff, multi_role_channels
):
    usages = [
        _usage(_utc(2026, 1, 5, 16), 1, "1", 10.0),
        _usage(_utc(2026, 1, 5, 16), 1, "10", 3.0),
        _usage(_utc(2026, 1, 5, 16), 2, "1", 2.0),
    ]

    result = ytd_summary(usages, multi_role_channels, config, tariff, on=date(2026, 1, 5))

    costs = [d.cost for d in result.by_device]
    assert costs == sorted(costs, reverse=True)


def test_ytd_summary_tiedcost_sortedbychannelnumascending(config, tariff):
    channels = [
        _channel(1, "1", "Mains", "mains"),
        _channel(1, "20", "Fridge", "branch"),
        _channel(1, "10", "Dryer", "branch"),
    ]
    ts = _utc(2026, 1, 5, 6)  # off-peak hour: equal rate for both branch loads
    usages = [
        _usage(ts, 1, "1", 4.0),
        _usage(ts, 1, "20", 2.0),
        _usage(ts, 1, "10", 2.0),
    ]

    result = ytd_summary(usages, channels, config, tariff, on=date(2026, 1, 5))

    branch_nums = [d.channel_num for d in result.by_device if d.role == "branch"]
    assert branch_nums == ["10", "20"]


# ---------------------------------------------------------------------------
# ytd_summary - full_total rollup correctness
# ---------------------------------------------------------------------------


def test_ytd_summary_fulltotal_equalssummonthlyfulltotal(config, tariff, mains_channel):
    jan_usages = _noon_daily_usages(2026, 1, 1, 1, "1", 5.0, 31)
    feb_usages = _noon_daily_usages(2026, 2, 1, 1, "1", 5.0, 10)
    usages = jan_usages + feb_usages

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 2, 10))

    assert result.full_total == pytest.approx(sum(m.full_total for m in result.months))


def test_ytd_summary_monthlyfulltotal_matchesindependentbillestimate(
    config, tariff, mains_channel
):
    usages = _noon_daily_usages(2026, 1, 1, 1, "1", 5.0, 31)

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 31))

    month = result.months[0]
    expected = _month_full_total(month.kwh, month.commodity_cost, month.month, tariff)
    assert month.full_total == pytest.approx(expected)


# ---------------------------------------------------------------------------
# ytd_summary - plan selection
# ---------------------------------------------------------------------------


def test_ytd_summary_uloplan_usesuloratesforcommodity(config, tariff, mains_channel):
    usages = [_usage(_utc(2026, 1, 5, 8), 1, "1", 2.0)]  # UTC 08:00 -> local 03:00, overnight

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 5), plan="ulo")

    assert (result.plan, result.whole_home_commodity) == ("ulo", pytest.approx(2.0 * 0.039))


def test_ytd_summary_unknownplan_raisesvalueerror(config, tariff, mains_channel):
    with pytest.raises(ValueError, match=re.escape("Unknown pricing plan: 'bogus'")):
        ytd_summary([], mains_channel, config, tariff, on=date(2026, 1, 5), plan="bogus")


# ---------------------------------------------------------------------------
# Hypothesis property tests - pure rollup invariant (T-7)
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(daily_kwh=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
def test_ytd_summary_singlemonthusage_wholehomekwhequalssummonthlykwh(
    config, tariff, mains_channel, daily_kwh
):
    usages = _noon_daily_usages(2026, 1, 1, 1, "1", daily_kwh, 5)

    result = ytd_summary(usages, mains_channel, config, tariff, on=date(2026, 1, 5))

    assert result.whole_home_kwh == pytest.approx(sum(m.kwh for m in result.months))
