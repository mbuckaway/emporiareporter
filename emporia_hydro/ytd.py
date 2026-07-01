# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Year-to-date whole-home and per-device cost rollup.

This module recomputes, from source usage intervals, the year-to-date (Jan 1
through a given ``on`` date) whole-home and per-device commodity cost, plus a
Jan..current calendar-month full-bill rollup. Every run recomputes from
:class:`~emporia_hydro.models.IntervalUsage` records rather than accumulating
incrementally, so the result is always consistent with the current
:mod:`~emporia_hydro.rates` and :mod:`~emporia_hydro.cost` configuration.
"""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from emporia_hydro.cost import CostBreakdown, Tariff, bill_estimate, price_usage
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.rates import RatesConfig, to_local

__all__ = ["DeviceYtd", "MonthCost", "YtdSummary", "ytd_summary"]


@dataclass(frozen=True)
class MonthCost:
    """Full-bill rollup for one calendar month within the year-to-date range."""

    month: date
    label: str
    kwh: float
    commodity_cost: float
    full_total: float


@dataclass(frozen=True)
class DeviceYtd:
    """Year-to-date kWh/cost totals for one channel (or the synthetic balance)."""

    channel_num: str
    name: str
    role: str
    kwh: float
    cost: float


@dataclass(frozen=True)
class YtdSummary:
    """Result of :func:`ytd_summary`: year-to-date whole-home and per-device cost.

    Attributes:
        year: The calendar year being summarized.
        through: The local date the range ends on (inclusive).
        plan: The pricing plan used to price every interval (``"tou"`` or
            ``"ulo"``).
        whole_home_kwh: Total whole-home kWh across ``[Jan 1, through]``.
        whole_home_commodity: Total whole-home commodity dollars across the
            same range.
        full_total: Sum of ``months[*].full_total``; the delivered-bill total
            across the year to date.
        by_device: Per-channel year-to-date totals (including the synthetic
            balance and any aux devices), sorted by cost descending then
            channel number ascending.
        months: One :class:`MonthCost` per calendar month from January through
            the month containing ``through``.
    """

    year: int
    through: date
    plan: str
    whole_home_kwh: float
    whole_home_commodity: float
    full_total: float
    by_device: list[DeviceYtd]
    months: list[MonthCost]


def _select_by_local_date(
    usages: Sequence[IntervalUsage], config: RatesConfig, start: date, end: date
) -> list[IntervalUsage]:
    """Return usages whose local date falls within ``[start, end]`` inclusive."""
    return [usage for usage in usages if start <= to_local(usage.ts, config).date() <= end]


def _to_device_ytd(breakdown: CostBreakdown) -> list[DeviceYtd]:
    """Convert a breakdown's per-channel totals into sorted DeviceYtd rows."""
    devices = [
        DeviceYtd(
            channel_num=entry.channel_num,
            name=entry.name,
            role=entry.role,
            kwh=entry.kwh,
            cost=entry.cost,
        )
        for entry in breakdown.by_channel.values()
    ]
    devices.sort(key=lambda device: (-device.cost, device.channel_num))
    return devices


def _month_end(year: int, month: int, through: date) -> date:
    """Return the last local date to include for ``year``-``month``.

    The current (through-containing) month is capped at ``through``; every
    earlier month runs to its full calendar last day.
    """
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    if (year, month) == (through.year, through.month):
        return min(last_day, through)
    return last_day


def _month_cost(
    usages: Sequence[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    plan: str,
    year: int,
    month: int,
    through: date,
) -> MonthCost:
    """Price one calendar month and estimate its full delivered-bill total."""
    month_start = date(year, month, 1)
    month_end = _month_end(year, month, through)
    month_usages = _select_by_local_date(usages, config, month_start, month_end)
    breakdown = price_usage(month_usages, channels, config, plan)
    full_total = bill_estimate(
        whole_home_kwh=breakdown.whole_home_kwh,
        commodity_cost=breakdown.whole_home_cost,
        on=month_start,
        tariff=tariff,
        months=1.0,
    ).total
    return MonthCost(
        month=month_start,
        label=f"{year:04d}-{month:02d}",
        kwh=breakdown.whole_home_kwh,
        commodity_cost=breakdown.whole_home_cost,
        full_total=full_total,
    )


def ytd_summary(
    usages: Sequence[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    *,
    on: date,
    plan: str = "tou",
) -> YtdSummary:
    """Compute the year-to-date whole-home and per-device cost rollup.

    Recomputes from source on every call: prices every usage interval whose
    local date falls in ``[Jan 1 of on.year, on]`` under ``plan``, then builds
    a full-bill rollup for each calendar month from January through the month
    containing ``on`` (the current month is truncated at ``on``).

    Args:
        usages: Interval usage records to summarize. May be empty.
        channels: Known channel metadata (see
            :func:`emporia_hydro.cost.price_usage`).
        config: The loaded rates configuration (see :mod:`emporia_hydro.rates`).
        tariff: The loaded delivery/OER/HST tariff (see :mod:`emporia_hydro.cost`).
        on: The local "as-of" date; the range ends here (inclusive).
        plan: Pricing plan to apply: ``"tou"`` or ``"ulo"``.

    Returns:
        A :class:`YtdSummary` with whole-home/per-device year-to-date totals
        and a per-month full-bill rollup.

    Raises:
        ValueError: If ``plan`` is not ``"tou"`` or ``"ulo"`` (see
            :func:`emporia_hydro.cost.price_usage`).
    """
    year_start = date(on.year, 1, 1)
    range_usages = _select_by_local_date(usages, config, year_start, on)
    breakdown = price_usage(range_usages, channels, config, plan)

    months = [
        _month_cost(usages, channels, config, tariff, plan, on.year, month, on)
        for month in range(1, on.month + 1)
    ]

    return YtdSummary(
        year=on.year,
        through=on,
        plan=plan,
        whole_home_kwh=breakdown.whole_home_kwh,
        whole_home_commodity=breakdown.whole_home_cost,
        full_total=sum(month.full_total for month in months),
        by_device=_to_device_ytd(breakdown),
        months=months,
    )
