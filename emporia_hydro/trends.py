# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Daily whole-home usage/cost trends and SVG chart rendering.

This module aggregates :class:`~emporia_hydro.models.IntervalUsage` records
(priced via :mod:`emporia_hydro.rates`) into one :class:`DayStat` per local
calendar day, computes weekday/weekend-or-holiday averages, a trailing
rolling average, and a simple least-squares trend, then renders a bar chart
of daily kWh (with a rolling-average overlay) to a headless-rendered SVG file
for a browser dashboard.

Only the Time-of-Use (TOU) pricing plan is supported for the daily bucket
split: TOU is the only plan whose classification depends solely on a
timestamp (see :func:`emporia_hydro.rates.tou_bucket`). ULO and Tiered are
intentionally out of scope here (see :mod:`emporia_hydro.cost` for ULO
commodity pricing).
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import matplotlib

matplotlib.use("Agg")
# Keep SVG text as real, selectable/scalable text (system fonts) rather than
# converting glyphs to vector paths -- required for dashboard accessibility.
matplotlib.rcParams["svg.fonttype"] = "none"

import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use("Agg"))

from emporia_hydro.models import Channel, IntervalUsage  # noqa: E402
from emporia_hydro.rates import (  # noqa: E402
    RatesConfig,
    is_off_peak_day,
    to_local,
    tou_bucket,
    tou_rate,
)

__all__ = [
    "DayStat",
    "daily_series",
    "render_daily_svg",
    "rolling_average",
    "trend",
    "weekday_weekend_summary",
]

# TrailLens palette: primary green for the daily-kWh bars, secondary blue for
# the rolling-average line. Foreground (axis/title/grid/spine) color swaps
# for a light vs. dark dashboard surface; both pass contrast against the
# transparent SVG background on either surface.
_PRIMARY_GREEN = "#4caf50"
_SECONDARY_BLUE = "#1976d2"
_LIGHT_FOREGROUND = "#212121"
_DARK_FOREGROUND = "#e0e0e0"
_GRID_ALPHA = 0.3

# "Flat" tolerance for trend(): a slope whose magnitude is at or below this
# is noise/rounding, not a real rise or fall.
_TREND_FLAT_EPSILON = 1e-9


@dataclass(frozen=True)
class DayStat:
    """Whole-home usage/cost totals for one local calendar day.

    Attributes:
        day: The local calendar date.
        kwh: Total whole-home kWh for the day.
        on_kwh: Portion of ``kwh`` billed at the TOU "on" rate.
        mid_kwh: Portion of ``kwh`` billed at the TOU "mid" rate.
        off_kwh: Portion of ``kwh`` billed at the TOU "off" rate.
        cost: Total commodity cost for the day (sum of each interval's
            ``kwh * tou_rate(ts, config)``).
    """

    day: date
    kwh: float
    on_kwh: float
    mid_kwh: float
    off_kwh: float
    cost: float


@dataclass
class _DayAccumulator:
    """Mutable per-day kWh/cost accumulator used while scanning usage intervals."""

    kwh: float = 0.0
    on_kwh: float = 0.0
    mid_kwh: float = 0.0
    off_kwh: float = 0.0
    cost: float = 0.0


def _mains_channel_nums(channels: Sequence[Channel]) -> frozenset[str]:
    """Return the ``channel_num`` set for channels with role ``"mains"``."""
    return frozenset(channel.channel_num for channel in channels if channel.role == "mains")


def _whole_home_usages(
    usages: Iterable[IntervalUsage], channels: Sequence[Channel]
) -> list[IntervalUsage]:
    """Filter usages to the whole-home (mains) series.

    Joins ``usage.channel`` to ``Channel.channel_num`` for channels whose
    role is ``"mains"``. If no mains-role channel is configured, every usage
    is treated as whole-home -- a fallback for imports that only ever
    measured the whole house (no branch/aux channels present).
    """
    usages_list = list(usages)
    mains_nums = _mains_channel_nums(channels)
    if not mains_nums:
        return usages_list
    return [usage for usage in usages_list if usage.channel in mains_nums]


def daily_series(
    usages: Iterable[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    plan: str = "tou",
) -> list[DayStat]:
    """Compute per-local-day whole-home usage/cost stats under a pricing plan.

    Args:
        usages: Interval usage records to aggregate. May be empty.
        channels: Known channel metadata, used to identify the whole-home
            (mains-role) series. See :func:`_whole_home_usages` for the
            fallback when no mains channel is configured.
        config: The loaded rates configuration (see
            :mod:`emporia_hydro.rates`).
        plan: Pricing plan to apply. Only ``"tou"`` (the default) is
            supported.

    Returns:
        One :class:`DayStat` per local calendar day that had usage, sorted
        by day ascending.

    Raises:
        ValueError: If ``plan`` is not ``"tou"``.
    """
    if plan != "tou":
        raise ValueError(f"Unsupported trend plan: {plan!r}; only 'tou' is supported")

    days: dict[date, _DayAccumulator] = {}
    for usage in _whole_home_usages(usages, channels):
        local_day = to_local(usage.ts, config).date()
        accumulator = days.setdefault(local_day, _DayAccumulator())
        bucket = tou_bucket(usage.ts, config)
        accumulator.kwh += usage.kwh
        accumulator.cost += usage.kwh * tou_rate(usage.ts, config)
        if bucket == "on":
            accumulator.on_kwh += usage.kwh
        elif bucket == "mid":
            accumulator.mid_kwh += usage.kwh
        else:
            accumulator.off_kwh += usage.kwh

    return [
        DayStat(
            day=day,
            kwh=accumulator.kwh,
            on_kwh=accumulator.on_kwh,
            mid_kwh=accumulator.mid_kwh,
            off_kwh=accumulator.off_kwh,
            cost=accumulator.cost,
        )
        for day, accumulator in sorted(days.items())
    ]


def _day_type_summary(stats: Sequence[DayStat]) -> dict[str, float | int]:
    """Average kWh/cost across ``stats``, or zeros when ``stats`` is empty."""
    count = len(stats)
    if count == 0:
        return {"avg_kwh": 0.0, "avg_cost": 0.0, "days": 0}
    return {
        "avg_kwh": sum(stat.kwh for stat in stats) / count,
        "avg_cost": sum(stat.cost for stat in stats) / count,
        "days": count,
    }


def weekday_weekend_summary(
    daily: Sequence[DayStat], config: RatesConfig
) -> dict[str, dict[str, float | int]]:
    """Average daily kWh/cost split by day-type: weekday vs. weekend/holiday.

    Args:
        daily: Per-day stats, typically from :func:`daily_series`. May be
            empty.
        config: The loaded rates configuration, used to resolve statutory
            holidays via :func:`emporia_hydro.rates.is_off_peak_day`.

    Returns:
        ``{"weekday": {...}, "weekend_holiday": {...}}`` where each group is
        ``{"avg_kwh": float, "avg_cost": float, "days": int}``. A group with
        no matching days reports all-zero averages.
    """
    weekday_stats = [stat for stat in daily if not is_off_peak_day(stat.day, config)]
    weekend_stats = [stat for stat in daily if is_off_peak_day(stat.day, config)]
    return {
        "weekday": _day_type_summary(weekday_stats),
        "weekend_holiday": _day_type_summary(weekend_stats),
    }


def rolling_average(values: Sequence[float], window: int = 7) -> list[float]:
    """Compute a trailing rolling mean, one output value per input value.

    The window at index ``i`` covers ``values[max(0, i - window + 1) : i + 1]``,
    so the first ``window - 1`` outputs are partial-window averages rather
    than ``None``/NaN placeholders.

    Args:
        values: The input series. May be empty.
        window: Trailing window size in samples. Must be at least 1.

    Returns:
        A list the same length as ``values``.

    Raises:
        ValueError: If ``window`` is less than 1.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1; got {window}")

    result: list[float] = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        segment = values[start : i + 1]
        result.append(sum(segment) / len(segment))
    return result


def trend(values: Sequence[float]) -> dict[str, float | str]:
    """Fit a least-squares linear trend over the series index.

    Args:
        values: The input series, treated as ``y`` values at equally spaced
            ``x = 0, 1, ..., len(values) - 1``.

    Returns:
        ``{"slope_per_day": float, "direction": "rising"|"falling"|"flat",
        "pct_per_day": float}``. ``pct_per_day`` is the slope expressed as a
        percentage of the series mean (0.0 when the mean is 0, to avoid
        dividing by zero). A series with fewer than 2 points has no defined
        slope and returns ``slope_per_day=0.0``, ``direction="flat"``,
        ``pct_per_day=0.0``.
    """
    n = len(values)
    if n < 2:
        return {"slope_per_day": 0.0, "direction": "flat", "pct_per_day": 0.0}

    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    numerator = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    slope = numerator / denominator

    if abs(slope) <= _TREND_FLAT_EPSILON:
        direction = "flat"
    elif slope > 0:
        direction = "rising"
    else:
        direction = "falling"

    pct_per_day = (slope / mean_y * 100.0) if mean_y != 0 else 0.0

    return {"slope_per_day": slope, "direction": direction, "pct_per_day": pct_per_day}


def _style_axes(ax: plt.Axes, foreground: str) -> None:
    """Apply the theme foreground color to ticks, spines, and grid."""
    ax.tick_params(colors=foreground)
    for spine in ax.spines.values():
        spine.set_color(foreground)
    ax.grid(True, color=foreground, alpha=_GRID_ALPHA)


def _render_empty_placeholder(path: str | os.PathLike, title: str, foreground: str) -> None:
    """Write a "No data" placeholder SVG when there is nothing to chart."""
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.text(
        0.5,
        0.5,
        "No data",
        ha="center",
        va="center",
        transform=ax.transAxes,
        color=foreground,
        fontsize=14,
    )
    ax.set_title(title, color=foreground)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color(foreground)
    fig.savefig(path, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)


def render_daily_svg(
    daily: Sequence[DayStat],
    path: str | os.PathLike,
    *,
    window: int = 7,
    title: str = "Daily usage & cost",
    dark: bool = False,
) -> None:
    """Render a bar chart of daily kWh with a rolling-average line to an SVG.

    The chart has a transparent background (no facecolor) so it composites
    correctly over a browser dashboard's light or dark surface, and its
    text remains real/selectable SVG text rather than vector-outlined paths.

    Always writes a file at ``path``, even for an empty ``daily`` series (a
    placeholder "No data" chart), so callers never have to special-case an
    empty result before rendering.

    Args:
        daily: Per-day stats to plot, typically from :func:`daily_series`.
            May be empty.
        path: Destination SVG file path.
        window: Rolling-average window in days (see :func:`rolling_average`).
        title: Chart title.
        dark: When True, use light-on-dark foreground colors suited to a
            dark dashboard theme; otherwise use dark-on-light foreground.

    Raises:
        ValueError: If ``window`` is less than 1 (see :func:`rolling_average`).
    """
    foreground = _DARK_FOREGROUND if dark else _LIGHT_FOREGROUND

    if not daily:
        _render_empty_placeholder(path, title, foreground)
        return

    days = [stat.day for stat in daily]
    kwh = [stat.kwh for stat in daily]
    # Validate/compute before creating the figure so an invalid window never
    # leaves an unclosed matplotlib figure behind.
    averages = rolling_average(kwh, window=window)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(days, kwh, color=_PRIMARY_GREEN, label="Daily kWh")
    ax.plot(days, averages, color=_SECONDARY_BLUE, linewidth=2, label=f"{window}-day avg")
    ax.set_xlabel("Date", color=foreground)
    ax.set_ylabel("kWh", color=foreground)
    ax.set_title(title, color=foreground)
    _style_axes(ax, foreground)
    legend = ax.legend()
    for text in legend.get_texts():
        text.set_color(foreground)
    fig.autofmt_xdate()

    fig.savefig(path, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)
