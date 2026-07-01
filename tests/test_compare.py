# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.compare - COMPLETE test suite written FIRST."""

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from emporia_hydro.billing import BillingPeriod, Settings
from emporia_hydro.compare import (
    CompareError,
    ComparisonResult,
    CyclePlanComparison,
    PlanCost,
    compare_plans,
    tiered_commodity_cost,
)
from emporia_hydro.cost import bill_estimate, load_tariff
from emporia_hydro.models import Channel, IntervalUsage

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


def _settings(billing_cycle: dict[str, Any], current_plan: str = "tou") -> Settings:
    """Build a Settings fixture with a caller-controlled billing_cycle mapping."""
    return Settings(
        timezone="America/Toronto",
        current_plan=current_plan,
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
    """A single mains-role channel used by most compare_plans tests."""
    return [_channel(1, "1", "Mains", "mains")]


@pytest.fixture
def july_usages() -> list[IntervalUsage]:
    """July 2026 weekday usage: 10 kWh on-peak (local 12:00) + 5 kWh off-peak (local 02:00).

    UTC 16:00 -> local 12:00 (TOU on 0.203 / ULO mid 0.157);
    UTC 06:00 -> local 02:00 (TOU off 0.098 / ULO overnight 0.039).
    whole_home_kwh = 15.0.
    """
    return [
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 6), 1, "1", 5.0),
    ]


# ---------------------------------------------------------------------------
# tiered_commodity_cost - pure function: boundaries, both branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwh", "expected"),
    [
        (0.0, 0.0),
        (599.0, 599.0 * 0.120),
        (600.0, 600.0 * 0.120),
        (601.0, 600.0 * 0.120 + 1.0 * 0.142),
    ],
    ids=["zero", "threshold_minus_one", "threshold", "threshold_plus_one"],
)
def test_tiered_commodity_cost_summerboundary_appliestwotierformula(config, kwh, expected):
    result = tiered_commodity_cost(kwh, date(2026, 7, 1), config)

    assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    ("kwh", "expected"),
    [
        (999.0, 999.0 * 0.120),
        (1000.0, 1000.0 * 0.120),
        (1001.0, 1000.0 * 0.120 + 1.0 * 0.142),
        (1200.0, 1000.0 * 0.120 + 200.0 * 0.142),
    ],
    ids=["threshold_minus_one", "threshold", "threshold_plus_one", "well_above"],
)
def test_tiered_commodity_cost_winterboundary_appliestwotierformula(config, kwh, expected):
    result = tiered_commodity_cost(kwh, date(2026, 1, 15), config)

    assert result == pytest.approx(expected)


def test_tiered_commodity_cost_belowthreshold_usesonlytier1(config):
    result = tiered_commodity_cost(100.0, date(2026, 7, 1), config)

    assert result == pytest.approx(100.0 * 0.120)


def test_tiered_commodity_cost_abovethreshold_usesbothtiers(config):
    result = tiered_commodity_cost(700.0, date(2026, 7, 1), config)

    assert result == pytest.approx(600.0 * 0.120 + 100.0 * 0.142)


# ---------------------------------------------------------------------------
# tiered_commodity_cost - Hypothesis invariants (T-7)
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(kwh=st.floats(min_value=0, max_value=600, allow_nan=False, allow_infinity=False))
def test_tiered_commodity_cost_atorbelowthreshold_equalskwhtimestier1(config, kwh):
    result = tiered_commodity_cost(kwh, date(2026, 7, 1), config)

    assert result == pytest.approx(kwh * 0.120)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    kwh=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False),
)
def test_tiered_commodity_cost_increasingkwh_costneverdecreases(config, kwh, delta):
    lower = tiered_commodity_cost(kwh, date(2026, 7, 1), config)
    higher = tiered_commodity_cost(kwh + delta, date(2026, 7, 1), config)

    assert higher >= lower - 1e-9


# ---------------------------------------------------------------------------
# compare_plans - single cycle: per-plan costs and cheapest selection
# ---------------------------------------------------------------------------


def test_compare_plans_singlecycle_pricesallthreeplans(config, tariff, mains_channel, july_usages):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    cycle = result.cycles[0]
    assert cycle.whole_home_kwh == pytest.approx(15.0)
    assert cycle.plan_costs["tou"].commodity_cost == pytest.approx(2.52)
    assert cycle.plan_costs["ulo"].commodity_cost == pytest.approx(1.765)
    assert cycle.plan_costs["tiered"].commodity_cost == pytest.approx(1.80)


def test_compare_plans_singlecycle_fulltotalmatchesbillestimate(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    expected_ulo_full = bill_estimate(
        whole_home_kwh=15.0, commodity_cost=1.765, on=date(2026, 7, 1), tariff=tariff
    ).total
    assert result.cycles[0].plan_costs["ulo"].full_total == pytest.approx(expected_ulo_full)


def test_compare_plans_singlecycle_cheapestisulo(config, tariff, mains_channel, july_usages):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert result.cycles[0].cheapest_plan == "ulo"


def test_compare_plans_currentplantou_savingsvscurrentispositive(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}, current_plan="tou"),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert result.cycles[0].savings_vs_current == pytest.approx(2.52 - 1.765)


def test_compare_plans_currentplanalreadycheapest_savingsiszero(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}, current_plan="ulo"),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert result.cycles[0].savings_vs_current == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compare_plans - current_plan resolution and override
# ---------------------------------------------------------------------------


def test_compare_plans_currentplannone_defaultstosettingscurrentplan(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}, current_plan="tiered"),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert result.current_plan == "tiered"


def test_compare_plans_currentplanoverride_usesargumentnotsettings(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}, current_plan="tou"),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        current_plan="ulo",
    )

    assert result.current_plan == "ulo"


# ---------------------------------------------------------------------------
# compare_plans - two cycles: aggregation and overall winner
# ---------------------------------------------------------------------------


def test_compare_plans_twocycles_returnsoneperbillingperiod(config, tariff, mains_channel):
    usages = [
        _usage(_utc(2026, 6, 8, 16), 1, "1", 20.0),
        _usage(_utc(2026, 6, 8, 6), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 6), 1, "1", 5.0),
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 6, 15),
        end=date(2026, 7, 15),
    )

    assert [c.period.label for c in result.cycles] == ["2026-06", "2026-07"]
    assert (result.cycles[0].whole_home_kwh, result.cycles[1].whole_home_kwh) == pytest.approx(
        (30.0, 15.0)
    )


def test_compare_plans_twocycles_totalssumcommodityacrosscycles(config, tariff, mains_channel):
    usages = [
        _usage(_utc(2026, 6, 8, 16), 1, "1", 20.0),
        _usage(_utc(2026, 6, 8, 6), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 6), 1, "1", 5.0),
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 6, 15),
        end=date(2026, 7, 15),
    )

    assert result.totals_by_plan["tou"].commodity_cost == pytest.approx(7.56)
    assert result.totals_by_plan["ulo"].commodity_cost == pytest.approx(5.295)
    assert result.totals_by_plan["tiered"].commodity_cost == pytest.approx(5.40)


def test_compare_plans_twocycles_totalsfulltotalsumspercyclebills(config, tariff, mains_channel):
    usages = [
        _usage(_utc(2026, 6, 8, 16), 1, "1", 20.0),
        _usage(_utc(2026, 6, 8, 6), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 6), 1, "1", 5.0),
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 6, 15),
        end=date(2026, 7, 15),
    )

    june_ulo = bill_estimate(
        whole_home_kwh=30.0, commodity_cost=3.53, on=date(2026, 6, 1), tariff=tariff
    ).total
    july_ulo = bill_estimate(
        whole_home_kwh=15.0, commodity_cost=1.765, on=date(2026, 7, 1), tariff=tariff
    ).total
    assert result.totals_by_plan["ulo"].full_total == pytest.approx(june_ulo + july_ulo)


def test_compare_plans_twocycles_overallcheapestisulo(config, tariff, mains_channel):
    usages = [
        _usage(_utc(2026, 6, 8, 16), 1, "1", 20.0),
        _usage(_utc(2026, 6, 8, 6), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),
        _usage(_utc(2026, 7, 6, 6), 1, "1", 5.0),
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}, current_plan="tou"),
        start=date(2026, 6, 15),
        end=date(2026, 7, 15),
    )

    assert result.overall_cheapest_plan == "ulo"
    assert result.overall_savings_vs_current == pytest.approx(7.56 - 5.295)


# ---------------------------------------------------------------------------
# compare_plans - interval selection by local date within the period
# ---------------------------------------------------------------------------


def test_compare_plans_intervaloutsideperiod_excludedfromcycle(config, tariff, mains_channel):
    usages = [
        _usage(_utc(2026, 7, 6, 16), 1, "1", 10.0),  # July -> included in July cycle
        _usage(_utc(2026, 6, 8, 16), 1, "1", 99.0),  # June -> excluded (range is July only)
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert len(result.cycles) == 1
    assert result.cycles[0].whole_home_kwh == pytest.approx(10.0)


def test_compare_plans_emptyusages_zeroeverycycle(config, tariff, mains_channel):
    result = compare_plans(
        [],
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    cycle = result.cycles[0]
    assert cycle.whole_home_kwh == pytest.approx(0.0)
    assert cycle.plan_costs["tou"].commodity_cost == pytest.approx(0.0)
    assert cycle.plan_costs["tiered"].commodity_cost == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compare_plans - deterministic tie-break prefers tou over ulo/tiered
# ---------------------------------------------------------------------------


def test_compare_plans_toutieswithulo_cheapestpreferstou(config, tariff, mains_channel):
    # Saturday July 4 2026 local 12:00: TOU off (0.098) == ULO weekend_off (0.098).
    usages = [_usage(_utc(2026, 7, 4, 16), 1, "1", 10.0)]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    cycle = result.cycles[0]
    assert cycle.plan_costs["tou"].commodity_cost == pytest.approx(
        cycle.plan_costs["ulo"].commodity_cost
    )
    assert cycle.cheapest_plan == "tou"


# ---------------------------------------------------------------------------
# compare_plans - negative path (T-5)
# ---------------------------------------------------------------------------


def test_compare_plans_invalidcurrentplan_raisescompareerror(config, tariff, mains_channel):
    expected_match = "current_plan must be one of tou/ulo/tiered, got 'bogus'"

    with pytest.raises(CompareError, match=re.escape(expected_match)):
        compare_plans(
            [],
            mains_channel,
            config,
            tariff,
            _settings({"mode": "calendar_month"}),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            current_plan="bogus",
        )


def test_compare_plans_invalidsettingscurrentplan_raisescompareerror(
    config, tariff, mains_channel
):
    expected_match = "current_plan must be one of tou/ulo/tiered, got 'gas'"

    with pytest.raises(CompareError, match=re.escape(expected_match)):
        compare_plans(
            [],
            mains_channel,
            config,
            tariff,
            _settings({"mode": "calendar_month"}, current_plan="gas"),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )


# ---------------------------------------------------------------------------
# Result dataclass shapes (types are load-bearing for the report)
# ---------------------------------------------------------------------------


def test_compare_plans_result_exposesexpecteddataclasstypes(
    config, tariff, mains_channel, july_usages
):
    result = compare_plans(
        july_usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    assert isinstance(result, ComparisonResult)
    assert isinstance(result.cycles[0], CyclePlanComparison)
    assert isinstance(result.cycles[0].plan_costs["tou"], PlanCost)
    assert isinstance(result.cycles[0].period, BillingPeriod)
    assert result.cycles[0].plan_costs["tou"].plan == "tou"


# ---------------------------------------------------------------------------
# Hypothesis invariant on the full comparison: full-bill ranking == commodity
# ranking (delivery/OER/HST adders are identical across plans; T-7 spirit)
# ---------------------------------------------------------------------------


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    on_peak_kwh=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
    off_peak_kwh=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
)
def test_compare_plans_anyusage_cheapestcommodityisalsocheapestfulltotal(
    config, tariff, mains_channel, on_peak_kwh, off_peak_kwh
):
    usages = [
        _usage(_utc(2026, 7, 6, 16), 1, "1", on_peak_kwh),
        _usage(_utc(2026, 7, 6, 6), 1, "1", off_peak_kwh),
    ]

    result = compare_plans(
        usages,
        mains_channel,
        config,
        tariff,
        _settings({"mode": "calendar_month"}),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )

    cycle = result.cycles[0]
    cheapest = cycle.plan_costs[cycle.cheapest_plan]
    assert all(cheapest.full_total <= pc.full_total + 1e-9 for pc in cycle.plan_costs.values())
