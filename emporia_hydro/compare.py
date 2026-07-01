# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Whole-home plan optimizer: reprice the SAME usage under TOU, ULO, and Tiered.

This module takes one set of :class:`~emporia_hydro.models.IntervalUsage`
records and reprices it, per billing cycle, under all three Ontario Regulated
Price Plans -- Time-of-Use, Ultra-Low-Overnight, and Tiered -- then picks the
cheapest plan and reports the savings versus the plan the household is on today
(``Settings.current_plan``).

The delivery, Ontario Electricity Rebate, and HST adders are identical across
plans, so ranking plans by commodity dollars is the same as ranking them by the
full delivered bill. Both totals are reported (``PlanCost.commodity_cost`` and
``PlanCost.full_total``); the cheapest-by-commodity plan is also cheapest by
full bill.

TOU and ULO are priced with :func:`emporia_hydro.cost.price_usage` (they are
timestamp-based). Tiered is volume-based, so it is priced here directly from the
cycle's whole-home kWh via :func:`tiered_commodity_cost`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from emporia_hydro.billing import BillingPeriod, Settings, billing_periods
from emporia_hydro.cost import Tariff, bill_estimate, price_usage
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.rates import (
    RatesConfig,
    tiered_rates,
    tiered_threshold_kwh,
    to_local,
)

__all__ = [
    "ComparisonResult",
    "CompareError",
    "CyclePlanComparison",
    "PlanCost",
    "compare_plans",
    "tiered_commodity_cost",
]

# Plan order fixed for deterministic cheapest-plan tie-breaking: on an exact
# commodity tie, the earlier plan in this list wins.
_PLAN_ORDER = ("tou", "ulo", "tiered")


class CompareError(Exception):
    """Raised when a plan comparison cannot proceed (e.g. an unknown plan)."""


@dataclass(frozen=True)
class PlanCost:
    """Commodity and full-bill dollars for one plan over one scope."""

    plan: str
    commodity_cost: float
    full_total: float


@dataclass(frozen=True)
class CyclePlanComparison:
    """The three-plan comparison for one billing cycle."""

    period: BillingPeriod
    whole_home_kwh: float
    plan_costs: dict[str, PlanCost]
    cheapest_plan: str
    current_plan: str
    savings_vs_current: float


@dataclass(frozen=True)
class ComparisonResult:
    """The full multi-cycle comparison plus summed totals and the overall winner."""

    cycles: list[CyclePlanComparison]
    totals_by_plan: dict[str, PlanCost]
    current_plan: str
    overall_cheapest_plan: str
    overall_savings_vs_current: float


def tiered_commodity_cost(whole_home_kwh: float, on: date, config: RatesConfig) -> float:
    """Price whole-home kWh under the volume-based Tiered plan for ``on``.

    The first block up to the seasonal threshold is billed at ``tier1``; every
    kWh above the threshold is billed at ``tier2``.

    Args:
        whole_home_kwh: Total metered whole-home kWh for the billing cycle.
        on: The local date used to select the effective-dated rates and the
            seasonal threshold.
        config: The loaded rates configuration (see :mod:`emporia_hydro.rates`).

    Returns:
        The Tiered commodity cost in dollars.
    """
    threshold = tiered_threshold_kwh(on, config)
    tier1, tier2 = tiered_rates(on, config)
    first = min(whole_home_kwh, threshold)
    rest = max(0.0, whole_home_kwh - threshold)
    return first * tier1 + rest * tier2


def _cheapest_by_commodity(plan_costs: dict[str, PlanCost]) -> str:
    """Return the plan with the lowest commodity cost, tie-broken by _PLAN_ORDER."""
    return min(_PLAN_ORDER, key=lambda plan: plan_costs[plan].commodity_cost)


def _plan_cost(
    plan: str, whole_home_kwh: float, commodity: float, on: date, tariff: Tariff
) -> PlanCost:
    """Build a :class:`PlanCost` by attaching the full delivered-bill total."""
    full = bill_estimate(
        whole_home_kwh=whole_home_kwh, commodity_cost=commodity, on=on, tariff=tariff, months=1.0
    ).total
    return PlanCost(plan=plan, commodity_cost=commodity, full_total=full)


def _cycle_plan_costs(
    cycle_usages: list[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    on: date,
) -> tuple[float, dict[str, PlanCost]]:
    """Price one cycle under all three plans; returns ``(whole_home_kwh, plan_costs)``."""
    tou = price_usage(cycle_usages, channels, config, plan="tou")
    whole_home_kwh = tou.whole_home_kwh
    ulo_commodity = price_usage(cycle_usages, channels, config, plan="ulo").whole_home_cost
    tiered_commodity = tiered_commodity_cost(whole_home_kwh, on, config)
    plan_costs = {
        "tou": _plan_cost("tou", whole_home_kwh, tou.whole_home_cost, on, tariff),
        "ulo": _plan_cost("ulo", whole_home_kwh, ulo_commodity, on, tariff),
        "tiered": _plan_cost("tiered", whole_home_kwh, tiered_commodity, on, tariff),
    }
    return whole_home_kwh, plan_costs


def _compare_cycle(
    period: BillingPeriod,
    cycle_usages: list[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    current_plan: str,
) -> CyclePlanComparison:
    """Build the :class:`CyclePlanComparison` for one billing period."""
    whole_home_kwh, plan_costs = _cycle_plan_costs(
        cycle_usages, channels, config, tariff, period.start
    )
    cheapest_plan = _cheapest_by_commodity(plan_costs)
    savings = plan_costs[current_plan].commodity_cost - plan_costs[cheapest_plan].commodity_cost
    return CyclePlanComparison(
        period=period,
        whole_home_kwh=whole_home_kwh,
        plan_costs=plan_costs,
        cheapest_plan=cheapest_plan,
        current_plan=current_plan,
        savings_vs_current=savings,
    )


def _sum_totals(cycles: list[CyclePlanComparison]) -> dict[str, PlanCost]:
    """Sum each plan's commodity and full-bill dollars across every cycle."""
    return {
        plan: PlanCost(
            plan=plan,
            commodity_cost=sum(c.plan_costs[plan].commodity_cost for c in cycles),
            full_total=sum(c.plan_costs[plan].full_total for c in cycles),
        )
        for plan in _PLAN_ORDER
    }


def _usages_in_period(
    usages: Sequence[IntervalUsage], period: BillingPeriod, config: RatesConfig
) -> list[IntervalUsage]:
    """Select usages whose local date falls within ``[period.start, period.end]``."""
    return [
        usage
        for usage in usages
        if period.start <= to_local(usage.ts, config).date() <= period.end
    ]


def compare_plans(
    usages: Sequence[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    settings: Settings,
    *,
    start: date,
    end: date,
    current_plan: str | None = None,
) -> ComparisonResult:
    """Reprice usage under all three plans per billing cycle and find the cheapest.

    Args:
        usages: Interval usage records to reprice. May be empty.
        channels: Known channel metadata (see :func:`emporia_hydro.cost.price_usage`).
        config: The loaded rates configuration (see :mod:`emporia_hydro.rates`).
        tariff: The loaded delivery/OER/HST tariff (see :mod:`emporia_hydro.cost`).
        settings: The loaded :class:`~emporia_hydro.billing.Settings`; its
            ``billing_cycle`` selects the period generation and its
            ``current_plan`` frames the savings when ``current_plan`` is None.
        start: Inclusive start date of the range to compare.
        end: Inclusive end date of the range to compare.
        current_plan: The plan the household is on today. Defaults to
            ``settings.current_plan`` when None.

    Returns:
        A :class:`ComparisonResult` with a per-cycle comparison, summed
        totals per plan, and the overall cheapest plan and savings.

    Raises:
        CompareError: If the resolved ``current_plan`` is not one of
            ``"tou"``, ``"ulo"``, or ``"tiered"``.
        BillingError: If ``billing_cycle`` is misconfigured (see
            :func:`emporia_hydro.billing.billing_periods`).
    """
    resolved_plan = current_plan if current_plan is not None else settings.current_plan
    if resolved_plan not in _PLAN_ORDER:
        raise CompareError(f"current_plan must be one of tou/ulo/tiered, got {resolved_plan!r}")

    periods = billing_periods(start, end, settings)
    cycles = [
        _compare_cycle(
            period,
            _usages_in_period(usages, period, config),
            channels,
            config,
            tariff,
            resolved_plan,
        )
        for period in periods
    ]

    totals_by_plan = _sum_totals(cycles)
    overall_cheapest_plan = _cheapest_by_commodity(totals_by_plan)
    overall_savings = (
        totals_by_plan[resolved_plan].commodity_cost
        - totals_by_plan[overall_cheapest_plan].commodity_cost
    )

    return ComparisonResult(
        cycles=cycles,
        totals_by_plan=totals_by_plan,
        current_plan=resolved_plan,
        overall_cheapest_plan=overall_cheapest_plan,
        overall_savings_vs_current=overall_savings,
    )
