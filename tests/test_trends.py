# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.trends - COMPLETE test suite written FIRST."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.trends import (
    DayStat,
    daily_series,
    render_daily_svg,
    rolling_average,
    trend,
    weekday_weekend_summary,
)


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


def _day_stat(
    day: date,
    kwh: float,
    on_kwh: float = 0.0,
    mid_kwh: float = 0.0,
    off_kwh: float = 0.0,
    cost: float = 0.0,
) -> DayStat:
    """Build a DayStat fixture directly, bypassing daily_series aggregation."""
    return DayStat(day=day, kwh=kwh, on_kwh=on_kwh, mid_kwh=mid_kwh, off_kwh=off_kwh, cost=cost)


# ---------------------------------------------------------------------------
# daily_series - whole-home filtering (mains present / absent fallback)
# ---------------------------------------------------------------------------


def test_daily_series_mainschannelpresent_excludesnonmainsusages(config):
    channels = [_channel(1, "1", "Mains", "mains"), _channel(1, "10", "Dryer", "branch")]
    usages = [
        _usage(_utc(2026, 7, 6, 17), 1, "1", 2.0),
        _usage(_utc(2026, 7, 6, 17), 1, "10", 5.0),
    ]

    result = daily_series(usages, channels, config)

    assert result[0].kwh == pytest.approx(2.0)


def test_daily_series_nomainschannel_treatsallusagesaswholehome(config):
    channels = [_channel(1, "10", "Dryer", "branch"), _channel(1, "20", "Fridge", "branch")]
    usages = [
        _usage(_utc(2026, 7, 6, 17), 1, "10", 1.0),
        _usage(_utc(2026, 7, 6, 17), 1, "20", 3.0),
    ]

    result = daily_series(usages, channels, config)

    assert result[0].kwh == pytest.approx(4.0)


def test_daily_series_emptychannels_treatsallusagesaswholehome(config):
    usages = [_usage(_utc(2026, 7, 6, 17), 1, "1", 1.5)]

    result = daily_series(usages, [], config)

    assert result[0].kwh == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# daily_series - boundary: empty / single / many usages
# ---------------------------------------------------------------------------


def test_daily_series_emptyusages_returnsemptylist(config):
    result = daily_series([], [_channel(1, "1", "Mains", "mains")], config)

    assert result == []


def test_daily_series_singleusage_returnssingledaystat(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 6), 1, "1", 1.0)]

    result = daily_series(usages, channels, config)

    assert result == [DayStat(date(2026, 7, 6), 1.0, 0.0, 0.0, 1.0, pytest.approx(0.098))]


def test_daily_series_manyusagesoneday_splitsbucketsandcostexactly(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [
        _usage(_utc(2026, 7, 6, 6), 1, "1", 1.0),  # local 02:00 -> off
        _usage(_utc(2026, 7, 6, 12), 1, "1", 2.0),  # local 08:00 -> mid
        _usage(_utc(2026, 7, 6, 17), 1, "1", 3.0),  # local 13:00 -> on
        _usage(_utc(2026, 7, 6, 22), 1, "1", 0.5),  # local 18:00 -> mid
    ]

    result = daily_series(usages, channels, config)

    day_stat = result[0]
    assert (day_stat.kwh, day_stat.on_kwh, day_stat.mid_kwh, day_stat.off_kwh) == pytest.approx(
        (6.5, 3.0, 2.5, 1.0)
    )
    assert day_stat.cost == pytest.approx(1.0995)


def test_daily_series_manydays_sortsascendingbylocalday(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [
        _usage(_utc(2026, 7, 8, 17), 1, "1", 1.0),
        _usage(_utc(2026, 7, 6, 17), 1, "1", 1.0),
        _usage(_utc(2026, 7, 7, 17), 1, "1", 1.0),
    ]

    result = daily_series(usages, channels, config)

    assert [stat.day for stat in result] == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]


def test_daily_series_utcnearmidnight_groupsbylocaldate(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 2, 0), 1, "1", 1.0)]  # local: Jul 5 22:00 (prior day)

    result = daily_series(usages, channels, config)

    assert result[0].day == date(2026, 7, 5)


# ---------------------------------------------------------------------------
# daily_series - plan validation (negative path)
# ---------------------------------------------------------------------------


def test_daily_series_uloplan_raisesvalueerror(config):
    with pytest.raises(ValueError, match=re.escape("Unsupported trend plan: 'ulo'")):
        daily_series([], [], config, plan="ulo")


def test_daily_series_touplan_defaultplandoesnotraise(config):
    result = daily_series([], [], config, plan="tou")

    assert result == []


# ---------------------------------------------------------------------------
# weekday_weekend_summary
# ---------------------------------------------------------------------------


def test_weekday_weekend_summary_emptydaily_returnszeroedbothgroups(config):
    result = weekday_weekend_summary([], config)

    assert result == {
        "weekday": {"avg_kwh": 0.0, "avg_cost": 0.0, "days": 0},
        "weekend_holiday": {"avg_kwh": 0.0, "avg_cost": 0.0, "days": 0},
    }


def test_weekday_weekend_summary_weekdaysonly_averagesweekdaygroup(config):
    daily = [
        _day_stat(date(2026, 7, 6), kwh=10.0, cost=1.0),  # Monday
        _day_stat(date(2026, 7, 7), kwh=20.0, cost=2.0),  # Tuesday
    ]

    result = weekday_weekend_summary(daily, config)

    assert result["weekday"] == {
        "avg_kwh": pytest.approx(15.0),
        "avg_cost": pytest.approx(1.5),
        "days": 2,
    }
    assert result["weekend_holiday"] == {"avg_kwh": 0.0, "avg_cost": 0.0, "days": 0}


def test_weekday_weekend_summary_weekendsonly_averagesweekendgroup(config):
    daily = [
        _day_stat(date(2026, 7, 4), kwh=5.0, cost=0.5),  # Saturday
        _day_stat(date(2026, 7, 5), kwh=7.0, cost=0.7),  # Sunday
    ]

    result = weekday_weekend_summary(daily, config)

    assert result["weekend_holiday"] == {
        "avg_kwh": pytest.approx(6.0),
        "avg_cost": pytest.approx(0.6),
        "days": 2,
    }
    assert result["weekday"] == {"avg_kwh": 0.0, "avg_cost": 0.0, "days": 0}


def test_weekday_weekend_summary_statutoryholidayonweekday_countsasweekendholiday(config):
    daily = [_day_stat(date(2026, 7, 1), kwh=8.0, cost=0.8)]  # Canada Day, a Wednesday

    result = weekday_weekend_summary(daily, config)

    assert result["weekend_holiday"] == {
        "avg_kwh": pytest.approx(8.0),
        "avg_cost": pytest.approx(0.8),
        "days": 1,
    }
    assert result["weekday"]["days"] == 0


def test_weekday_weekend_summary_mixeddays_splitsintobothgroups(config):
    daily = [
        _day_stat(date(2026, 7, 6), kwh=10.0, cost=1.0),  # Monday
        _day_stat(date(2026, 7, 4), kwh=4.0, cost=0.4),  # Saturday
    ]

    result = weekday_weekend_summary(daily, config)

    assert (result["weekday"]["days"], result["weekend_holiday"]["days"]) == (1, 1)


# ---------------------------------------------------------------------------
# rolling_average - boundary: window min-1 / min / min+1, empty/single/many
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", [0, -1], ids=["window_zero", "window_negative"])
def test_rolling_average_windowlessthanone_raisesvalueerror(window):
    with pytest.raises(ValueError, match=re.escape("window must be >= 1")):
        rolling_average([1.0, 2.0], window)


def test_rolling_average_windowequalsone_returnsvaluesunchanged():
    result = rolling_average([1.0, 2.0, 3.0], window=1)

    assert result == pytest.approx([1.0, 2.0, 3.0])


def test_rolling_average_windowequalstwo_averagestrailingpairs():
    result = rolling_average([1.0, 2.0, 3.0, 4.0], window=2)

    assert result == pytest.approx([1.0, 1.5, 2.5, 3.5])


def test_rolling_average_windowlargerthanseries_averagesallavailablepoints():
    result = rolling_average([1.0, 2.0, 3.0], window=100)

    assert result == pytest.approx([1.0, 1.5, 2.0])


def test_rolling_average_emptyvalues_returnsemptylist():
    result = rolling_average([], window=7)

    assert result == []


def test_rolling_average_singlevalue_returnssinglevalue():
    result = rolling_average([5.0], window=7)

    assert result == pytest.approx([5.0])


def test_rolling_average_manyvalues_lengthmatchesinput():
    values = [float(i) for i in range(14)]

    result = rolling_average(values, window=7)

    assert len(result) == 14


# ---------------------------------------------------------------------------
# trend - empty/single, flat/rising/falling, mean-zero guard
# ---------------------------------------------------------------------------


def test_trend_emptyvalues_returnsflatzeroslope():
    result = trend([])

    assert result == {"slope_per_day": 0.0, "direction": "flat", "pct_per_day": 0.0}


def test_trend_singlevalue_returnsflatzeroslope():
    result = trend([42.0])

    assert result == {"slope_per_day": 0.0, "direction": "flat", "pct_per_day": 0.0}


def test_trend_twovalues_computesslopeatminimalseriesboundary():
    result = trend([1.0, 3.0])

    assert (result["slope_per_day"], result["direction"]) == (pytest.approx(2.0), "rising")


def test_trend_constantvalues_returnsflatdirection():
    result = trend([3.0, 3.0, 3.0, 3.0])

    assert (result["slope_per_day"], result["direction"]) == (pytest.approx(0.0, abs=1e-9), "flat")


def test_trend_monotonerising_returnspositiveslopeandrisingdirection():
    result = trend([1.0, 2.0, 3.0, 4.0, 5.0])

    assert result["slope_per_day"] == pytest.approx(1.0)
    assert result["direction"] == "rising"
    assert result["pct_per_day"] == pytest.approx(100.0 / 3.0)


def test_trend_monotonefalling_returnsnegativeslopeandfallingdirection():
    result = trend([5.0, 4.0, 3.0, 2.0, 1.0])

    assert result["slope_per_day"] == pytest.approx(-1.0)
    assert result["direction"] == "falling"


def test_trend_meanzerobutslopenonzero_pctperdayguardsdivisionbyzero():
    result = trend([-1.0, 0.0, 1.0])

    assert result["slope_per_day"] == pytest.approx(1.0)
    assert result["pct_per_day"] == 0.0


# ---------------------------------------------------------------------------
# render_daily_svg - empty placeholder, non-empty chart, light/dark theme
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_render_daily_svg_emptydaily_writesplaceholdersvg(tmp_path, dark):
    path = tmp_path / "empty.svg"

    render_daily_svg([], path, dark=dark)

    data = path.read_bytes()
    assert data != b""
    assert b"<svg" in data


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_render_daily_svg_manydays_writesvalidsvg(tmp_path, dark):
    daily = [_day_stat(date(2026, 7, 1) + timedelta(days=i), kwh=float(i + 1)) for i in range(10)]
    path = tmp_path / "chart.svg"

    render_daily_svg(daily, path, dark=dark)

    data = path.read_bytes()
    assert data != b""
    assert b"<svg" in data


@pytest.mark.parametrize(
    "window", [1, 7, 30], ids=["window_min", "window_default", "window_largerthanseries"]
)
def test_render_daily_svg_windowboundary_writesvalidsvg(tmp_path, window):
    daily = [_day_stat(date(2026, 7, 1) + timedelta(days=i), kwh=float(i + 1)) for i in range(5)]
    path = tmp_path / "chart.svg"

    render_daily_svg(daily, path, window=window)

    data = path.read_bytes()
    assert b"<svg" in data


def test_render_daily_svg_customtitle_writesvalidsvg(tmp_path):
    daily = [_day_stat(date(2026, 7, 1), kwh=3.0)]
    path = tmp_path / "chart.svg"

    render_daily_svg(daily, path, title="Custom Title")

    data = path.read_bytes()
    assert b"<svg" in data


def test_render_daily_svg_invalidwindow_raisesvalueerror(tmp_path):
    daily = [_day_stat(date(2026, 7, 1), kwh=3.0)]
    path = tmp_path / "chart.svg"

    with pytest.raises(ValueError, match=re.escape("window must be >= 1")):
        render_daily_svg(daily, path, window=0)


def test_render_daily_svg_pathaspathlibpath_writesfileatpath(tmp_path):
    daily = [_day_stat(date(2026, 7, 1), kwh=3.0)]
    path = tmp_path / "subdir_free.svg"

    render_daily_svg(daily, path)

    assert path.exists()


def test_render_daily_svg_pathasstring_writesfileatpath(tmp_path):
    daily = [_day_stat(date(2026, 7, 1), kwh=3.0)]
    path = str(tmp_path / "as_string.svg")

    render_daily_svg(daily, path)

    assert (tmp_path / "as_string.svg").exists()


# ---------------------------------------------------------------------------
# Hypothesis property tests (T-7)
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    kwh_values=st.lists(
        st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    )
)
def test_daily_series_anykwhvalues_bucketsplitsumequalsdaytotal(config, kwh_values):
    channels = [_channel(1, "1", "Mains", "mains")]
    ts = _utc(2026, 7, 6, 17)  # fixed on-peak timestamp: every interval lands in "on"
    usages = [_usage(ts, 1, "1", kwh) for kwh in kwh_values]

    result = daily_series(usages, channels, config)

    day_stat = result[0]
    assert day_stat.on_kwh + day_stat.mid_kwh + day_stat.off_kwh == pytest.approx(day_stat.kwh)


@given(
    values=st.lists(
        st.floats(min_value=-1_000, max_value=1_000, allow_nan=False, allow_infinity=False),
        max_size=30,
    ),
    window=st.integers(min_value=1, max_value=10),
)
def test_rolling_average_anyvalues_outputlengthequalsinputlength(values, window):
    result = rolling_average(values, window)

    assert len(result) == len(values)


@given(
    base=st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
    step=st.floats(min_value=0.01, max_value=10, allow_nan=False, allow_infinity=False),
    n=st.integers(min_value=2, max_value=20),
)
def test_trend_monotoneincreasinginput_directionisrising(base, step, n):
    values = [base + i * step for i in range(n)]

    result = trend(values)

    assert result["direction"] == "rising"
