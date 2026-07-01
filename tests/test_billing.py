# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.billing - COMPLETE test suite written FIRST."""

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.billing import (
    BillingError,
    BillingPeriod,
    Settings,
    billing_periods,
    current_period,
    load_settings,
    predict_bill,
)
from emporia_hydro.cost import bill_estimate, load_tariff
from emporia_hydro.models import Channel, IntervalUsage

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_SETTINGS_DICT = {
    "timezone": "America/Toronto",
    "current_plan": "tou",
    "billing_cycle": {"mode": "calendar_month"},
    "server": {"host": "127.0.0.1", "port": 8765},
    "output": {"reports_dir": "reports", "data_dir": "data"},
}


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


def _daily_usages(
    daily_kwh: list[float], year: int, month: int, start_day: int
) -> list[IntervalUsage]:
    """Build one noon-local mains IntervalUsage per day, starting at ``start_day``.

    Noon local (UTC-4 in July's EDT) is always mid-day UTC, so every interval
    lands on its intended local calendar date regardless of DST arithmetic.
    """
    return [
        _usage(_utc(year, month, start_day + offset, 16), 1, "1", kwh)
        for offset, kwh in enumerate(daily_kwh)
    ]


def _settings(billing_cycle: dict[str, Any]) -> Settings:
    """Build a Settings fixture with a caller-controlled billing_cycle mapping."""
    return Settings(
        timezone="America/Toronto",
        current_plan="tou",
        billing_cycle=billing_cycle,
        server={"host": "127.0.0.1", "port": 8765},
        output={"reports_dir": "reports", "data_dir": "data"},
    )


@pytest.fixture
def tariff():
    """Load the real repo config/tariff.json for use as test ground truth."""
    return load_tariff(REPO_ROOT / "config")


@pytest.fixture
def mains_channel() -> list[Channel]:
    """A single mains-role channel used by most predict_bill tests."""
    return [_channel(1, "1", "Mains", "mains")]


# ---------------------------------------------------------------------------
# load_settings
# ---------------------------------------------------------------------------


def test_load_settings_realconfig_returnsparsedfields():
    result = load_settings(REPO_ROOT / "config")

    assert (result.timezone, result.current_plan, result.billing_cycle) == (
        "America/Toronto",
        "tou",
        {"mode": "calendar_month"},
    )
    assert result.server == {"host": "127.0.0.1", "port": 8765}
    assert result.output == {"reports_dir": "reports", "data_dir": "data"}


def test_load_settings_missingfile_raisesbillingerror(tmp_path):
    expected_match = f"Settings config file not found: {tmp_path / 'settings.json'}"

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        load_settings(tmp_path)


def test_load_settings_invalidjson_raisesbillingerror(tmp_path):
    (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(BillingError, match=re.escape("Invalid JSON in settings config file")):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    ("missing_key", "expected_match"),
    [
        ("timezone", "Missing required key 'timezone' in settings.json"),
        ("current_plan", "Missing required key 'current_plan' in settings.json"),
        ("billing_cycle", "Missing required key 'billing_cycle' in settings.json"),
        ("server", "Missing required key 'server' in settings.json"),
        ("output", "Missing required key 'output' in settings.json"),
    ],
    ids=["timezone", "current_plan", "billing_cycle", "server", "output"],
)
def test_load_settings_missingkey_raisesbillingerror(tmp_path, missing_key, expected_match):
    data = dict(VALID_SETTINGS_DICT)
    del data[missing_key]
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        load_settings(tmp_path)


# ---------------------------------------------------------------------------
# billing_periods - calendar_month mode
# ---------------------------------------------------------------------------


def test_billing_periods_calendarmonth_singlemonthrange_returnsonemonthperiod():
    billing_settings = _settings({"mode": "calendar_month"})

    periods = billing_periods(date(2026, 7, 15), date(2026, 7, 15), billing_settings)

    assert periods == [BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")]


def test_billing_periods_calendarmonth_spanningtwomonths_returnstwomonthperiods():
    billing_settings = _settings({"mode": "calendar_month"})

    periods = billing_periods(date(2026, 6, 20), date(2026, 7, 5), billing_settings)

    assert periods == [
        BillingPeriod(date(2026, 6, 1), date(2026, 6, 30), "2026-06"),
        BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07"),
    ]


def test_billing_periods_calendarmonth_yearboundary_returnscorrectlabels():
    billing_settings = _settings({"mode": "calendar_month"})

    periods = billing_periods(date(2025, 12, 15), date(2026, 1, 10), billing_settings)

    assert periods == [
        BillingPeriod(date(2025, 12, 1), date(2025, 12, 31), "2025-12"),
        BillingPeriod(date(2026, 1, 1), date(2026, 1, 31), "2026-01"),
    ]


def test_billing_periods_defaultmode_missingmodekey_usescalendarmonth():
    billing_settings = _settings({})

    periods = billing_periods(date(2026, 7, 15), date(2026, 7, 15), billing_settings)

    assert periods == [BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")]


# ---------------------------------------------------------------------------
# billing_periods - anchor_day mode
# ---------------------------------------------------------------------------


def test_billing_periods_anchorday_dayoneboundary_matchescalendarmonthperiod():
    billing_settings = _settings({"mode": "anchor_day", "day": 1})

    periods = billing_periods(date(2026, 7, 15), date(2026, 7, 15), billing_settings)

    assert periods == [BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07-01")]


def test_billing_periods_anchorday_daytwentyeightboundary_returnscorrectperiod():
    billing_settings = _settings({"mode": "anchor_day", "day": 28})

    periods = billing_periods(date(2026, 2, 20), date(2026, 2, 20), billing_settings)

    assert periods == [BillingPeriod(date(2026, 1, 28), date(2026, 2, 27), "2026-01-28")]


def test_billing_periods_anchorday_spanningtwoperiods_returnstwoperiods():
    billing_settings = _settings({"mode": "anchor_day", "day": 10})

    periods = billing_periods(date(2026, 7, 5), date(2026, 7, 15), billing_settings)

    assert periods == [
        BillingPeriod(date(2026, 6, 10), date(2026, 7, 9), "2026-06-10"),
        BillingPeriod(date(2026, 7, 10), date(2026, 8, 9), "2026-07-10"),
    ]


def test_billing_periods_anchorday_yearboundary_returnscorrectperiod():
    billing_settings = _settings({"mode": "anchor_day", "day": 20})

    periods = billing_periods(date(2025, 12, 25), date(2025, 12, 25), billing_settings)

    assert periods == [BillingPeriod(date(2025, 12, 20), date(2026, 1, 19), "2025-12-20")]


def test_billing_periods_anchorday_missingday_raisesbillingerror():
    billing_settings = _settings({"mode": "anchor_day"})
    expected_match = (
        "billing_cycle mode 'anchor_day' requires 'day' to be an integer 1-28, got None"
    )

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 7, 1), date(2026, 7, 31), billing_settings)


@pytest.mark.parametrize("day", [0, 29], ids=["day_min_minus_one", "day_max_plus_one"])
def test_billing_periods_anchorday_dayoutofrange_raisesbillingerror(day):
    billing_settings = _settings({"mode": "anchor_day", "day": day})
    expected_match = (
        f"billing_cycle mode 'anchor_day' requires 'day' to be an integer 1-28, got {day!r}"
    )

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 7, 1), date(2026, 7, 31), billing_settings)


def test_billing_periods_anchorday_nonintday_raisesbillingerror():
    billing_settings = _settings({"mode": "anchor_day", "day": "15"})
    expected_match = (
        "billing_cycle mode 'anchor_day' requires 'day' to be an integer 1-28, got '15'"
    )

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 7, 1), date(2026, 7, 31), billing_settings)


# ---------------------------------------------------------------------------
# billing_periods - explicit mode
# ---------------------------------------------------------------------------


def test_billing_periods_explicit_overlappingrange_returnsonlyoverlappingperiods():
    billing_settings = _settings(
        {
            "mode": "explicit",
            "periods": [
                ["2026-01-01", "2026-01-31"],
                ["2026-02-01", "2026-02-28"],
                ["2026-03-01", "2026-03-31"],
            ],
        }
    )

    periods = billing_periods(date(2026, 1, 15), date(2026, 2, 10), billing_settings)

    assert periods == [
        BillingPeriod(date(2026, 1, 1), date(2026, 1, 31), "2026-01-01"),
        BillingPeriod(date(2026, 2, 1), date(2026, 2, 28), "2026-02-01"),
    ]


def test_billing_periods_explicit_nonoverlappingrange_returnsemptylist():
    billing_settings = _settings({"mode": "explicit", "periods": [["2026-01-01", "2026-01-31"]]})

    periods = billing_periods(date(2026, 3, 1), date(2026, 3, 31), billing_settings)

    assert periods == []


def test_billing_periods_explicit_emptyperiodslist_raisesbillingerror():
    billing_settings = _settings({"mode": "explicit", "periods": []})
    expected_match = "billing_cycle mode 'explicit' requires a non-empty 'periods' list"

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 1, 1), date(2026, 1, 31), billing_settings)


def test_billing_periods_explicit_missingperiodskey_raisesbillingerror():
    billing_settings = _settings({"mode": "explicit"})
    expected_match = "billing_cycle mode 'explicit' requires a non-empty 'periods' list"

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 1, 1), date(2026, 1, 31), billing_settings)


def test_billing_periods_unknownmode_raisesbillingerror():
    billing_settings = _settings({"mode": "bogus"})
    expected_match = "Unknown billing_cycle mode: 'bogus'"

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        billing_periods(date(2026, 1, 1), date(2026, 1, 31), billing_settings)


# ---------------------------------------------------------------------------
# current_period
# ---------------------------------------------------------------------------


def test_current_period_calendarmonth_returnscontainingperiod():
    billing_settings = _settings({"mode": "calendar_month"})

    period = current_period(date(2026, 7, 15), billing_settings)

    assert period == BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")


@pytest.mark.parametrize(
    "on", [date(2026, 7, 1), date(2026, 7, 31)], ids=["period_start", "period_end"]
)
def test_current_period_periodboundaryday_returnssameperiod(on):
    billing_settings = _settings({"mode": "calendar_month"})

    period = current_period(on, billing_settings)

    assert period == BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")


def test_current_period_explicitnocoverage_raisesbillingerror():
    billing_settings = _settings({"mode": "explicit", "periods": [["2026-01-01", "2026-01-31"]]})
    expected_match = "No billing period covers date 2026-02-15"

    with pytest.raises(BillingError, match=re.escape(expected_match)):
        current_period(date(2026, 2, 15), billing_settings)


# ---------------------------------------------------------------------------
# predict_bill
# ---------------------------------------------------------------------------


def test_predict_bill_onlyweekdaysampled_fallsbacktooverallaverageforweekend(
    config, tariff, mains_channel
):
    usages = _daily_usages([10.0, 10.0, 10.0, 10.0, 10.0], 2026, 7, 6)  # Mon-Fri, Jul 6-10
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(
        usages, mains_channel, config, tariff, billing_settings, on=date(2026, 7, 10)
    )

    assert (result.days_elapsed, result.days_remaining) == (5, 21)
    assert (result.to_date_kwh, result.projected_kwh, result.predicted_kwh) == pytest.approx(
        (50.0, 210.0, 260.0)
    )


def test_predict_bill_mixeddaytypes_projectsusingmatchingtypeaverage(
    config, tariff, mains_channel
):
    usages = _daily_usages(
        [10.0, 10.0, 10.0, 10.0, 10.0, 5.0, 5.0], 2026, 7, 6
    )  # Mon-Fri @10, Sat-Sun @5, Jul 6-12
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(
        usages, mains_channel, config, tariff, billing_settings, on=date(2026, 7, 12)
    )

    assert (result.days_elapsed, result.days_remaining) == (7, 19)
    assert set(result.per_day_type) == {"weekday", "weekend_holiday"}
    assert result.per_day_type["weekday"] == pytest.approx((10.0, 2.03))
    assert result.per_day_type["weekend_holiday"] == pytest.approx((5.0, 0.49))
    assert (result.to_date_kwh, result.to_date_energy_cost) == pytest.approx((60.0, 11.13))
    assert (result.projected_kwh, result.projected_energy_cost) == pytest.approx((170.0, 32.41))
    assert (result.predicted_kwh, result.predicted_energy_cost) == pytest.approx((230.0, 43.54))


def test_predict_bill_noelapseddata_projectszero(config, tariff, mains_channel):
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill([], mains_channel, config, tariff, billing_settings, on=date(2026, 7, 1))

    assert (result.days_elapsed, result.days_remaining) == (0, 30)
    assert result.per_day_type == {}
    assert (result.to_date_kwh, result.to_date_energy_cost) == (0.0, 0.0)
    assert (result.predicted_kwh, result.predicted_energy_cost) == (0.0, 0.0)


def test_predict_bill_oncycleend_dayszeroremaining(config, tariff, mains_channel):
    usages = _daily_usages([12.0], 2026, 7, 31)
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(
        usages, mains_channel, config, tariff, billing_settings, on=date(2026, 7, 31)
    )

    assert (result.days_elapsed, result.days_remaining) == (1, 0)
    assert (result.projected_kwh, result.projected_energy_cost) == (0.0, 0.0)
    assert result.predicted_kwh == pytest.approx(result.to_date_kwh)


def test_predict_bill_predictedfull_matchesindependentbillestimate(config, tariff, mains_channel):
    usages = _daily_usages([10.0, 10.0, 10.0, 10.0, 10.0], 2026, 7, 6)
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(
        usages, mains_channel, config, tariff, billing_settings, on=date(2026, 7, 10)
    )

    expected = bill_estimate(
        whole_home_kwh=result.predicted_kwh,
        commodity_cost=result.predicted_energy_cost,
        on=result.period.start,
        tariff=tariff,
        months=1.0,
    )
    assert result.predicted_full == expected


def test_predict_bill_uloplan_usesuloratesfortodatecost(config, tariff, mains_channel):
    usages = [_usage(_utc(2026, 7, 6, 8), 1, "1", 2.0)]  # UTC 08:00 -> local 04:00, overnight
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(
        usages, mains_channel, config, tariff, billing_settings, on=date(2026, 7, 6), plan="ulo"
    )

    assert result.plan == "ulo"
    assert result.to_date_energy_cost == pytest.approx(2.0 * 0.039)


# ---------------------------------------------------------------------------
# Hypothesis property tests - pure period math and prediction invariants (T-7)
# ---------------------------------------------------------------------------


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
    span_days=st.integers(min_value=0, max_value=400),
)
def test_billing_periods_calendarmonth_anyrange_perioddayssumtospan(start, span_days):
    end = start + timedelta(days=span_days)
    billing_settings = _settings({"mode": "calendar_month"})

    periods = billing_periods(start, end, billing_settings)

    total_period_days = sum((p.end - p.start).days + 1 for p in periods)
    expected_span_days = (periods[-1].end - periods[0].start).days + 1
    assert total_period_days == expected_span_days


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
    span_days=st.integers(min_value=0, max_value=400),
)
def test_billing_periods_anchorday_anyrange_perioddayssumtospan(start, span_days):
    end = start + timedelta(days=span_days)
    billing_settings = _settings({"mode": "anchor_day", "day": 15})

    periods = billing_periods(start, end, billing_settings)

    total_period_days = sum((p.end - p.start).days + 1 for p in periods)
    expected_span_days = (periods[-1].end - periods[0].start).days + 1
    assert total_period_days == expected_span_days


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    daily_kwh=st.lists(
        st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    )
)
def test_predict_bill_anyelapsedkwh_predictedkwh_gte_todatekwh(
    config, tariff, mains_channel, daily_kwh
):
    usages = _daily_usages(daily_kwh, 2026, 7, 1)
    on = date(2026, 7, len(daily_kwh))
    billing_settings = _settings({"mode": "calendar_month"})

    result = predict_bill(usages, mains_channel, config, tariff, billing_settings, on=on)

    assert result.predicted_kwh >= result.to_date_kwh - 1e-9
