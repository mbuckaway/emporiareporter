# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Ontario (Guelph/Alectra) electricity pricing and time-of-use classification.

This module loads ``config/rates.json`` and provides pure, DST-aware functions
to classify a timestamp into a Time-of-Use (TOU) or Ultra-Low-Overnight (ULO)
billing bucket, resolve observed statutory holidays for a given year, and look
up the effective-dated $/kWh rates for the TOU, ULO, and Tiered plans.

Every downstream cost calculation depends on this module, so all classifiers
are pure functions of their inputs (plus the loaded, immutable config) and
raise :class:`RatesConfigError` loudly on malformed or missing configuration
rather than silently guessing.
"""

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

__all__ = [
    "RatesConfig",
    "RatesConfigError",
    "is_off_peak_day",
    "load_config",
    "observed_holidays",
    "price_row",
    "season_for",
    "tiered_rates",
    "tiered_threshold_kwh",
    "to_local",
    "tou_bucket",
    "tou_rate",
    "ulo_bucket",
    "ulo_rate",
]

_SUMMER_START = (5, 1)
_SUMMER_END = (10, 31)


class RatesConfigError(Exception):
    """Raised when ``config/rates.json`` is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class RatesConfig:
    """Immutable, parsed view of ``config/rates.json``.

    Attributes:
        timezone_name: IANA timezone name, e.g. ``"America/Toronto"``.
        zone: The resolved :class:`~zoneinfo.ZoneInfo` for ``timezone_name``.
        plan_prices: Plan name ("tou"/"ulo"/"tiered") to a tuple of effective-dated
            price rows. Each row has ``effective`` (date), ``expiry`` (date or
            None), and plan-specific rate keys (e.g. ``off``/``mid``/``on``).
        tiered_thresholds: ``{"summer_kwh": int, "winter_kwh": int}``.
        tou_schedule: Season ("summer"/"winter") to bucket name to a tuple of
            half-open ``(start_hour, end_hour)`` ranges.
        ulo_schedule: Bucket name to a tuple of half-open ``(start_hour, end_hour)``
            ranges. ULO hours are year-round (no season split).
        holiday_rules: Raw holiday rule dicts from ``config/rates.json``.
        holiday_overrides: Explicit ``YYYY-MM-DD`` holiday date overrides.
    """

    timezone_name: str
    zone: ZoneInfo
    plan_prices: dict[str, tuple[dict[str, Any], ...]]
    tiered_thresholds: dict[str, int]
    tou_schedule: dict[str, dict[str, tuple[tuple[int, int], ...]]]
    ulo_schedule: dict[str, tuple[tuple[int, int], ...]]
    holiday_rules: tuple[dict[str, Any], ...]
    holiday_overrides: tuple[str, ...]


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Return ``mapping[key]`` or raise a clear :class:`RatesConfigError`."""
    if key not in mapping:
        raise RatesConfigError(f"Missing required key '{key}' in {context} of config/rates.json")
    return mapping[key]


def _parse_price_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Parse a plan's raw price rows, converting ``effective``/``expiry`` to dates."""
    parsed: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        new_row["effective"] = date.fromisoformat(row["effective"])
        expiry = row.get("expiry")
        new_row["expiry"] = date.fromisoformat(expiry) if expiry else None
        parsed.append(new_row)
    return tuple(parsed)


def _parse_schedule_segments(
    bucket_map: dict[str, list[list[int]]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    """Convert raw ``{bucket: [[start, end], ...]}`` JSON into tuples of int pairs."""
    return {
        bucket: tuple((start, end) for start, end in ranges)
        for bucket, ranges in bucket_map.items()
    }


def load_config(config_dir: str | os.PathLike = "config") -> RatesConfig:
    """Load and parse ``rates.json`` from ``config_dir``.

    Args:
        config_dir: Directory containing ``rates.json``. Defaults to ``"config"``
            relative to the current working directory.

    Returns:
        The parsed, immutable :class:`RatesConfig`.

    Raises:
        RatesConfigError: If the file is missing, is not valid JSON, or is
            missing a required key.
    """
    path = Path(config_dir) / "rates.json"
    if not path.is_file():
        raise RatesConfigError(f"Rates config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RatesConfigError(f"Invalid JSON in rates config file {path}: {exc}") from exc

    timezone_name = _require(data, "timezone", "rates.json")
    plans = _require(data, "plans", "rates.json")
    tou_plan = _require(plans, "tou", "plans")
    ulo_plan = _require(plans, "ulo", "plans")
    tou_prices = _parse_price_rows(_require(tou_plan, "prices", "plans.tou"))
    ulo_prices = _parse_price_rows(_require(ulo_plan, "prices", "plans.ulo"))
    tiered_plan = _require(plans, "tiered", "plans")
    tiered_prices = _parse_price_rows(_require(tiered_plan, "prices", "plans.tiered"))
    thresholds = _require(tiered_plan, "thresholds", "plans.tiered")
    tiered_thresholds = {
        "summer_kwh": _require(thresholds, "summer_kwh", "plans.tiered.thresholds"),
        "winter_kwh": _require(thresholds, "winter_kwh", "plans.tiered.thresholds"),
    }

    schedule = _require(data, "schedule", "rates.json")
    tou_schedule_raw = _require(schedule, "tou", "schedule")
    tou_schedule = {
        "summer": _parse_schedule_segments(_require(tou_schedule_raw, "summer", "schedule.tou")),
        "winter": _parse_schedule_segments(_require(tou_schedule_raw, "winter", "schedule.tou")),
    }
    ulo_schedule = _parse_schedule_segments(_require(schedule, "ulo", "schedule"))

    holidays = _require(data, "holidays", "rates.json")
    holiday_rules = tuple(_require(holidays, "rules", "holidays"))
    holiday_overrides = tuple(holidays.get("overrides", []))

    return RatesConfig(
        timezone_name=timezone_name,
        zone=ZoneInfo(timezone_name),
        plan_prices={"tou": tou_prices, "ulo": ulo_prices, "tiered": tiered_prices},
        tiered_thresholds=tiered_thresholds,
        tou_schedule=tou_schedule,
        ulo_schedule=ulo_schedule,
        holiday_rules=holiday_rules,
        holiday_overrides=holiday_overrides,
    )


def to_local(ts: datetime, config: RatesConfig) -> datetime:
    """Convert an aware datetime to the configured local (DST-aware) timezone.

    Args:
        ts: A timezone-aware datetime (typically UTC).
        config: The loaded :class:`RatesConfig`.

    Returns:
        ``ts`` converted to ``config.zone``.

    Raises:
        ValueError: If ``ts`` is naive (has no timezone).
    """
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("to_local requires a timezone-aware datetime; got a naive datetime")
    return ts.astimezone(config.zone)


def season_for(d: date) -> str:
    """Classify a date as ``"summer"`` (May 1-Oct 31) or ``"winter"`` (else).

    Args:
        d: The date to classify.

    Returns:
        ``"summer"`` or ``"winter"``.
    """
    month_day = (d.month, d.day)
    if _SUMMER_START <= month_day <= _SUMMER_END:
        return "summer"
    return "winter"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the n-th occurrence of ``weekday`` (0=Mon) in month/year."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _monday_before(year: int, month: int, day: int) -> date:
    """Return the Monday strictly before ``date(year, month, day)``."""
    target = date(year, month, day)
    days_back = target.weekday() % 7
    if days_back == 0:
        days_back = 7
    return target - timedelta(days=days_back)


def _easter_sunday(year: int) -> date:
    """Return Easter Sunday via the Anonymous Gregorian (Meeus/Jones/Butcher) algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    q = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * q) // 451
    month = (h + q - 7 * m + 114) // 31
    day = ((h + q - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _resolve_holiday_date(rule: dict[str, Any], year: int) -> date:
    """Resolve one holiday rule to its statutory date for ``year``."""
    rule_type = rule.get("type")
    if rule_type == "fixed":
        return date(year, rule["month"], rule["day"])
    if rule_type == "nth_weekday":
        return _nth_weekday(year, rule["month"], rule["weekday"], rule["n"])
    if rule_type == "monday_before":
        return _monday_before(year, rule["month"], rule["day"])
    if rule_type == "good_friday":
        return _easter_sunday(year) - timedelta(days=2)
    raise RatesConfigError(f"Unknown holiday rule type: {rule_type!r}")


def observed_holidays(year: int, config: RatesConfig) -> frozenset[date]:
    """Resolve the observed off-peak holidays for ``year``.

    A holiday that falls on Saturday or Sunday is moved to the next weekday
    that is not itself a statutory holiday (the OEB "observed" day), matching
    Ontario's Boxing Day / weekend-holiday shift rule.

    Args:
        year: The calendar year to resolve holidays for.
        config: The loaded :class:`RatesConfig`.

    Returns:
        The set of observed holiday dates for ``year``, plus any explicit
        ``config`` overrides that fall within ``year``.

    Raises:
        RatesConfigError: If a holiday rule has an unrecognized ``type``.
    """
    raw_dates = [_resolve_holiday_date(rule, year) for rule in config.holiday_rules]
    raw_set = set(raw_dates)
    observed: set[date] = set()
    for raw_date in raw_dates:
        if raw_date.weekday() < 5:
            observed.add(raw_date)
            continue
        shifted = raw_date + timedelta(days=1)
        while shifted.weekday() >= 5 or shifted in raw_set:
            shifted += timedelta(days=1)
        observed.add(shifted)
    for override in config.holiday_overrides:
        override_date = date.fromisoformat(override)
        if override_date.year == year:
            observed.add(override_date)
    return frozenset(observed)


def is_off_peak_day(d: date, config: RatesConfig) -> bool:
    """Return True if ``d`` is a Saturday, Sunday, or observed holiday.

    Args:
        d: The local date to check.
        config: The loaded :class:`RatesConfig`.

    Returns:
        True if the day is entirely off-peak (TOU) / weekend-off (ULO base).
    """
    if d.weekday() >= 5:
        return True
    return d in observed_holidays(d.year, config)


def _hour_in_ranges(hour: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Return True if ``hour`` falls within any half-open ``[start, end)`` range."""
    return any(start <= hour < end for start, end in ranges)


def _bucket_for_hour(hour: int, segments: dict[str, tuple[tuple[int, int], ...]]) -> str | None:
    """Return the first bucket name whose ranges contain ``hour``, or None."""
    for bucket, ranges in segments.items():
        if _hour_in_ranges(hour, ranges):
            return bucket
    return None


def tou_bucket(ts: datetime, config: RatesConfig) -> str:
    """Classify a timestamp into a TOU bucket: ``"on"``, ``"mid"``, or ``"off"``.

    Args:
        ts: A timezone-aware datetime (typically UTC).
        config: The loaded :class:`RatesConfig`.

    Returns:
        The TOU bucket name for the local date/hour of ``ts``.

    Raises:
        RatesConfigError: If no configured schedule segment covers the local hour.
    """
    local = to_local(ts, config)
    local_date = local.date()
    if is_off_peak_day(local_date, config):
        return "off"
    season = season_for(local_date)
    bucket = _bucket_for_hour(local.hour, config.tou_schedule[season])
    if bucket is None:
        raise RatesConfigError(
            f"No TOU schedule segment covers hour {local.hour} for season {season!r}"
        )
    return bucket


def ulo_bucket(ts: datetime, config: RatesConfig) -> str:
    """Classify a timestamp into a ULO bucket.

    Overnight ``[0, 7) + [23, 24)`` always wins, even on weekends/holidays.
    Otherwise weekends/holidays are ``"weekend_off"``; weekdays split into
    ``"on"``/``"mid"``.

    Args:
        ts: A timezone-aware datetime (typically UTC).
        config: The loaded :class:`RatesConfig`.

    Returns:
        One of ``"overnight"``, ``"weekend_off"``, ``"on"``, or ``"mid"``.

    Raises:
        RatesConfigError: If no configured schedule segment covers the local hour.
    """
    local = to_local(ts, config)
    hour = local.hour
    if _hour_in_ranges(hour, config.ulo_schedule.get("overnight", ())):
        return "overnight"
    if is_off_peak_day(local.date(), config):
        return "weekend_off"
    weekday_segments = {
        "on": config.ulo_schedule.get("on", ()),
        "mid": config.ulo_schedule.get("mid", ()),
    }
    bucket = _bucket_for_hour(hour, weekday_segments)
    if bucket is None:
        raise RatesConfigError(f"No ULO schedule segment covers hour {hour}")
    return bucket


def price_row(plan: str, on: date, config: RatesConfig) -> dict[str, Any]:
    """Look up the effective-dated price row for ``plan`` on date ``on``.

    Args:
        plan: One of ``"tou"``, ``"ulo"``, ``"tiered"``.
        on: The local date the row must cover.
        config: The loaded :class:`RatesConfig`.

    Returns:
        The matching price row (the one with the latest ``effective`` date
        among rows whose ``effective`` <= ``on`` <= ``expiry`` or absent).

    Raises:
        RatesConfigError: If ``plan`` is unknown or no row covers ``on``.
    """
    rows = config.plan_prices.get(plan)
    if rows is None:
        raise RatesConfigError(f"Unknown pricing plan: {plan!r}")
    candidates = [
        row
        for row in rows
        if row["effective"] <= on and (row["expiry"] is None or on <= row["expiry"])
    ]
    if not candidates:
        raise RatesConfigError(f"No price row for plan {plan!r} covering date {on.isoformat()}")
    return max(candidates, key=lambda row: row["effective"])


def tou_rate(ts: datetime, config: RatesConfig) -> float:
    """Return the effective-dated TOU $/kWh rate for ``ts``.

    Args:
        ts: A timezone-aware datetime (typically UTC).
        config: The loaded :class:`RatesConfig`.

    Returns:
        The $/kWh rate for the TOU bucket of ``ts``.
    """
    local_date = to_local(ts, config).date()
    row = price_row("tou", local_date, config)
    return row[tou_bucket(ts, config)]


def ulo_rate(ts: datetime, config: RatesConfig) -> float:
    """Return the effective-dated ULO $/kWh rate for ``ts``.

    Args:
        ts: A timezone-aware datetime (typically UTC).
        config: The loaded :class:`RatesConfig`.

    Returns:
        The $/kWh rate for the ULO bucket of ``ts``.
    """
    local_date = to_local(ts, config).date()
    row = price_row("ulo", local_date, config)
    return row[ulo_bucket(ts, config)]


def tiered_rates(on: date, config: RatesConfig) -> tuple[float, float]:
    """Return the effective-dated ``(tier1, tier2)`` $/kWh rates for ``on``.

    Args:
        on: The local date the row must cover.
        config: The loaded :class:`RatesConfig`.

    Returns:
        A ``(tier1, tier2)`` tuple of $/kWh rates.
    """
    row = price_row("tiered", on, config)
    return (row["tier1"], row["tier2"])


def tiered_threshold_kwh(on: date, config: RatesConfig) -> int:
    """Return the Tiered plan's monthly threshold in kWh for the season of ``on``.

    Args:
        on: The local date to classify by season.
        config: The loaded :class:`RatesConfig`.

    Returns:
        ``config.tiered_thresholds["summer_kwh"]`` in summer, else ``"winter_kwh"``.
    """
    key = "summer_kwh" if season_for(on) == "summer" else "winter_kwh"
    return config.tiered_thresholds[key]
