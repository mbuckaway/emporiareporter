# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.cost - COMPLETE test suite written FIRST."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.cost import (
    ChannelCost,
    CostConfigError,
    bill_estimate,
    delivery_row,
    load_tariff,
    oer_rate,
    price_usage,
)
from emporia_hydro.models import BALANCE_CHANNEL, Channel, IntervalUsage

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_TARIFF_DICT = {
    "delivery": [
        {
            "effective": "2026-01-01",
            "fixed_monthly": 35.99,
            "smart_metering_monthly": 0.42,
            "sss_monthly": 0.25,
            "variable_per_kwh": 0.0235,
        }
    ],
    "oer": [{"effective": "2025-11-01", "rate": 0.235}],
    "hst": 0.13,
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


@pytest.fixture
def tariff():
    """Load the real repo config/tariff.json for use as test ground truth."""
    return load_tariff(REPO_ROOT / "config")


# ---------------------------------------------------------------------------
# load_tariff
# ---------------------------------------------------------------------------


def test_load_tariff_realconfig_returnsparsedfields(tariff):
    result = (tariff.hst, len(tariff.delivery), len(tariff.oer))

    assert result == (0.13, 2, 2)


def test_load_tariff_missingfile_raisescostconfigerror(tmp_path):
    expected_match = f"Tariff config file not found: {tmp_path / 'tariff.json'}"

    with pytest.raises(CostConfigError, match=re.escape(expected_match)):
        load_tariff(tmp_path)


def test_load_tariff_invalidjson_raisescostconfigerror(tmp_path):
    (tmp_path / "tariff.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CostConfigError, match=re.escape("Invalid JSON in tariff config file")):
        load_tariff(tmp_path)


@pytest.mark.parametrize(
    ("missing_key", "expected_match"),
    [
        ("delivery", "Missing required key 'delivery' in tariff.json"),
        ("oer", "Missing required key 'oer' in tariff.json"),
        ("hst", "Missing required key 'hst' in tariff.json"),
    ],
    ids=["delivery", "oer", "hst"],
)
def test_load_tariff_missingkey_raisescostconfigerror(tmp_path, missing_key, expected_match):
    data = dict(VALID_TARIFF_DICT)
    del data[missing_key]
    (tmp_path / "tariff.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CostConfigError, match=re.escape(expected_match)):
        load_tariff(tmp_path)


# ---------------------------------------------------------------------------
# delivery_row / oer_rate - effective-dating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on", "expected_fixed_monthly"),
    [(date(2025, 12, 31), 34.81), (date(2026, 1, 1), 35.99)],
    ids=["2025_dec31_prerow", "2026_jan1_postrow"],
)
def test_delivery_row_effectivedateboundary_picksmatchingrow(tariff, on, expected_fixed_monthly):
    row = delivery_row(on, tariff)

    assert row["fixed_monthly"] == expected_fixed_monthly


def test_delivery_row_2025date_returnsolderdeliveryamounts(tariff):
    row = delivery_row(date(2025, 6, 15), tariff)

    assert (row["fixed_monthly"], row["variable_per_kwh"]) == (34.81, 0.0257)


def test_delivery_row_nodatecoverage_raisescostconfigerror(tariff):
    expected_match = "No delivery row covers date 2024-12-31"

    with pytest.raises(CostConfigError, match=re.escape(expected_match)):
        delivery_row(date(2024, 12, 31), tariff)


@pytest.mark.parametrize(
    ("on", "expected_rate"),
    [(date(2025, 10, 31), 0.131), (date(2025, 11, 1), 0.235)],
    ids=["2025_oct31_prerow", "2025_nov1_postrow"],
)
def test_oer_rate_effectivedateboundary_picksmatchingrow(tariff, on, expected_rate):
    assert oer_rate(on, tariff) == expected_rate


def test_oer_rate_nodatecoverage_raisescostconfigerror(tariff):
    expected_match = "No oer row covers date 1999-12-31"

    with pytest.raises(CostConfigError, match=re.escape(expected_match)):
        oer_rate(date(1999, 12, 31), tariff)


# ---------------------------------------------------------------------------
# price_usage - plan validation and dispatch
# ---------------------------------------------------------------------------


def test_price_usage_unknownplan_raisesvalueerror(config):
    with pytest.raises(ValueError, match=re.escape("Unknown pricing plan: 'bogus'")):
        price_usage([], [], config, plan="bogus")


def test_price_usage_touplan_usestoubucketandrate(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 17), 1, "1", 1.0)]

    result = price_usage(usages, channels, config, plan="tou")

    assert set(result.by_bucket) == {"on"}
    assert result.by_bucket["on"] == pytest.approx((1.0, 0.203))


def test_price_usage_uloplan_usesulobucketandrate(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 4, 30), 1, "1", 1.0)]

    result = price_usage(usages, channels, config, plan="ulo")

    assert set(result.by_bucket) == {"overnight"}
    assert result.by_bucket["overnight"] == pytest.approx((1.0, 0.039))


# ---------------------------------------------------------------------------
# price_usage - boundary: empty / single / many, kwh 0 and >0
# ---------------------------------------------------------------------------


def test_price_usage_emptyusages_returnszeroedbreakdown(config):
    result = price_usage([], [], config)

    assert result.by_bucket == {}
    assert result.by_channel == {
        BALANCE_CHANNEL: ChannelCost(BALANCE_CHANNEL, BALANCE_CHANNEL, "balance", 0.0, 0.0)
    }


def test_price_usage_singleinterval_returnsexactcost(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 6), 1, "1", 1.0)]

    result = price_usage(usages, channels, config)

    assert (result.whole_home_kwh, result.whole_home_cost) == pytest.approx((1.0, 0.098))


def test_price_usage_zerokwhinterval_returnszerocost(config):
    channels = [_channel(1, "1", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 6), 1, "1", 0.0)]

    result = price_usage(usages, channels, config)

    assert (result.whole_home_kwh, result.whole_home_cost) == (0.0, 0.0)


def test_price_usage_manyintervals_accumulatesacrossbucketsandchannels(config):
    channels = [
        _channel(1, "1,2,3", "Mains", "mains"),
        _channel(1, "10", "Dryer", "branch"),
        _channel(1, "20", "Fridge", "branch"),
        _channel(2, "1", "EV Charger", "aux"),
    ]
    on_peak = _utc(2026, 7, 6, 17)
    off_peak = _utc(2026, 7, 6, 6)
    usages = [
        _usage(on_peak, 1, "1,2,3", 2.0),
        _usage(on_peak, 1, "10", 1.0),
        _usage(on_peak, 1, "20", 0.0),
        _usage(on_peak, 2, "1", 0.5),
        _usage(off_peak, 1, "1,2,3", 1.0),
        _usage(off_peak, 1, "10", 0.3),
    ]

    result = price_usage(usages, channels, config, plan="tou")

    assert set(result.by_bucket) == {"on", "off"}
    assert result.by_bucket["on"] == pytest.approx((3.5, 0.7105))
    assert result.by_bucket["off"] == pytest.approx((1.3, 0.1274))
    assert set(result.by_channel) == {"1,2,3", "10", "20", "1", BALANCE_CHANNEL}
    mains, branch_a, branch_b, aux, balance = (
        result.by_channel["1,2,3"],
        result.by_channel["10"],
        result.by_channel["20"],
        result.by_channel["1"],
        result.by_channel[BALANCE_CHANNEL],
    )
    assert (mains.kwh, mains.cost, mains.role) == pytest.approx((3.0, 0.504, "mains"))
    assert (branch_a.kwh, branch_a.cost, branch_a.role) == pytest.approx((1.3, 0.2324, "branch"))
    assert (branch_b.kwh, branch_b.cost, branch_b.role) == pytest.approx((0.0, 0.0, "branch"))
    assert (aux.kwh, aux.cost, aux.role) == pytest.approx((0.5, 0.1015, "aux"))
    assert (balance.kwh, balance.cost, balance.role) == pytest.approx((1.7, 0.2716, "balance"))
    assert (
        result.whole_home_kwh,
        result.whole_home_cost,
        result.balance_kwh,
        result.balance_cost,
    ) == pytest.approx((3.0, 0.504, 1.7, 0.2716))


# ---------------------------------------------------------------------------
# price_usage - channel joins and balance clamping
# ---------------------------------------------------------------------------


def test_price_usage_unknownchannel_defaultstobranchrole(config):
    channels = [_channel(1, "1,2,3", "Mains", "mains")]
    usages = [_usage(_utc(2026, 7, 6, 6), 99, "Z", 2.0)]

    result = price_usage(usages, channels, config)

    entry = result.by_channel["Z"]
    assert (entry.name, entry.role) == ("Z", "branch")
    assert (entry.kwh, entry.cost) == pytest.approx((2.0, 0.196))


def test_price_usage_branchexceedsmains_balanceclampstozero(config):
    channels = [_channel(1, "1", "Mains", "mains"), _channel(1, "2", "BigLoad", "branch")]
    ts = _utc(2026, 7, 6, 6)
    usages = [_usage(ts, 1, "1", 1.0), _usage(ts, 1, "2", 5.0)]

    result = price_usage(usages, channels, config)

    assert (result.balance_kwh, result.balance_cost) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# bill_estimate - verified worked examples and boundaries
# ---------------------------------------------------------------------------


def test_bill_estimate_workedexample2026_returnsexactcomponents(tariff):
    result = bill_estimate(
        whole_home_kwh=1000, commodity_cost=150.00, on=date(2026, 7, 1), tariff=tariff
    )

    assert (
        result.delivery_variable,
        result.delivery_fixed,
        result.subtotal,
        result.oer_credit,
        result.taxable,
        result.hst,
        result.total,
    ) == pytest.approx((23.50, 36.66, 210.16, 49.3876, 160.7724, 20.900412, 181.672812))


def test_bill_estimate_2025date_usesolderdeliveryandoer(tariff):
    result = bill_estimate(
        whole_home_kwh=1000, commodity_cost=150.00, on=date(2025, 6, 15), tariff=tariff
    )

    assert (result.delivery_fixed, result.total) == pytest.approx((35.48, 207.3724246))


@pytest.mark.parametrize(
    ("months", "expected_delivery_fixed", "expected_total"),
    [(1.0, 36.66, 181.672812), (2.0, 73.32, 213.363549)],
    ids=["months1", "months2"],
)
def test_bill_estimate_monthsboundary_scalesfixeddelivery(
    tariff, months, expected_delivery_fixed, expected_total
):
    result = bill_estimate(
        whole_home_kwh=1000,
        commodity_cost=150.00,
        on=date(2026, 7, 1),
        tariff=tariff,
        months=months,
    )

    assert (result.delivery_fixed, result.total) == pytest.approx(
        (expected_delivery_fixed, expected_total)
    )


def test_bill_estimate_zerowholehomekwh_zerovariablecomponent(tariff):
    result = bill_estimate(
        whole_home_kwh=0.0, commodity_cost=100.0, on=date(2026, 7, 1), tariff=tariff
    )

    assert (result.delivery_variable, result.total) == pytest.approx((0.0, 118.135737))


def test_bill_estimate_zerocommoditycost_stillincludesdelivery(tariff):
    result = bill_estimate(
        whole_home_kwh=500.0, commodity_cost=0.0, on=date(2026, 7, 1), tariff=tariff
    )

    assert (result.subtotal, result.total) == pytest.approx((48.41, 41.8480245))


def test_bill_estimate_nodeliverycoverage_raisescostconfigerror(tariff):
    expected_match = "No delivery row covers date 2024-12-31"

    with pytest.raises(CostConfigError, match=re.escape(expected_match)):
        bill_estimate(
            whole_home_kwh=100.0, commodity_cost=10.0, on=date(2024, 12, 31), tariff=tariff
        )


# ---------------------------------------------------------------------------
# Hypothesis property tests - pure bill math and pure pricing math (T-7)
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    whole_home_kwh=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
    commodity_cost=st.floats(min_value=0, max_value=5_000, allow_nan=False, allow_infinity=False),
)
def test_bill_estimate_anyinputs_totalequalstaxableplushst(tariff, whole_home_kwh, commodity_cost):
    result = bill_estimate(
        whole_home_kwh=whole_home_kwh,
        commodity_cost=commodity_cost,
        on=date(2026, 7, 1),
        tariff=tariff,
    )

    assert result.total == pytest.approx(result.taxable + result.hst)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    whole_home_kwh=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
    delta_kwh=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
)
def test_bill_estimate_increasingwholehomekwh_totalneverdecreases(
    tariff, whole_home_kwh, delta_kwh
):
    lower = bill_estimate(
        whole_home_kwh=whole_home_kwh, commodity_cost=100.0, on=date(2026, 7, 1), tariff=tariff
    )
    higher = bill_estimate(
        whole_home_kwh=whole_home_kwh + delta_kwh,
        commodity_cost=100.0,
        on=date(2026, 7, 1),
        tariff=tariff,
    )

    assert higher.total >= lower.total - 1e-9


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    whole_home_kwh=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
    commodity_cost=st.floats(min_value=0, max_value=5_000, allow_nan=False, allow_infinity=False),
)
def test_bill_estimate_positivesubtotal_oercreditneverexceedssubtotal(
    tariff, whole_home_kwh, commodity_cost
):
    result = bill_estimate(
        whole_home_kwh=whole_home_kwh,
        commodity_cost=commodity_cost,
        on=date(2026, 7, 1),
        tariff=tariff,
    )

    assert result.taxable <= result.subtotal + 1e-9


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    kwh_1=st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False),
    kwh_2=st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False),
    kwh_3=st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False),
)
def test_price_usage_anykwhvalues_bucketcostequalschannelcost(config, kwh_1, kwh_2, kwh_3):
    channels = [_channel(1, "1", "Mains", "mains")]
    ts = _utc(2026, 7, 6, 17)
    usages = [_usage(ts, 1, "1", kwh_1), _usage(ts, 1, "1", kwh_2), _usage(ts, 1, "1", kwh_3)]

    result = price_usage(usages, channels, config, plan="tou")

    assert result.by_bucket["on"][1] == pytest.approx(result.by_channel["1"].cost)
