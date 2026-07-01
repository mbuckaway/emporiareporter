# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Jinja2-rendered local HTML dashboard: report pages, YTD page, and index.

This module is the "write side" of the dashboard: it renders per-billing-cycle
report pages, a year-to-date page, and a manifest-driven index page to an
output directory (typically ``reports/``), and builds the standalone Tailwind
v4 stylesheet consumed by every page. :mod:`emporia_hydro.server` then serves
that output directory over plain HTTP.

:func:`generate_report` is the single orchestration entry point the CLI's
``report`` subcommand calls: it prices nothing itself (all pricing/comparison/
prediction/trend/YTD math lives in :mod:`emporia_hydro.cost`,
:mod:`emporia_hydro.compare`, :mod:`emporia_hydro.billing`,
:mod:`emporia_hydro.trends`, and :mod:`emporia_hydro.ytd`) -- it only turns
already-computed results into HTML, charts, and the manifest/index/YTD-history
bookkeeping files.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "none"

import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use("Agg"))
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from emporia_hydro.billing import BillPrediction, Settings  # noqa: E402
from emporia_hydro.compare import ComparisonResult  # noqa: E402
from emporia_hydro.cost import CostBreakdown  # noqa: E402
from emporia_hydro.trends import DayStat, render_daily_svg  # noqa: E402
from emporia_hydro.ytd import YtdSummary  # noqa: E402

__all__ = [
    "ReportError",
    "build_css",
    "generate_report",
    "publish_css",
    "regenerate_index",
    "render_report",
    "render_ytd",
    "update_manifest",
]

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_DEFAULT_TAILWIND_BINARY = Path(__file__).resolve().parent.parent / ".bin" / "tailwindcss"
_DEFAULT_THEME_CSS = Path(__file__).resolve().parent / "assets" / "theme.css"
_DEFAULT_APP_CSS = Path(__file__).resolve().parent / "assets" / "static" / "app.css"

# TrailLens palette for the plan-comparison bar chart; matches trends.py's
# primary/secondary and adds a third tone for the Tiered plan.
_PLAN_BAR_COLORS: dict[str, str] = {"tou": "#4caf50", "ulo": "#1976d2", "tiered": "#f57f17"}
_LIGHT_FOREGROUND = "#212121"


class ReportError(Exception):
    """Raised when the CSS build fails or a report/manifest/history file is malformed."""


def build_css(
    *,
    binary: str | os.PathLike = _DEFAULT_TAILWIND_BINARY,
    input_css: str | os.PathLike = _DEFAULT_THEME_CSS,
    output_css: str | os.PathLike = _DEFAULT_APP_CSS,
    force: bool = False,
) -> Path:
    """Build the standalone Tailwind v4 stylesheet from ``theme.css``.

    Skips the rebuild when ``output_css`` already exists and is newer than
    ``input_css``, unless ``force`` is True.

    Args:
        binary: Path to the standalone Tailwind v4 CLI binary.
        input_css: Path to the Tailwind v4 input stylesheet (``theme.css``).
        output_css: Path to write the built, minified stylesheet to.
        force: When True, rebuild even if ``output_css`` is already current.

    Returns:
        The path to the built stylesheet (``output_css``).

    Raises:
        ReportError: If ``binary`` does not exist, or the build subprocess
            fails.
    """
    binary_path = Path(binary)
    output_path = Path(output_css)
    input_path = Path(input_css)
    if not binary_path.is_file():
        raise ReportError(f"Tailwind binary not found: {binary_path}")

    output_mtime = output_path.stat().st_mtime if output_path.is_file() else None
    is_current = output_mtime is not None and output_mtime >= input_path.stat().st_mtime
    if not force and is_current:
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [str(binary_path), "-i", str(input_path), "-o", str(output_path), "--minify"]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ReportError(f"Tailwind CSS build failed: {exc.stderr}") from exc
    return output_path


def publish_css(out_dir: str | os.PathLike, *, force: bool = False) -> Path:
    """Build the stylesheet and copy it into ``out_dir/static/app.css``.

    The report/index/YTD templates all link ``static/app.css`` relative to the
    served root, so a generated dashboard is only self-contained (and styled
    once served) when the built stylesheet lives at that path. This bridges the
    package-local build output to the served output directory.

    Args:
        out_dir: The dashboard output directory root.
        force: Passed through to :func:`build_css` to force a rebuild.

    Returns:
        The path the stylesheet was published to (``out_dir/static/app.css``).

    Raises:
        ReportError: If the Tailwind build fails (see :func:`build_css`).
    """
    built = build_css(force=force)
    dest = Path(out_dir) / "static" / "app.css"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(built, dest)
    return dest


def _jinja_env() -> Environment:
    """Build the Jinja2 environment rooted at ``emporia_hydro/templates``."""
    return Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )


def render_report(context: dict[str, Any], out_dir: str | os.PathLike, *, label: str) -> Path:
    """Render ``report.html`` for one billing cycle to ``out_dir/reports/<label>.html``.

    Args:
        context: The Jinja2 template context (KPI values, comparison,
            prediction, cost breakdown, day stats, recommendation, chart
            paths -- see :func:`generate_report` for how it is assembled).
        out_dir: The dashboard output directory root.
        label: The billing-cycle label used as the report's file stem.

    Returns:
        The path the rendered report was written to.
    """
    reports_dir = Path(out_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html = _jinja_env().get_template("report.html").render(**context, asset_prefix="../")
    out_path = reports_dir / f"{label}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _read_json_list(path: Path, error_prefix: str) -> list[Any]:
    """Read ``path`` as a JSON list, or return an empty list if absent.

    Raises:
        ReportError: If the file is not valid JSON, or parses to something
            other than a JSON array (which would break the later ``.append``).
    """
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{error_prefix} {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ReportError(
            f"{error_prefix} {path}: expected a JSON array, got {type(data).__name__}"
        )
    return data


def update_manifest(entry: dict[str, Any], out_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Append ``entry`` to ``out_dir/manifest.json``, creating it if absent.

    Args:
        entry: The manifest row to append (report label, period, totals,
            recommendation -- see :func:`generate_report`).
        out_dir: The dashboard output directory root.

    Returns:
        The full manifest list after appending ``entry``.

    Raises:
        ReportError: If an existing ``manifest.json`` is not valid JSON.
    """
    manifest_path = Path(out_dir) / "manifest.json"
    manifest = _read_json_list(manifest_path, "Invalid JSON in manifest file")
    manifest.append(entry)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def regenerate_index(manifest: list[dict[str, Any]], out_dir: str | os.PathLike) -> Path:
    """Render ``index.html`` from the manifest, linking every report and YTD.

    Args:
        manifest: The full report manifest (see :func:`update_manifest`).
        out_dir: The dashboard output directory root.

    Returns:
        The path the rendered index was written to (``out_dir/index.html``).
    """
    out_path = Path(out_dir) / "index.html"
    html = _jinja_env().get_template("index.html").render(manifest=manifest, asset_prefix="")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_ytd(ytd: YtdSummary, out_dir: str | os.PathLike, *, snapshot_date: date) -> Path:
    """Render ``ytd.html`` and append a dated snapshot to ``ytd_history.json``.

    Args:
        ytd: The year-to-date summary to render (see
            :func:`emporia_hydro.ytd.ytd_summary`).
        out_dir: The dashboard output directory root.
        snapshot_date: The date to record for this history snapshot. Passed
            in explicitly rather than read from the clock so the render is
            deterministic and testable.

    Returns:
        The path the rendered YTD page was written to (``out_dir/ytd.html``).

    Raises:
        ReportError: If an existing ``ytd_history.json`` is not valid JSON.
    """
    out_dir_path = Path(out_dir)
    history_path = out_dir_path / "ytd_history.json"
    history = _read_json_list(history_path, "Invalid JSON in YTD history file")
    history.append(
        {
            "date": snapshot_date.isoformat(),
            "whole_home_kwh": ytd.whole_home_kwh,
            "full_total": ytd.full_total,
        }
    )
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    out_path = out_dir_path / "ytd.html"
    html = _jinja_env().get_template("ytd.html").render(ytd=ytd, asset_prefix="")
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _render_compare_svg(comparison: ComparisonResult, path: str | os.PathLike) -> None:
    """Render a grouped bar chart of each plan's full bill total per cycle.

    Args:
        comparison: The multi-cycle plan comparison to chart.
        path: Destination SVG file path.
    """
    labels = [cycle.period.label for cycle in comparison.cycles]
    plans = ("tou", "ulo", "tiered")
    bar_width = 0.8 / len(plans)
    x_positions = range(len(labels))

    fig, ax = plt.subplots(figsize=(10, 5))
    for index, plan in enumerate(plans):
        offsets = [x + (index - 1) * bar_width for x in x_positions]
        totals = [cycle.plan_costs[plan].full_total for cycle in comparison.cycles]
        ax.bar(offsets, totals, width=bar_width, label=plan.upper(), color=_PLAN_BAR_COLORS[plan])

    ax.set_xticks(list(x_positions))
    ax.set_xticklabels(labels, color=_LIGHT_FOREGROUND)
    ax.set_ylabel("Full bill total ($)", color=_LIGHT_FOREGROUND)
    ax.set_title("Plan comparison by billing cycle", color=_LIGHT_FOREGROUND)
    ax.tick_params(colors=_LIGHT_FOREGROUND)
    for spine in ax.spines.values():
        spine.set_color(_LIGHT_FOREGROUND)
    legend = ax.legend()
    for text in legend.get_texts():
        text.set_color(_LIGHT_FOREGROUND)

    fig.savefig(path, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)


@dataclass(frozen=True)
class _ReportKpis:
    """The scalar KPI values shown at the top of a rendered report page."""

    period_kwh: float
    period_energy_cost: float
    period_full_cost: float
    recommended_plan: str
    savings_vs_current: float


def _current_cycle_kpis(cost: CostBreakdown, comparison: ComparisonResult) -> _ReportKpis:
    """Derive the period/recommendation KPI values for the most recent cycle."""
    current_cycle = comparison.cycles[-1] if comparison.cycles else None
    period_full_cost = (
        current_cycle.plan_costs[comparison.current_plan].full_total if current_cycle else 0.0
    )
    return _ReportKpis(
        period_kwh=cost.whole_home_kwh,
        period_energy_cost=cost.whole_home_cost,
        period_full_cost=period_full_cost,
        recommended_plan=comparison.overall_cheapest_plan,
        savings_vs_current=comparison.overall_savings_vs_current,
    )


def _render_charts(
    day_stats: Sequence[DayStat], comparison: ComparisonResult, chart_dir: Path, label: str
) -> dict[str, str]:
    """Render the daily-trend and plan-comparison SVGs; return relative paths."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    daily_path = chart_dir / f"{label}-daily.svg"
    compare_path = chart_dir / f"{label}-compare.svg"
    render_daily_svg(day_stats, daily_path, title=f"Daily usage & cost - {label}")
    _render_compare_svg(comparison, compare_path)
    return {
        "daily": f"charts/{label}-daily.svg",
        "compare": f"charts/{label}-compare.svg",
    }


def generate_report(
    *,
    cost: CostBreakdown,
    comparison: ComparisonResult,
    prediction: BillPrediction,
    day_stats: Sequence[DayStat],
    ytd: YtdSummary,
    settings: Settings,
    out_dir: str | os.PathLike,
    label: str,
    snapshot_date: date,
    chart_dir: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Orchestrate one full dashboard refresh: report, charts, index, and YTD.

    Renders the per-cycle report page and its charts, appends the manifest
    entry, regenerates the index, and refreshes the YTD page and history.
    Performs no pricing/comparison/prediction math itself -- every input is
    already computed by :mod:`emporia_hydro.cost`, :mod:`emporia_hydro.compare`,
    :mod:`emporia_hydro.billing`, :mod:`emporia_hydro.trends`, and
    :mod:`emporia_hydro.ytd`.

    Args:
        cost: The current cycle's commodity cost breakdown.
        comparison: The multi-cycle TOU/ULO/Tiered plan comparison.
        prediction: The in-progress bill prediction for the current cycle.
        day_stats: Per-day usage/cost stats for the daily trend chart/table.
        ytd: The year-to-date whole-home/per-device rollup.
        settings: The loaded application settings.
        out_dir: The dashboard output directory root.
        label: The billing-cycle label for this report.
        snapshot_date: The "as of" date recorded in the manifest and the YTD
            history snapshot. Passed in explicitly (never read from the
            clock) so every render is deterministic.
        chart_dir: Directory to write chart SVGs to. Defaults to
            ``out_dir/charts``.

    Returns:
        The manifest entry appended for this report (see
        :func:`update_manifest`).
    """
    out_dir_path = Path(out_dir)
    resolved_chart_dir = Path(chart_dir) if chart_dir is not None else out_dir_path / "charts"
    chart_paths = _render_charts(day_stats, comparison, resolved_chart_dir, label)

    kpis = _current_cycle_kpis(cost, comparison)
    context = {
        "label": label,
        "period_kwh": kpis.period_kwh,
        "period_energy_cost": kpis.period_energy_cost,
        "period_full_cost": kpis.period_full_cost,
        "prediction": prediction,
        "comparison": comparison,
        "cost": cost,
        "day_stats": day_stats,
        "recommended_plan": kpis.recommended_plan,
        "savings_vs_current": kpis.savings_vs_current,
        "chart_paths": chart_paths,
    }
    render_report(context, out_dir_path, label=label)

    entry = {
        "label": label,
        "date": snapshot_date.isoformat(),
        "plan": settings.current_plan,
        "kwh": kpis.period_kwh,
        "energy_cost": kpis.period_energy_cost,
        "full_cost": kpis.period_full_cost,
        "recommended_plan": kpis.recommended_plan,
        "savings_vs_current": kpis.savings_vs_current,
    }
    manifest = update_manifest(entry, out_dir_path)
    regenerate_index(manifest, out_dir_path)
    render_ytd(ytd, out_dir_path, snapshot_date=snapshot_date)

    return entry
