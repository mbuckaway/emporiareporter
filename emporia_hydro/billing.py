# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Alectra billing-cycle modeling and in-progress bill prediction.

This module derives the sequence of billing periods implied by
``config/settings.json``'s ``billing_cycle`` setting (calendar month, a fixed
meter-read anchor day, or an explicit list of date ranges), and predicts the
total bill for a billing cycle that is still in progress by combining actual
metered usage to date with a day-type-aware (weekday vs. weekend/holiday)
projection of the remaining days.

``billing_periods``/``BillingPeriod``/``current_period`` are the reusable
billing-period API that ``compare`` also consumes; keep them public and free
of prediction-specific concerns.
"""

import calendar
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from emporia_hydro.cost import BillEstimate, Tariff, bill_estimate, price_usage
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.rates import RatesConfig, is_off_peak_day, to_local

__all__ = [
    "BillPrediction",
    "BillingError",
    "BillingPeriod",
    "Settings",
    "billing_periods",
    "current_period",
    "load_settings",
    "predict_bill",
]

_MIN_ANCHOR_DAY = 1
_MAX_ANCHOR_DAY = 28


class BillingError(Exception):
    """Raised when ``config/settings.json`` is missing/malformed, or a
    billing-cycle computation cannot proceed (unknown mode, no covering
    period, invalid anchor day, etc.)."""


@dataclass(frozen=True)
class Settings:
    """Immutable, parsed view of ``config/settings.json``.

    Attributes:
        timezone: IANA timezone name, e.g. ``"America/Toronto"``.
        current_plan: The pricing plan the account is currently billed on
            (``"tou"``, ``"ulo"``, or ``"tiered"``), used to frame savings.
        billing_cycle: Raw ``billing_cycle`` mapping; see :func:`billing_periods`
            for the supported ``mode`` values and their extra keys.
        server: Raw local-dashboard server settings (``host``, ``port``).
        output: Raw output-directory settings (``reports_dir``, ``data_dir``).
    """

    timezone: str
    current_plan: str
    billing_cycle: dict[str, Any]
    server: dict[str, Any]
    output: dict[str, Any]


@dataclass(frozen=True)
class BillingPeriod:
    """One inclusive-dated billing period: ``[start, end]``."""

    start: date
    end: date
    label: str


@dataclass(frozen=True)
class BillPrediction:
    """Result of :func:`predict_bill`: actual-to-date usage plus a projected
    full-cycle estimate for a billing period still in progress.

    Attributes:
        period: The billing period being predicted.
        plan: The pricing plan used to price both actual and projected usage.
        days_elapsed: Count of local days in ``[period.start, on]`` that had
            usage data.
        days_remaining: Count of local days in ``(on, period.end]``.
        to_date_kwh: Actual whole-home kWh summed over elapsed days.
        to_date_energy_cost: Actual commodity dollars summed over elapsed days.
        projected_kwh: Projected whole-home kWh for the remaining days.
        projected_energy_cost: Projected commodity dollars for the remaining days.
        predicted_kwh: ``to_date_kwh + projected_kwh``.
        predicted_energy_cost: ``to_date_energy_cost + projected_energy_cost``.
        predicted_full: The full delivered-bill estimate for ``predicted_kwh``.
        per_day_type: Day type (``"weekday"``/``"weekend_holiday"``) to the
            ``(avg_kwh, avg_cost)`` observed across elapsed days of that type.
            A type absent from elapsed data is absent from this mapping.
    """

    period: BillingPeriod
    plan: str
    days_elapsed: int
    days_remaining: int
    to_date_kwh: float
    to_date_energy_cost: float
    projected_kwh: float
    projected_energy_cost: float
    predicted_kwh: float
    predicted_energy_cost: float
    predicted_full: BillEstimate
    per_day_type: dict[str, tuple[float, float]]


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Return ``mapping[key]`` or raise a clear :class:`BillingError`."""
    if key not in mapping:
        raise BillingError(f"Missing required key '{key}' in {context}")
    return mapping[key]


def load_settings(config_dir: str | os.PathLike = "config") -> Settings:
    """Load and parse ``settings.json`` from ``config_dir``.

    Args:
        config_dir: Directory containing ``settings.json``. Defaults to
            ``"config"`` relative to the current working directory.

    Returns:
        The parsed, immutable :class:`Settings`.

    Raises:
        BillingError: If the file is missing, is not valid JSON, or is
            missing a required key.
    """
    path = Path(config_dir) / "settings.json"
    if not path.is_file():
        raise BillingError(f"Settings config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BillingError(f"Invalid JSON in settings config file {path}: {exc}") from exc

    return Settings(
        timezone=_require(data, "timezone", "settings.json"),
        current_plan=_require(data, "current_plan", "settings.json"),
        billing_cycle=_require(data, "billing_cycle", "settings.json"),
        server=_require(data, "server", "settings.json"),
        output=_require(data, "output", "settings.json"),
    )


def _last_day_of_month(year: int, month: int) -> date:
    """Return the last calendar date of ``year``-``month``."""
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(d: date, delta: int) -> date:
    """Return ``d`` shifted by ``delta`` whole months, keeping the same day.

    Only used for anchor-day arithmetic, where the day-of-month is always
    1-28 and therefore valid in every target month (no Feb-29/30/31 overflow).
    """
    month_index = d.month - 1 + delta
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, d.day)


def _calendar_month_periods(start: date, end: date) -> list[BillingPeriod]:
    """Generate one full-calendar-month period per month from ``start`` to ``end``."""
    periods: list[BillingPeriod] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        period_start = date(year, month, 1)
        period_end = _last_day_of_month(year, month)
        periods.append(BillingPeriod(period_start, period_end, f"{year:04d}-{month:02d}"))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return periods


def _anchor_day(settings: Settings) -> int:
    """Return the validated ``billing_cycle['day']`` anchor for ``anchor_day`` mode."""
    day = settings.billing_cycle.get("day")
    if not isinstance(day, int) or not (_MIN_ANCHOR_DAY <= day <= _MAX_ANCHOR_DAY):
        raise BillingError(
            "billing_cycle mode 'anchor_day' requires 'day' to be an integer "
            f"{_MIN_ANCHOR_DAY}-{_MAX_ANCHOR_DAY}, got {day!r}"
        )
    return day


def _anchor_period_start(d: date, day: int) -> date:
    """Return the start date of the anchor-day period that contains ``d``."""
    if d.day >= day:
        return date(d.year, d.month, day)
    return _add_months(date(d.year, d.month, day), -1)


def _anchor_day_periods(start: date, end: date, day: int) -> list[BillingPeriod]:
    """Generate consecutive anchor-day periods overlapping ``[start, end]``."""
    periods: list[BillingPeriod] = []
    period_start = _anchor_period_start(start, day)
    while period_start <= end:
        period_end = _add_months(period_start, 1) - timedelta(days=1)
        periods.append(BillingPeriod(period_start, period_end, period_start.isoformat()))
        period_start = _add_months(period_start, 1)
    return periods


def _explicit_periods(start: date, end: date, settings: Settings) -> list[BillingPeriod]:
    """Return the configured explicit periods overlapping ``[start, end]``."""
    raw_periods = settings.billing_cycle.get("periods")
    if not raw_periods:
        raise BillingError("billing_cycle mode 'explicit' requires a non-empty 'periods' list")
    periods: list[BillingPeriod] = []
    for raw_start, raw_end in raw_periods:
        period_start = date.fromisoformat(raw_start)
        period_end = date.fromisoformat(raw_end)
        if period_start <= end and period_end >= start:
            periods.append(BillingPeriod(period_start, period_end, period_start.isoformat()))
    return periods


def billing_periods(start: date, end: date, settings: Settings) -> list[BillingPeriod]:
    """Generate the sequence of billing periods covering ``[start, end]``.

    Args:
        start: Inclusive start date of the range to cover.
        end: Inclusive end date of the range to cover.
        settings: The loaded :class:`Settings`, whose ``billing_cycle["mode"]``
            selects the generation strategy:

            - ``"calendar_month"`` (default): one period per calendar month.
            - ``"anchor_day"``: periods run ``[day, next_month_day - 1]`` for
              the configured ``billing_cycle["day"]`` (1-28).
            - ``"explicit"``: the configured ``billing_cycle["periods"]`` list
              of ``[start_iso, end_iso]`` pairs, filtered to those overlapping
              ``[start, end]``.

    Returns:
        The matching periods in chronological order. May be empty (e.g. an
        ``"explicit"`` schedule with no period overlapping the range).

    Raises:
        BillingError: If ``billing_cycle["mode"]`` is unrecognized, mode
            ``"explicit"`` has no configured periods, or mode ``"anchor_day"``
            has a missing/invalid ``day``.
    """
    mode = settings.billing_cycle.get("mode", "calendar_month")
    if mode == "calendar_month":
        return _calendar_month_periods(start, end)
    if mode == "anchor_day":
        return _anchor_day_periods(start, end, _anchor_day(settings))
    if mode == "explicit":
        return _explicit_periods(start, end, settings)
    raise BillingError(f"Unknown billing_cycle mode: {mode!r}")


def current_period(on: date, settings: Settings) -> BillingPeriod:
    """Return the billing period that contains ``on``.

    Args:
        on: The local date to locate a period for.
        settings: The loaded :class:`Settings`.

    Returns:
        The single :class:`BillingPeriod` covering ``on``.

    Raises:
        BillingError: If no configured period covers ``on`` (only reachable
            with an ``"explicit"`` schedule that has a gap).
    """
    periods = billing_periods(on, on, settings)
    if not periods:
        raise BillingError(f"No billing period covers date {on.isoformat()}")
    return periods[0]


def _group_by_local_date(
    usages: Sequence[IntervalUsage], config: RatesConfig
) -> dict[date, list[IntervalUsage]]:
    """Group usage intervals by their local calendar date."""
    grouped: dict[date, list[IntervalUsage]] = {}
    for usage in usages:
        local_date = to_local(usage.ts, config).date()
        grouped.setdefault(local_date, []).append(usage)
    return grouped


def _day_type(d: date, config: RatesConfig) -> str:
    """Classify ``d`` as ``"weekend_holiday"`` or ``"weekday"``."""
    return "weekend_holiday" if is_off_peak_day(d, config) else "weekday"


@dataclass
class _DayTypeTotal:
    """Mutable per-day-type kWh/cost accumulator used to compute daily averages."""

    kwh: float = 0.0
    cost: float = 0.0
    days: int = 0

    def add(self, kwh: float, cost: float) -> None:
        """Add one day's whole-home kWh and cost to the running total."""
        self.kwh += kwh
        self.cost += cost
        self.days += 1

    def average(self) -> tuple[float, float]:
        """Return the ``(avg_kwh, avg_cost)`` per day across days added so far."""
        return (self.kwh / self.days, self.cost / self.days)


def _price_elapsed_days(
    elapsed_dates: list[date],
    by_local_date: dict[date, list[IntervalUsage]],
    channels: Sequence[Channel],
    config: RatesConfig,
    plan: str,
) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """Price each elapsed day and return actual totals plus per-day-type averages."""
    day_type_totals: dict[str, _DayTypeTotal] = {}
    to_date_kwh = 0.0
    to_date_energy_cost = 0.0
    for elapsed_date in elapsed_dates:
        breakdown = price_usage(by_local_date[elapsed_date], channels, config, plan)
        to_date_kwh += breakdown.whole_home_kwh
        to_date_energy_cost += breakdown.whole_home_cost
        day_type_totals.setdefault(_day_type(elapsed_date, config), _DayTypeTotal()).add(
            breakdown.whole_home_kwh, breakdown.whole_home_cost
        )
    per_day_type = {day_type: total.average() for day_type, total in day_type_totals.items()}
    return to_date_kwh, to_date_energy_cost, per_day_type


def _project_remaining_days(
    on: date,
    days_remaining: int,
    per_day_type: dict[str, tuple[float, float]],
    overall_average: tuple[float, float],
    config: RatesConfig,
) -> tuple[float, float]:
    """Sum the day-type-aware projected kWh/cost across each remaining day."""
    projected_kwh = 0.0
    projected_energy_cost = 0.0
    for offset in range(1, days_remaining + 1):
        future_day_type = _day_type(on + timedelta(days=offset), config)
        avg_kwh, avg_cost = per_day_type.get(future_day_type, overall_average)
        projected_kwh += avg_kwh
        projected_energy_cost += avg_cost
    return projected_kwh, projected_energy_cost


def predict_bill(
    usages: Sequence[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    tariff: Tariff,
    settings: Settings,
    *,
    on: date,
    plan: str = "tou",
) -> BillPrediction:
    """Predict the full-cycle bill for the billing period containing ``on``.

    Actual usage for every local day from the period start through ``on``
    (inclusive) that has data is priced directly. Each remaining day (``on``+1
    through the period end) is projected using the average kWh/cost of
    already-elapsed days of the same day type (``"weekday"`` or
    ``"weekend_holiday"``), falling back to the overall elapsed-day average
    when no elapsed day of that type exists yet. If there are no elapsed days
    at all, every projected/predicted total is ``0.0``.

    Args:
        usages: Interval usage records for (at least) the elapsed portion of
            the current billing period. May be empty.
        channels: Known channel metadata (see :func:`emporia_hydro.cost.price_usage`).
        config: The loaded rates configuration (see :mod:`emporia_hydro.rates`).
        tariff: The loaded delivery/OER/HST tariff (see :mod:`emporia_hydro.cost`).
        settings: The loaded :class:`Settings`.
        on: The local "as-of" date to predict from.
        plan: Pricing plan to apply: ``"tou"`` or ``"ulo"``.

    Returns:
        A :class:`BillPrediction` with actual-to-date totals, the projected
        remainder, and the combined full-cycle estimate.

    Raises:
        BillingError: If no billing period covers ``on`` (see :func:`current_period`).
    """
    period = current_period(on, settings)
    by_local_date = _group_by_local_date(usages, config)
    elapsed_dates = sorted(d for d in by_local_date if period.start <= d <= on)
    days_elapsed = len(elapsed_dates)
    days_remaining = max(0, (period.end - on).days)

    to_date_kwh, to_date_energy_cost, per_day_type = _price_elapsed_days(
        elapsed_dates, by_local_date, channels, config, plan
    )
    overall_average = (
        (to_date_kwh / days_elapsed, to_date_energy_cost / days_elapsed)
        if days_elapsed
        else (0.0, 0.0)
    )
    projected_kwh, projected_energy_cost = _project_remaining_days(
        on, days_remaining, per_day_type, overall_average, config
    )

    predicted_kwh = to_date_kwh + projected_kwh
    predicted_energy_cost = to_date_energy_cost + projected_energy_cost

    return BillPrediction(
        period=period,
        plan=plan,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        to_date_kwh=to_date_kwh,
        to_date_energy_cost=to_date_energy_cost,
        projected_kwh=projected_kwh,
        projected_energy_cost=projected_energy_cost,
        predicted_kwh=predicted_kwh,
        predicted_energy_cost=predicted_energy_cost,
        predicted_full=bill_estimate(
            whole_home_kwh=predicted_kwh,
            commodity_cost=predicted_energy_cost,
            on=period.start,
            tariff=tariff,
            months=1.0,
        ),
        per_day_type=per_day_type,
    )
