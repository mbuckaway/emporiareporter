# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""The only place stored ``config/rates.json`` rows are read, edited, or fetched.

``rates_show``, ``rates_set``, and ``rates_import`` are fully OFFLINE: they only
ever read/write the local ``config/rates.json`` file. ``rates_update`` and
``rates_check`` are OPT-IN network operations, but this module never performs
network I/O itself -- callers inject a ``fetcher`` callable that returns a
rates-file-shaped dict. This keeps the module free of any hard-coded OEB/Alectra
scraping endpoint; the CLI supplies the real fetcher later.
"""

import dataclasses
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from emporia_hydro.rates import (
    RatesConfig,
    RatesConfigError,
    load_config,
    price_row,
    tiered_threshold_kwh,
    tou_bucket,
    tou_rate,
    ulo_bucket,
    ulo_rate,
)

__all__ = [
    "ActiveBucket",
    "CheckResult",
    "RatesIoError",
    "RowDiff",
    "ShowResult",
    "UpdateDiff",
    "rates_check",
    "rates_import",
    "rates_set",
    "rates_show",
    "rates_update",
]

_PLAN_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "tou": ("off", "mid", "on"),
    "ulo": ("overnight", "weekend_off", "mid", "on"),
    "tiered": ("tier1", "tier2"),
}


class RatesIoError(Exception):
    """Raised when a rates show/set/import/update/check operation cannot proceed."""


@dataclass(frozen=True)
class ActiveBucket:
    """The bucket a plan is in at a given instant, and its effective $/kWh rate."""

    plan: str
    bucket: str
    rate: float


@dataclass(frozen=True)
class ShowResult:
    """Result of :func:`rates_show`: the stored rows in effect plus active buckets.

    Attributes:
        on: The local calendar date of ``at``, used to select effective-dated rows.
        at: The instant the buckets/rates were evaluated at.
        timezone: The configured IANA timezone name.
        tou_row: The effective-dated TOU price row covering ``on``.
        ulo_row: The effective-dated ULO price row covering ``on``.
        tiered_row: The effective-dated Tiered price row covering ``on``.
        tiered_threshold_kwh: The Tiered monthly threshold in kWh for ``on``'s season.
        active: Plan name to its :class:`ActiveBucket` at ``at`` (``"tou"`` and
            ``"ulo"`` only; Tiered is volume-based and has no time bucket).
    """

    on: date
    at: datetime
    timezone: str
    tou_row: dict[str, Any]
    ulo_row: dict[str, Any]
    tiered_row: dict[str, Any]
    tiered_threshold_kwh: int
    active: dict[str, ActiveBucket]


@dataclass(frozen=True)
class RowDiff:
    """One changed field on a price row shared by the stored and fetched files."""

    plan: str
    effective: str
    field: str
    old: Any
    new: Any


@dataclass(frozen=True)
class UpdateDiff:
    """Result of diffing stored vs. fetched price rows across every plan.

    Attributes:
        added: Fetched rows whose ``effective`` date has no stored counterpart,
            for the plan they belong to (each row also carries ``"plan"``).
        changed: One :class:`RowDiff` per differing field on a row present in
            both stored and fetched data.
        unchanged: Count of rows present in both with no differing field.
        applied: True if the merged rows were written to ``rates.json``.
    """

    added: list[dict[str, Any]]
    changed: list[RowDiff]
    unchanged: int
    applied: bool


@dataclass(frozen=True)
class CheckResult:
    """Result of :func:`rates_check`: whether stored rates are stale, and why."""

    stale: bool
    diff: UpdateDiff


def _now(config: RatesConfig) -> datetime:
    """Return the current instant in ``config``'s timezone. Monkeypatched in tests."""
    # logic-coverage-exempt: T-3/T-4 - wall-clock read is non-deterministic;
    # exercised indirectly via test_rates_show_atomitted_usesnowhelper, which
    # monkeypatches this function rather than asserting on real wall-clock output.
    return datetime.now(tz=config.zone)


def _read_rates_file(path: Path) -> dict[str, Any]:
    """Read and JSON-parse ``path``, raising :class:`RatesIoError` on failure."""
    if not path.is_file():
        raise RatesIoError(f"Rates file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RatesIoError(f"Invalid JSON in rates file {path}: {exc}") from exc


def _write_rates_file(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as indented JSON to ``path``."""
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def rates_show(config: RatesConfig, *, at: datetime | None = None) -> ShowResult:
    """Show the stored rate rows in effect and the active bucket/rate right now.

    Fully offline: reads only the already-loaded ``config``.

    Args:
        config: The loaded :class:`~emporia_hydro.rates.RatesConfig`.
        at: The instant to evaluate. Defaults to the current time in
            ``config``'s timezone.

    Returns:
        A :class:`ShowResult` with the effective-dated rows and active buckets.

    Raises:
        RatesConfigError: If no price row covers the resolved local date (see
            :func:`emporia_hydro.rates.price_row`).
    """
    instant = at if at is not None else _now(config)
    local_date = instant.astimezone(config.zone).date()

    tou_row = price_row("tou", local_date, config)
    ulo_row = price_row("ulo", local_date, config)
    tiered_row = price_row("tiered", local_date, config)

    active = {
        "tou": ActiveBucket("tou", tou_bucket(instant, config), tou_rate(instant, config)),
        "ulo": ActiveBucket("ulo", ulo_bucket(instant, config), ulo_rate(instant, config)),
    }

    return ShowResult(
        on=local_date,
        at=instant,
        timezone=config.timezone_name,
        tou_row=tou_row,
        ulo_row=ulo_row,
        tiered_row=tiered_row,
        tiered_threshold_kwh=tiered_threshold_kwh(local_date, config),
        active=active,
    )


def _validate_plan(plan: str) -> tuple[str, ...]:
    """Return the required rate keys for ``plan``, or raise :class:`RatesIoError`."""
    required = _PLAN_REQUIRED_KEYS.get(plan)
    if required is None:
        known = ", ".join(repr(name) for name in _PLAN_REQUIRED_KEYS)
        raise RatesIoError(f"Unknown plan: {plan!r}; must be one of {known}")
    return required


def _validate_rate_keys(plan: str, required: tuple[str, ...], rates: dict[str, float]) -> None:
    """Raise :class:`RatesIoError` if any required rate key is missing."""
    for key in required:
        if key not in rates:
            raise RatesIoError(f"Missing required rate key {key!r} for plan {plan!r}")


def _build_price_row(
    effective: str, rates: dict[str, float], expiry: str | None, confidence: str
) -> dict[str, Any]:
    """Assemble one price-row dict in the same shape as rates.json rows."""
    return {"effective": effective, "expiry": expiry, **rates, "confidence": confidence}


def _upsert_price_row(
    prices: list[dict[str, Any]], effective: str, new_row: dict[str, Any]
) -> None:
    """Replace the row matching ``effective`` in-place, or append + re-sort."""
    for index, row in enumerate(prices):
        if row["effective"] == effective:
            prices[index] = new_row
            break
    else:
        prices.append(new_row)
    prices.sort(key=lambda row: row["effective"], reverse=True)


def rates_set(
    config_dir: str | os.PathLike,
    *,
    plan: str,
    effective: str,
    rates: dict[str, float],
    expiry: str | None = None,
    confidence: str = "high",
) -> dict[str, Any]:
    """Append or replace a price row for ``plan`` at ``effective`` in ``rates.json``.

    Offline: only reads and writes the local ``config_dir/rates.json``. Every
    other key in the file (``_comment``, ``seasons``, ``schedule``, ``holidays``,
    ``thresholds``, and every other plan) is preserved unchanged.

    Args:
        config_dir: Directory containing ``rates.json``.
        plan: One of ``"tou"``, ``"ulo"``, ``"tiered"``.
        effective: The row's effective date, ``YYYY-MM-DD``.
        rates: The plan's required rate keys mapped to $/kWh values (TOU:
            ``off``/``mid``/``on``; ULO: ``overnight``/``weekend_off``/``mid``/
            ``on``; Tiered: ``tier1``/``tier2``).
        expiry: Optional expiry date, ``YYYY-MM-DD``.
        confidence: Source-confidence label written onto the row.

    Returns:
        The written price row.

    Raises:
        RatesIoError: If ``plan`` is unrecognized or ``rates`` is missing a
            required key for that plan.
    """
    required = _validate_plan(plan)
    _validate_rate_keys(plan, required, rates)

    path = Path(config_dir) / "rates.json"
    data = _read_rates_file(path)

    new_row = _build_price_row(effective, rates, expiry, confidence)
    prices = data["plans"][plan]["prices"]
    _upsert_price_row(prices, effective, new_row)

    _write_rates_file(path, data)
    return new_row


def rates_import(src_path: str | os.PathLike, config_dir: str | os.PathLike) -> dict[str, Any]:
    """Validate and import an external rates file, replacing ``config_dir/rates.json``.

    Offline: validates ``src_path`` by copying it into an isolated temp directory
    and calling :func:`emporia_hydro.rates.load_config` on that copy, so an
    invalid file never touches the destination.

    Args:
        src_path: Path to the candidate rates file to import.
        config_dir: Directory whose ``rates.json`` is replaced on success.

    Returns:
        ``{"plans": [<plan names>], "rows": <total price row count>}``.

    Raises:
        RatesIoError: If ``src_path`` does not exist or fails to parse as a
            valid rates file.
    """
    src = Path(src_path)
    if not src.is_file():
        raise RatesIoError(f"Rates import file not found: {src}")

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        shutil.copy(src, tmp_dir / "rates.json")
        try:
            load_config(tmp_dir)
        except RatesConfigError as exc:
            raise RatesIoError(f"Invalid rates file {src}: {exc}") from exc

    data = json.loads(src.read_text(encoding="utf-8"))
    dest = Path(config_dir) / "rates.json"
    shutil.copy(src, dest)

    plans = list(data["plans"])
    rows = sum(len(data["plans"][plan]["prices"]) for plan in plans)
    return {"plans": plans, "rows": rows}


def _diff_plan_rows(
    plan: str, stored_rows: list[dict[str, Any]], fetched_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[RowDiff], int]:
    """Diff one plan's stored vs. fetched price rows by ``effective`` date."""
    stored_by_effective = {row["effective"]: row for row in stored_rows}
    added: list[dict[str, Any]] = []
    changed: list[RowDiff] = []
    unchanged = 0

    for fetched_row in fetched_rows:
        effective = fetched_row["effective"]
        stored_row = stored_by_effective.get(effective)
        if stored_row is None:
            added.append({"plan": plan, **fetched_row})
            continue
        row_changes = [
            RowDiff(plan, effective, field, stored_row.get(field), fetched_row.get(field))
            for field in sorted(stored_row.keys() | fetched_row.keys())
            if stored_row.get(field) != fetched_row.get(field)
        ]
        if row_changes:
            changed.extend(row_changes)
        else:
            unchanged += 1

    return added, changed, unchanged


def _diff_rates(stored: dict[str, Any], fetched: dict[str, Any]) -> UpdateDiff:
    """Diff every plan's price rows between the stored and fetched rates files."""
    added: list[dict[str, Any]] = []
    changed: list[RowDiff] = []
    unchanged = 0

    for plan, fetched_plan in fetched["plans"].items():
        stored_rows = stored["plans"].get(plan, {}).get("prices", [])
        plan_added, plan_changed, plan_unchanged = _diff_plan_rows(
            plan, stored_rows, fetched_plan["prices"]
        )
        added.extend(plan_added)
        changed.extend(plan_changed)
        unchanged += plan_unchanged

    return UpdateDiff(added=added, changed=changed, unchanged=unchanged, applied=False)


def _merge_fetched_into_stored(stored: dict[str, Any], fetched: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``stored`` with every fetched price row upserted in."""
    merged = json.loads(json.dumps(stored))
    for plan, fetched_plan in fetched["plans"].items():
        # A plan present only in the fetched file is upserted as a brand-new
        # plan -- the same tolerance the diff already advertises via 'added' --
        # rather than raising KeyError on a missing stored counterpart.
        prices = merged["plans"].setdefault(plan, {"prices": []}).setdefault("prices", [])
        for fetched_row in fetched_plan["prices"]:
            _upsert_price_row(prices, fetched_row["effective"], fetched_row)
    return merged


def rates_update(
    config_dir: str | os.PathLike, *, fetcher: Any, apply: bool = False
) -> UpdateDiff:
    """Diff stored rates against a fetched rates file, optionally writing the merge.

    No network I/O happens in this module: ``fetcher`` is an injected zero-arg
    callable returning a dict in the same shape as ``rates.json``.

    Args:
        config_dir: Directory containing ``rates.json``.
        fetcher: Zero-argument callable returning a rates-file-shaped dict.
        apply: If True, write the merged (stored + fetched) rows back to
            ``rates.json``. If False, only compute and return the diff.

    Returns:
        The computed :class:`UpdateDiff`, with ``applied`` reflecting whether a
        write happened.
    """
    path = Path(config_dir) / "rates.json"
    stored = _read_rates_file(path)
    fetched = fetcher()

    diff = _diff_rates(stored, fetched)

    if apply:
        merged = _merge_fetched_into_stored(stored, fetched)
        _write_rates_file(path, merged)

    return dataclasses.replace(diff, applied=apply)


def rates_check(config_dir: str | os.PathLike, *, fetcher: Any) -> CheckResult:
    """Compare stored rates against a fetched rates file without ever mutating.

    Args:
        config_dir: Directory containing ``rates.json``.
        fetcher: Zero-argument callable returning a rates-file-shaped dict.

    Returns:
        A :class:`CheckResult` with ``stale=True`` if any row was added or
        changed relative to the stored file.
    """
    diff = rates_update(config_dir, fetcher=fetcher, apply=False)
    stale = bool(diff.added or diff.changed)
    return CheckResult(stale=stale, diff=diff)
