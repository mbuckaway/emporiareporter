# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Electricity cost calculation: commodity pricing, bucket splits, bill estimate.

This module prices :class:`~emporia_hydro.models.IntervalUsage` records against
the fixed-rate (TOU/ULO) plans in ``config/rates.json`` (see
:mod:`emporia_hydro.rates`) to produce per-device/per-channel commodity cost and
a per-bucket split. It separately loads ``config/tariff.json`` (Alectra delivery
adders, the Ontario Electricity Rebate, and HST) to produce an OPTIONAL full
delivered-bill estimate on top of a priced commodity total.

Tiered pricing is volume-based (depends on cumulative monthly kWh, not a
timestamp bucket) and is intentionally out of scope for :func:`price_usage`.
"""

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from emporia_hydro.models import BALANCE_CHANNEL, Channel, IntervalUsage
from emporia_hydro.rates import RatesConfig, tou_bucket, tou_rate, ulo_bucket, ulo_rate

__all__ = [
    "BillEstimate",
    "ChannelCost",
    "CostBreakdown",
    "CostConfigError",
    "Tariff",
    "bill_estimate",
    "delivery_row",
    "load_tariff",
    "oer_rate",
    "price_usage",
]


class CostConfigError(Exception):
    """Raised when ``config/tariff.json`` is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class Tariff:
    """Immutable, parsed view of ``config/tariff.json``.

    Attributes:
        delivery: Effective-dated delivery-charge rows (each has ``effective``,
            ``expiry``, ``fixed_monthly``, ``smart_metering_monthly``,
            ``sss_monthly``, ``variable_per_kwh``).
        oer: Effective-dated Ontario Electricity Rebate rows (each has
            ``effective``, ``expiry``, ``rate``).
        hst: The HST rate applied to the post-OER taxable amount (e.g. ``0.13``).
    """

    delivery: tuple[dict[str, Any], ...]
    oer: tuple[dict[str, Any], ...]
    hst: float


@dataclass(frozen=True)
class ChannelCost:
    """Commodity kWh/cost totals for one channel (or the synthetic balance)."""

    channel_num: str
    name: str
    role: str
    kwh: float
    cost: float


@dataclass(frozen=True)
class CostBreakdown:
    """Result of :func:`price_usage`: per-bucket and per-channel commodity cost.

    Attributes:
        by_bucket: Bucket name (e.g. ``"on"``, ``"mid"``, ``"off"``) to a
            ``(kwh, cost)`` tuple summed across every interval in that bucket.
        by_channel: Channel number to its :class:`ChannelCost`, including a
            synthetic :data:`~emporia_hydro.models.BALANCE_CHANNEL` entry for
            whole-home usage not attributed to a monitored branch circuit.
        whole_home_kwh: Total kWh across channels with role ``"mains"``.
        whole_home_cost: Total commodity cost across channels with role
            ``"mains"``.
        balance_kwh: ``whole_home_kwh`` minus the sum of ``"branch"`` role
            channels, clamped to ``0.0``.
        balance_cost: ``whole_home_cost`` minus the sum of ``"branch"`` role
            channels' cost, clamped to ``0.0``.
    """

    by_bucket: dict[str, tuple[float, float]]
    by_channel: dict[str, ChannelCost]
    whole_home_kwh: float
    whole_home_cost: float
    balance_kwh: float
    balance_cost: float


@dataclass(frozen=True)
class BillEstimate:
    """Result of :func:`bill_estimate`: every dollar component of a full bill."""

    delivery_variable: float
    delivery_fixed: float
    subtotal: float
    oer_credit: float
    taxable: float
    hst: float
    total: float


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Return ``mapping[key]`` or raise a clear :class:`CostConfigError`."""
    if key not in mapping:
        raise CostConfigError(f"Missing required key '{key}' in {context}")
    return mapping[key]


def _parse_effective_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Parse effective-dated rows, converting effective/expiry strings to dates."""
    parsed: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        new_row["effective"] = date.fromisoformat(row["effective"])
        expiry = row.get("expiry")
        new_row["expiry"] = date.fromisoformat(expiry) if expiry else None
        parsed.append(new_row)
    return tuple(parsed)


def load_tariff(config_dir: str | os.PathLike = "config") -> Tariff:
    """Load and parse ``tariff.json`` from ``config_dir``.

    Args:
        config_dir: Directory containing ``tariff.json``. Defaults to
            ``"config"`` relative to the current working directory.

    Returns:
        The parsed, immutable :class:`Tariff`.

    Raises:
        CostConfigError: If the file is missing, is not valid JSON, or is
            missing a required key.
    """
    path = Path(config_dir) / "tariff.json"
    if not path.is_file():
        raise CostConfigError(f"Tariff config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CostConfigError(f"Invalid JSON in tariff config file {path}: {exc}") from exc

    delivery = _parse_effective_rows(_require(data, "delivery", "tariff.json"))
    oer = _parse_effective_rows(_require(data, "oer", "tariff.json"))
    hst = _require(data, "hst", "tariff.json")

    return Tariff(delivery=delivery, oer=oer, hst=hst)


def _effective_row(rows: tuple[dict[str, Any], ...], on: date, context: str) -> dict[str, Any]:
    """Return the latest row in ``rows`` whose effective/expiry window covers ``on``."""
    candidates = [
        row
        for row in rows
        if row["effective"] <= on and (row["expiry"] is None or on <= row["expiry"])
    ]
    if not candidates:
        raise CostConfigError(f"No {context} row covers date {on.isoformat()}")
    return max(candidates, key=lambda row: row["effective"])


def delivery_row(on: date, tariff: Tariff) -> dict[str, Any]:
    """Return the effective-dated delivery-charge row covering ``on``.

    Args:
        on: The local date the row must cover.
        tariff: The loaded :class:`Tariff`.

    Returns:
        The matching delivery row (``fixed_monthly``, ``smart_metering_monthly``,
        ``sss_monthly``, ``variable_per_kwh``, plus metadata keys).

    Raises:
        CostConfigError: If no delivery row covers ``on``.
    """
    return _effective_row(tariff.delivery, on, "delivery")


def oer_rate(on: date, tariff: Tariff) -> float:
    """Return the effective-dated Ontario Electricity Rebate rate for ``on``.

    Args:
        on: The local date the rate must cover.
        tariff: The loaded :class:`Tariff`.

    Returns:
        The OER credit rate (e.g. ``0.235`` for 23.5%).

    Raises:
        CostConfigError: If no OER row covers ``on``.
    """
    row = _effective_row(tariff.oer, on, "oer")
    return row["rate"]


@dataclass(frozen=True)
class _PlanFuncs:
    """Bucket-classifier and rate-lookup function pair for one pricing plan."""

    bucket: Callable[[datetime, RatesConfig], str]
    rate: Callable[[datetime, RatesConfig], float]


_PLAN_FUNCS: dict[str, _PlanFuncs] = {
    "tou": _PlanFuncs(bucket=tou_bucket, rate=tou_rate),
    "ulo": _PlanFuncs(bucket=ulo_bucket, rate=ulo_rate),
}


def _plan_functions(plan: str) -> _PlanFuncs:
    """Return the bucket/rate function pair for ``plan``, or raise ValueError."""
    funcs = _PLAN_FUNCS.get(plan)
    if funcs is None:
        raise ValueError(f"Unknown pricing plan: {plan!r}; must be 'tou' or 'ulo'")
    return funcs


def _price_interval(
    usage: IntervalUsage, config: RatesConfig, funcs: _PlanFuncs
) -> tuple[str, float]:
    """Classify and cost one interval: returns ``(bucket, dollar_cost)``."""
    bucket = funcs.bucket(usage.ts, config)
    rate = funcs.rate(usage.ts, config)
    return bucket, usage.kwh * rate


@dataclass
class _RunningTotal:
    """Mutable kWh/cost accumulator used while scanning usage intervals."""

    kwh: float = 0.0
    cost: float = 0.0

    def add(self, kwh: float, cost: float) -> None:
        """Add one interval's kWh and cost to the running total."""
        self.kwh += kwh
        self.cost += cost


def _index_channels(channels: Sequence[Channel]) -> dict[tuple[int, str], Channel]:
    """Build a ``(device_gid, channel_num) -> Channel`` lookup for metadata joins."""
    return {(channel.device_gid, channel.channel_num): channel for channel in channels}


def _channel_meta(
    usage: IntervalUsage, channel_index: dict[tuple[int, str], Channel]
) -> tuple[str, str]:
    """Return ``(name, role)`` for ``usage``'s channel; unknown channels are branch."""
    channel = channel_index.get((usage.device_gid, usage.channel))
    if channel is None:
        return usage.channel, "branch"
    return channel.name, channel.role


def _sum_role(by_channel: dict[str, ChannelCost], role: str) -> tuple[float, float]:
    """Sum kWh/cost across every ``by_channel`` entry matching ``role``."""
    matches = [entry for entry in by_channel.values() if entry.role == role]
    return sum(entry.kwh for entry in matches), sum(entry.cost for entry in matches)


def price_usage(
    usages: Iterable[IntervalUsage],
    channels: Sequence[Channel],
    config: RatesConfig,
    plan: str = "tou",
) -> CostBreakdown:
    """Price a set of usage intervals under a fixed-rate plan (TOU or ULO).

    Args:
        usages: Interval usage records to price. May be empty.
        channels: Known channel metadata used to join device/channel to
            name/role. A usage interval whose ``(device_gid, channel)`` is not
            found defaults to role ``"branch"`` with name equal to its raw
            channel number.
        config: The loaded rates configuration (see :mod:`emporia_hydro.rates`).
        plan: Pricing plan to apply: ``"tou"`` or ``"ulo"``. Tiered is
            volume-based and is not supported here.

    Returns:
        A :class:`CostBreakdown` with per-bucket and per-channel totals, plus
        whole-home and unmonitored-balance totals.

    Raises:
        ValueError: If ``plan`` is not ``"tou"`` or ``"ulo"``.
    """
    funcs = _plan_functions(plan)
    channel_index = _index_channels(channels)

    bucket_totals: dict[str, _RunningTotal] = {}
    channel_totals: dict[str, _RunningTotal] = {}
    channel_meta: dict[str, tuple[str, str]] = {}

    for usage in usages:
        bucket, cost = _price_interval(usage, config, funcs)
        bucket_totals.setdefault(bucket, _RunningTotal()).add(usage.kwh, cost)

        channel_meta[usage.channel] = _channel_meta(usage, channel_index)
        channel_totals.setdefault(usage.channel, _RunningTotal()).add(usage.kwh, cost)

    by_bucket = {bucket: (total.kwh, total.cost) for bucket, total in bucket_totals.items()}
    by_channel = {
        num: ChannelCost(
            channel_num=num,
            name=channel_meta[num][0],
            role=channel_meta[num][1],
            kwh=total.kwh,
            cost=total.cost,
        )
        for num, total in channel_totals.items()
    }

    whole_home_kwh, whole_home_cost = _sum_role(by_channel, "mains")
    branch_kwh, branch_cost = _sum_role(by_channel, "branch")
    # Unmonitored circuits normally make mains > branches; clamp any negative
    # remainder (measurement noise/rounding) to zero instead of a nonsensical
    # negative balance.
    balance_kwh = max(0.0, whole_home_kwh - branch_kwh)
    balance_cost = max(0.0, whole_home_cost - branch_cost)
    by_channel[BALANCE_CHANNEL] = ChannelCost(
        channel_num=BALANCE_CHANNEL,
        name=BALANCE_CHANNEL,
        role="balance",
        kwh=balance_kwh,
        cost=balance_cost,
    )

    return CostBreakdown(
        by_bucket=by_bucket,
        by_channel=by_channel,
        whole_home_kwh=whole_home_kwh,
        whole_home_cost=whole_home_cost,
        balance_kwh=balance_kwh,
        balance_cost=balance_cost,
    )


def bill_estimate(
    *,
    whole_home_kwh: float,
    commodity_cost: float,
    on: date,
    tariff: Tariff,
    months: float = 1.0,
) -> BillEstimate:
    """Estimate the full delivered electricity bill for one billing period.

    Combines the priced commodity cost with Alectra delivery adders, applies
    the Ontario Electricity Rebate (OER) credit, then HST. Global Adjustment is
    already embedded in the commodity price (see :mod:`emporia_hydro.rates`)
    and is intentionally not added again here.

    Args:
        whole_home_kwh: Total metered whole-home kWh for the period.
        commodity_cost: Total priced commodity dollars for the period (see
            :attr:`CostBreakdown.whole_home_cost`).
        on: The local date used to select effective-dated delivery/OER rows.
        tariff: The loaded :class:`Tariff`.
        months: Number of billing months the fixed delivery charges cover.

    Returns:
        A :class:`BillEstimate` with every intermediate dollar component.

    Raises:
        CostConfigError: If no delivery or OER row covers ``on``.
    """
    row = delivery_row(on, tariff)
    delivery_variable = whole_home_kwh * row["variable_per_kwh"]
    delivery_fixed = (
        row["fixed_monthly"] + row["smart_metering_monthly"] + row["sss_monthly"]
    ) * months
    subtotal = commodity_cost + delivery_variable + delivery_fixed
    oer_credit = oer_rate(on, tariff) * subtotal
    taxable = subtotal - oer_credit
    hst = tariff.hst * taxable
    total = taxable + hst

    return BillEstimate(
        delivery_variable=delivery_variable,
        delivery_fixed=delivery_fixed,
        subtotal=subtotal,
        oer_credit=oer_credit,
        taxable=taxable,
        hst=hst,
        total=total,
    )
