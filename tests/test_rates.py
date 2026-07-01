# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.rates - COMPLETE test suite written FIRST."""

import copy
import dataclasses
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.rates import (
    RatesConfigError,
    is_off_peak_day,
    load_config,
    observed_holidays,
    price_row,
    season_for,
    tiered_rates,
    tiered_threshold_kwh,
    to_local,
    tou_bucket,
    tou_rate,
    ulo_bucket,
    ulo_rate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TORONTO = ZoneInfo("America/Toronto")

SUMMER_WEEKDAY = date(2026, 6, 16)  # Tuesday, non-holiday
WINTER_WEEKDAY = date(2026, 1, 13)  # Tuesday, non-holiday
SATURDAY = date(2026, 6, 20)
SUNDAY = date(2026, 6, 21)
CANADA_DAY_2026 = date(2026, 7, 1)  # Wednesday, statutory holiday

EXPECTED_2026_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day
        date(2026, 2, 16),  # Family Day
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 18),  # Victoria Day
        date(2026, 7, 1),  # Canada Day
        date(2026, 8, 3),  # Civic Holiday
        date(2026, 9, 7),  # Labour Day
        date(2026, 10, 12),  # Thanksgiving
        date(2026, 12, 25),  # Christmas Day
        date(2026, 12, 28),  # Boxing Day (Sat Dec 26 shifted to Mon Dec 28)
    }
)


def _delete_nested_key(data: dict, path: tuple[str, ...]) -> dict:
    """Return a deep copy of data with the nested key at path removed."""
    result = copy.deepcopy(data)
    cursor = result
    for key in path[:-1]:
        cursor = cursor[key]
    del cursor[path[-1]]
    return result


def _local(d: date, hour: int, minute: int = 0) -> datetime:
    """Build a timezone-aware America/Toronto datetime for a given local wall time."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TORONTO)


@pytest.fixture
def base_rates_dict() -> dict:
    """Raw parsed JSON of the real config/rates.json, used as a mutation base."""
    raw = (REPO_ROOT / "config" / "rates.json").read_text(encoding="utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_realconfig_returns_parsed_fields(config):
    result = (config.timezone_name, config.tiered_thresholds)
    assert result == ("America/Toronto", {"summer_kwh": 600, "winter_kwh": 1000})


def test_load_config_missingfile_raises_ratesconfigerror(tmp_path):
    expected_path = tmp_path / "rates.json"
    expected_match = f"Rates config file not found: {expected_path}"

    with pytest.raises(RatesConfigError, match=re.escape(expected_match)):
        load_config(tmp_path)


def test_load_config_invalidjson_raises_ratesconfigerror(tmp_path):
    (tmp_path / "rates.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RatesConfigError, match=re.escape("Invalid JSON in rates config file")):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("missing_path", "expected_match"),
    [
        (("timezone",), "Missing required key 'timezone'"),
        (("plans",), "Missing required key 'plans'"),
        (("plans", "tou", "prices"), "Missing required key 'prices'"),
        (("plans", "tiered", "thresholds"), "Missing required key 'thresholds'"),
        (("plans", "tiered", "thresholds", "summer_kwh"), "Missing required key 'summer_kwh'"),
        (("schedule",), "Missing required key 'schedule'"),
        (("schedule", "tou", "summer"), "Missing required key 'summer'"),
        (("schedule", "ulo"), "Missing required key 'ulo'"),
        (("holidays", "rules"), "Missing required key 'rules'"),
    ],
    ids=[
        "timezone",
        "plans",
        "plans.tou.prices",
        "plans.tiered.thresholds",
        "plans.tiered.thresholds.summer_kwh",
        "schedule",
        "schedule.tou.summer",
        "schedule.ulo",
        "holidays.rules",
    ],
)
def test_load_config_missingrequiredkey_raises_ratesconfigerror(
    base_rates_dict, tmp_path, missing_path, expected_match
):
    mutated = _delete_nested_key(base_rates_dict, missing_path)
    (tmp_path / "rates.json").write_text(json.dumps(mutated), encoding="utf-8")

    with pytest.raises(RatesConfigError, match=re.escape(expected_match)):
        load_config(tmp_path)


def test_load_config_missingoverrideskey_defaultsemptytuple(base_rates_dict, tmp_path):
    mutated = _delete_nested_key(base_rates_dict, ("holidays", "overrides"))
    (tmp_path / "rates.json").write_text(json.dumps(mutated), encoding="utf-8")

    result = load_config(tmp_path)

    assert result.holiday_overrides == ()


def test_load_config_defaultconfigdir_loadsrepoconfig(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)

    result = load_config()

    assert result.timezone_name == "America/Toronto"


# ---------------------------------------------------------------------------
# to_local
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_dt", "expected_local", "expected_offset_hours"),
    [
        (datetime(2026, 1, 13, 15, 0, tzinfo=UTC), _local(WINTER_WEEKDAY, 10, 0), -5),
        (datetime(2026, 6, 16, 15, 0, tzinfo=UTC), _local(SUMMER_WEEKDAY, 11, 0), -4),
    ],
    ids=["winter_est", "summer_edt"],
)
def test_to_local_awareutcinstant_returnslocalwalltime(
    config, utc_dt, expected_local, expected_offset_hours
):
    result = to_local(utc_dt, config)

    assert (result, result.utcoffset().total_seconds() / 3600) == (
        expected_local,
        expected_offset_hours,
    )


def test_to_local_naivedatetime_raisesvalueerror(config):
    naive = datetime(2026, 6, 16, 12, 0)

    with pytest.raises(ValueError, match=re.escape("naive datetime")):
        to_local(naive, config)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dt=st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2100, 1, 1)).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )
)
def test_to_local_anyawareutcdatetime_preservesinstant(config, dt):
    local = to_local(dt, config)

    assert local.astimezone(UTC) == dt


# ---------------------------------------------------------------------------
# season_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("d", "expected_season"),
    [
        (date(2026, 4, 29), "winter"),
        (date(2026, 4, 30), "winter"),
        (date(2026, 5, 1), "summer"),
        (date(2026, 5, 2), "summer"),
        (date(2026, 10, 30), "summer"),
        (date(2026, 10, 31), "summer"),
        (date(2026, 11, 1), "winter"),
        (date(2026, 11, 2), "winter"),
    ],
    ids=["apr29", "apr30", "may1", "may2", "oct30", "oct31", "nov1", "nov2"],
)
def test_season_for_boundarydate_returnsexpectedseason(d, expected_season):
    assert season_for(d) == expected_season


@given(d=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)))
def test_season_for_anydate_returnsvalidseasonname(d):
    assert season_for(d) in {"summer", "winter"}


# ---------------------------------------------------------------------------
# observed_holidays
# ---------------------------------------------------------------------------


def test_observed_holidays_year2026_returnsexpectedset(config):
    result = observed_holidays(2026, config)

    assert result == EXPECTED_2026_HOLIDAYS


def test_observed_holidays_shiftcollideswithanotherholiday_skipsaheadtonextweekday(config):
    # Sat Jan 4 2025 must shift past Sun Jan 5 (weekend) AND past Mon Jan 6 (also a raw
    # holiday date below), landing on Tue Jan 7 -- exercises the `shifted in raw_set`
    # collision-avoidance branch, not just the weekend-skip branch.
    collision_config = dataclasses.replace(
        config,
        holiday_rules=(
            {"name": "SatHoliday", "type": "fixed", "month": 1, "day": 4},
            {"name": "MonHoliday", "type": "fixed", "month": 1, "day": 6},
        ),
    )

    result = observed_holidays(2025, collision_config)

    assert result == frozenset({date(2025, 1, 6), date(2025, 1, 7)})


def test_observed_holidays_unknownruletype_raisesratesconfigerror(config):
    bad_config = dataclasses.replace(config, holiday_rules=({"name": "Bogus", "type": "made_up"},))

    with pytest.raises(RatesConfigError, match=re.escape("Unknown holiday rule type: 'made_up'")):
        observed_holidays(2026, bad_config)


@pytest.mark.parametrize(
    ("year", "expected_included"),
    [(2027, True), (2026, False)],
    ids=["matchingyear", "mismatchedyear"],
)
def test_observed_holidays_overrideyearfilter_includesonlymatchingyear(
    config, year, expected_included
):
    override_config = dataclasses.replace(config, holiday_overrides=("2027-03-15",))

    result = date(2027, 3, 15) in observed_holidays(year, override_config)

    assert result == expected_included


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(year=st.integers(min_value=2000, max_value=2100))
def test_observed_holidays_anyyear_allobserveddatesareweekdays(config, year):
    holidays_for_year = observed_holidays(year, config)

    assert all(d.weekday() < 5 for d in holidays_for_year)


# ---------------------------------------------------------------------------
# is_off_peak_day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (SATURDAY, True),
        (CANADA_DAY_2026, True),
        (SUMMER_WEEKDAY, False),
    ],
    ids=["saturday", "holidayweekday", "plainweekday"],
)
def test_is_off_peak_day_variousdates_returnsexpected(config, d, expected):
    assert is_off_peak_day(d, config) == expected


# ---------------------------------------------------------------------------
# tou_bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "d",
    [SATURDAY, SUNDAY, CANADA_DAY_2026],
    ids=["saturday", "sunday", "holiday"],
)
def test_tou_bucket_offpeakday_returnsoff(config, d):
    ts = _local(d, 12)

    assert tou_bucket(ts, config) == "off"


@pytest.mark.parametrize(
    ("hour", "expected_bucket"),
    [
        (6, "off"),
        (7, "mid"),
        (8, "mid"),
        (10, "mid"),
        (11, "on"),
        (12, "on"),
        (16, "on"),
        (17, "mid"),
        (18, "mid"),
        (19, "off"),
        (20, "off"),
        (23, "off"),
        (0, "off"),
    ],
)
def test_tou_bucket_summerweekdayhour_returnsexpectedbucket(config, hour, expected_bucket):
    ts = _local(SUMMER_WEEKDAY, hour)

    assert tou_bucket(ts, config) == expected_bucket


@pytest.mark.parametrize(
    ("hour", "expected_bucket"),
    [
        (6, "off"),
        (7, "on"),
        (8, "on"),
        (10, "on"),
        (11, "mid"),
        (12, "mid"),
        (16, "mid"),
        (17, "on"),
        (18, "on"),
        (19, "off"),
        (20, "off"),
        (23, "off"),
        (0, "off"),
    ],
)
def test_tou_bucket_winterweekdayhour_returnsexpectedbucket(config, hour, expected_bucket):
    ts = _local(WINTER_WEEKDAY, hour)

    assert tou_bucket(ts, config) == expected_bucket


def test_tou_bucket_nocoveringsegment_raisesratesconfigerror(config):
    broken = dataclasses.replace(
        config, tou_schedule={**config.tou_schedule, "summer": {"on": ((11, 17),)}}
    )
    ts = _local(SUMMER_WEEKDAY, 3)

    with pytest.raises(RatesConfigError, match=re.escape("No TOU schedule segment covers hour 3")):
        tou_bucket(ts, broken)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dt=st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 12, 31)).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )
)
def test_tou_bucket_anytimestamp_returnsvalidbucketname(config, dt):
    assert tou_bucket(dt, config) in {"on", "mid", "off"}


# ---------------------------------------------------------------------------
# ulo_bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected_bucket"),
    [
        (6, "overnight"),
        (7, "mid"),
        (8, "mid"),
        (15, "mid"),
        (16, "on"),
        (17, "on"),
        (20, "on"),
        (21, "mid"),
        (22, "mid"),
        (23, "overnight"),
        (0, "overnight"),
    ],
)
def test_ulo_bucket_weekdayhour_returnsexpectedbucket(config, hour, expected_bucket):
    ts = _local(SUMMER_WEEKDAY, hour)

    assert ulo_bucket(ts, config) == expected_bucket


@pytest.mark.parametrize(
    ("d", "hour"),
    [(SATURDAY, 1), (CANADA_DAY_2026, 2)],
    ids=["weekend", "holiday"],
)
def test_ulo_bucket_offpeakday_overnighthour_returnsovernight(config, d, hour):
    ts = _local(d, hour)

    assert ulo_bucket(ts, config) == "overnight"


@pytest.mark.parametrize(
    "d",
    [SATURDAY, CANADA_DAY_2026],
    ids=["weekend", "holiday"],
)
def test_ulo_bucket_offpeakday_nonovernighthour_returnsweekendoff(config, d):
    ts = _local(d, 10)

    assert ulo_bucket(ts, config) == "weekend_off"


def test_ulo_bucket_nocoveringsegment_raisesratesconfigerror(config):
    broken = dataclasses.replace(config, ulo_schedule={**config.ulo_schedule, "on": (), "mid": ()})
    ts = _local(SUMMER_WEEKDAY, 10)
    expected_match = "No ULO schedule segment covers hour 10"

    with pytest.raises(RatesConfigError, match=re.escape(expected_match)):
        ulo_bucket(ts, broken)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dt=st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 12, 31)).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )
)
def test_ulo_bucket_anytimestamp_returnsvalidbucketname(config, dt):
    assert ulo_bucket(dt, config) in {"overnight", "weekend_off", "on", "mid"}


# ---------------------------------------------------------------------------
# DST transition correctness (spring-forward 2026-03-08, fall-back 2026-11-01)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utc_dt",
    [
        datetime(2026, 3, 8, 6, 59, tzinfo=UTC),  # local 01:59 EST, pre-transition
        datetime(2026, 3, 8, 7, 0, tzinfo=UTC),  # local 03:00 EDT, post-transition
    ],
    ids=["pretransition_est", "posttransition_edt"],
)
def test_tou_bucket_springforwardday_sundayremainsoffacrossoffsetjump(config, utc_dt):
    assert tou_bucket(utc_dt, config) == "off"


@pytest.mark.parametrize(
    ("utc_dt", "expected_bucket"),
    [
        (datetime(2026, 3, 8, 6, 59, tzinfo=UTC), "overnight"),  # local 01:59 EST
        (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), "overnight"),  # local 03:00 EDT
        (datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "weekend_off"),  # local 08:00 EDT
    ],
    ids=["pretransition_overnight", "posttransition_overnight", "posttransition_daytime"],
)
def test_ulo_bucket_springforwardday_correctlocalhouracrossoffsetjump(
    config, utc_dt, expected_bucket
):
    assert ulo_bucket(utc_dt, config) == expected_bucket


@pytest.mark.parametrize(
    ("utc_dt", "expected_bucket"),
    [
        (datetime(2026, 11, 1, 5, 59, tzinfo=UTC), "overnight"),  # local 01:59 EDT
        (datetime(2026, 11, 1, 6, 0, tzinfo=UTC), "overnight"),  # local 01:00 EST, post-transition
        (datetime(2026, 11, 1, 13, 0, tzinfo=UTC), "weekend_off"),  # local 08:00 EST
    ],
    ids=["pretransition_overnight", "posttransition_overnight", "posttransition_daytime"],
)
def test_ulo_bucket_fallbackday_correctlocalhouracrossoffsetjump(config, utc_dt, expected_bucket):
    assert ulo_bucket(utc_dt, config) == expected_bucket


# ---------------------------------------------------------------------------
# price_row / effective-dated boundary
# ---------------------------------------------------------------------------


def test_price_row_effectivedateboundary_prerow_returnsoldrates(config):
    row = price_row("tou", date(2025, 10, 31), config)

    assert (row["off"], row["mid"], row["on"]) == (0.076, 0.122, 0.158)


def test_price_row_effectivedateboundary_postrow_returnsnewrates(config):
    row = price_row("tou", date(2025, 11, 1), config)

    assert (row["off"], row["mid"], row["on"]) == (0.098, 0.157, 0.203)


@pytest.mark.parametrize(
    ("utc_dt", "expected_rate"),
    [
        (datetime(2025, 11, 1, 3, 0, tzinfo=UTC), 0.076),  # Oct31 2025 23:00 local, pre-row
        (datetime(2025, 11, 1, 11, 0, tzinfo=UTC), 0.098),  # Nov1 2025 07:00 local, post-row
    ],
    ids=["oct31_2025_23h_local_prerow", "nov1_2025_07h_local_postrow"],
)
def test_tou_rate_effectivedateboundary_pickscorrectpricerow(config, utc_dt, expected_rate):
    assert tou_rate(utc_dt, config) == expected_rate


def test_price_row_unknownplan_raisesratesconfigerror(config):
    with pytest.raises(RatesConfigError, match=re.escape("Unknown pricing plan: 'bogus'")):
        price_row("bogus", SUMMER_WEEKDAY, config)


def test_price_row_nodatecoverage_raisesratesconfigerror(config):
    with pytest.raises(
        RatesConfigError, match=re.escape("No price row for plan 'tou' covering date 2000-01-01")
    ):
        price_row("tou", date(2000, 1, 1), config)


def test_price_row_openendedexpiry_matchesfardate(config):
    open_row = {"effective": date(2030, 1, 1), "expiry": None, "off": 0.5, "mid": 0.6, "on": 0.7}
    synthetic = dataclasses.replace(config, plan_prices={**config.plan_prices, "tou": (open_row,)})

    row = price_row("tou", date(2099, 1, 1), synthetic)

    assert row is open_row


def test_price_row_multiplecandidates_pickslatesteffective(config):
    older_row = {"effective": date(2020, 1, 1), "expiry": None, "off": 0.1, "mid": 0.1, "on": 0.1}
    newer_row = {"effective": date(2021, 1, 1), "expiry": None, "off": 0.2, "mid": 0.2, "on": 0.2}
    synthetic = dataclasses.replace(
        config, plan_prices={**config.plan_prices, "tou": (older_row, newer_row)}
    )

    row = price_row("tou", date(2022, 1, 1), synthetic)

    assert row is newer_row


# ---------------------------------------------------------------------------
# tou_rate / ulo_rate current-row verified domain facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected_rate"),
    [(2, 0.098), (8, 0.157), (12, 0.203)],
    ids=["off", "mid", "on"],
)
def test_tou_rate_currentrow_summerweekday_matchesbucketrate(config, hour, expected_rate):
    ts = _local(SUMMER_WEEKDAY, hour)

    assert tou_rate(ts, config) == expected_rate


@pytest.mark.parametrize(
    ("d", "hour", "expected_rate"),
    [
        (SUMMER_WEEKDAY, 2, 0.039),  # overnight
        (SUMMER_WEEKDAY, 10, 0.157),  # mid
        (SUMMER_WEEKDAY, 18, 0.391),  # on
        (SATURDAY, 10, 0.098),  # weekend_off
    ],
    ids=["overnight", "mid", "on", "weekend_off"],
)
def test_ulo_rate_currentrow_matchesbucketrate(config, d, hour, expected_rate):
    ts = _local(d, hour)

    assert ulo_rate(ts, config) == expected_rate


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dt=st.datetimes(min_value=datetime(2025, 12, 1), max_value=datetime(2026, 9, 30)).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )
)
def test_tou_rate_withincurrentrow_returnsknownrate(config, dt):
    assert tou_rate(dt, config) in {0.098, 0.157, 0.203}


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dt=st.datetimes(min_value=datetime(2025, 12, 1), max_value=datetime(2026, 9, 30)).map(
        lambda naive: naive.replace(tzinfo=UTC)
    )
)
def test_ulo_rate_withincurrentrow_returnsknownrate(config, dt):
    assert ulo_rate(dt, config) in {0.039, 0.098, 0.157, 0.391}


# ---------------------------------------------------------------------------
# tiered_rates / tiered_threshold_kwh
# ---------------------------------------------------------------------------


def test_tiered_rates_currentrow_returnsexpectedtiers(config):
    assert tiered_rates(SUMMER_WEEKDAY, config) == (0.120, 0.142)


def test_tiered_rates_olderrow_returnsexpectedtiers(config):
    assert tiered_rates(date(2025, 6, 1), config) == (0.093, 0.110)


@pytest.mark.parametrize(
    ("d", "expected_threshold"),
    [(SUMMER_WEEKDAY, 600), (WINTER_WEEKDAY, 1000)],
    ids=["summer", "winter"],
)
def test_tiered_threshold_kwh_season_returnsexpectedthreshold(config, d, expected_threshold):
    assert tiered_threshold_kwh(d, config) == expected_threshold


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(d=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)))
def test_tiered_threshold_kwh_anydate_returnsconfiguredthreshold(config, d):
    assert tiered_threshold_kwh(d, config) in {
        config.tiered_thresholds["summer_kwh"],
        config.tiered_thresholds["winter_kwh"],
    }
