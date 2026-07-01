# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.report - COMPLETE test suite written FIRST."""

import json
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from emporia_hydro import report
from emporia_hydro.billing import BillingPeriod, BillPrediction, Settings
from emporia_hydro.compare import ComparisonResult, CyclePlanComparison, PlanCost
from emporia_hydro.cost import BillEstimate, ChannelCost, CostBreakdown
from emporia_hydro.models import BALANCE_CHANNEL
from emporia_hydro.report import (
    ReportError,
    build_css,
    generate_report,
    publish_css,
    regenerate_index,
    render_report,
    render_ytd,
    update_manifest,
)
from emporia_hydro.trends import DayStat
from emporia_hydro.ytd import DeviceYtd, MonthCost, YtdSummary

# ---------------------------------------------------------------------------
# Fixture builders - small REAL dataclass instances with known values
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    """Build a minimal real Settings fixture."""
    return Settings(
        timezone="America/Toronto",
        current_plan="tou",
        billing_cycle={"mode": "calendar_month"},
        server={"host": "127.0.0.1", "port": 8765},
        output={"reports_dir": "reports", "data_dir": "data"},
    )


def _bill_estimate(total: float) -> BillEstimate:
    """Build a BillEstimate fixture with only ``total`` varying."""
    return BillEstimate(
        delivery_variable=1.0,
        delivery_fixed=2.0,
        subtotal=3.0,
        oer_credit=0.5,
        taxable=2.5,
        hst=0.325,
        total=total,
    )


def _cost_breakdown() -> CostBreakdown:
    """Build a small, known CostBreakdown with two channels and a balance."""
    return CostBreakdown(
        by_bucket={"on": (10.0, 2.03), "off": (5.0, 0.49)},
        by_channel={
            "1": ChannelCost("1", "Mains", "mains", 15.0, 2.52),
            "10": ChannelCost("10", "Dryer", "branch", 3.0, 0.6),
            BALANCE_CHANNEL: ChannelCost(BALANCE_CHANNEL, BALANCE_CHANNEL, "balance", 12.0, 1.92),
        },
        whole_home_kwh=15.0,
        whole_home_cost=2.52,
        balance_kwh=12.0,
        balance_cost=1.92,
    )


def _comparison_result() -> ComparisonResult:
    """Build a ComparisonResult where ULO is cheapest and TOU is current."""
    period = BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")
    plan_costs = {
        "tou": PlanCost("tou", 100.0, 150.0),
        "ulo": PlanCost("ulo", 80.0, 125.0),
        "tiered": PlanCost("tiered", 90.0, 140.0),
    }
    cycle = CyclePlanComparison(
        period=period,
        whole_home_kwh=500.0,
        plan_costs=plan_costs,
        cheapest_plan="ulo",
        current_plan="tou",
        savings_vs_current=20.0,
    )
    return ComparisonResult(
        cycles=[cycle],
        totals_by_plan=plan_costs,
        current_plan="tou",
        overall_cheapest_plan="ulo",
        overall_savings_vs_current=20.0,
    )


def _bill_prediction() -> BillPrediction:
    """Build a small, known BillPrediction fixture."""
    period = BillingPeriod(date(2026, 7, 1), date(2026, 7, 31), "2026-07")
    return BillPrediction(
        period=period,
        plan="tou",
        days_elapsed=10,
        days_remaining=21,
        to_date_kwh=150.0,
        to_date_energy_cost=25.0,
        projected_kwh=315.0,
        projected_energy_cost=52.5,
        predicted_kwh=465.0,
        predicted_energy_cost=77.5,
        predicted_full=_bill_estimate(120.0),
        per_day_type={"weekday": (15.0, 2.5), "weekend_holiday": (12.0, 2.0)},
    )


def _day_stats() -> list[DayStat]:
    """Build two DayStat rows spanning two consecutive local days."""
    return [
        DayStat(date(2026, 7, 1), 15.0, 6.0, 5.0, 4.0, 2.52),
        DayStat(date(2026, 7, 2), 12.0, 4.0, 4.0, 4.0, 1.98),
    ]


def _ytd_summary() -> YtdSummary:
    """Build a small, known YtdSummary fixture."""
    months = [MonthCost(date(2026, 1, 1), "2026-01", 300.0, 45.0, 90.0)]
    by_device = [
        DeviceYtd("1", "Mains", "mains", 300.0, 45.0),
        DeviceYtd(BALANCE_CHANNEL, BALANCE_CHANNEL, "balance", 250.0, 38.0),
    ]
    return YtdSummary(
        year=2026,
        through=date(2026, 1, 31),
        plan="tou",
        whole_home_kwh=300.0,
        whole_home_commodity=45.0,
        full_total=90.0,
        by_device=by_device,
        months=months,
    )


@pytest.fixture
def report_context() -> dict:
    """The full render_report() Jinja2 context, built from real dataclasses."""
    return {
        "label": "2026-07",
        "period_kwh": 500.0,
        "period_energy_cost": 100.0,
        "period_full_cost": 150.0,
        "prediction": _bill_prediction(),
        "comparison": _comparison_result(),
        "cost": _cost_breakdown(),
        "day_stats": _day_stats(),
        "recommended_plan": "ulo",
        "savings_vs_current": 20.0,
        "chart_paths": {
            "daily": "charts/2026-07-daily.svg",
            "compare": "charts/2026-07-compare.svg",
        },
    }


# ---------------------------------------------------------------------------
# build_css - subprocess boundary mocked (T-10, T-11)
# ---------------------------------------------------------------------------


def test_build_css_missingoutput_invokestailwindwithdocumentedargs(tmp_path, monkeypatch):
    calls = []

    def _fake_run(args, check, capture_output, text):
        calls.append((tuple(args), check, capture_output, text))
        Path(args[args.index("-o") + 1]).write_text("body{}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    input_css = tmp_path / "theme.css"
    input_css.write_text("@import 'tailwindcss';", encoding="utf-8")
    output_css = tmp_path / "static" / "app.css"
    binary = tmp_path / "tailwindcss"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = build_css(binary=binary, input_css=input_css, output_css=output_css)

    assert calls == [
        (
            (str(binary), "-i", str(input_css), "-o", str(output_css), "--minify"),
            True,
            True,
            True,
        )
    ]
    assert result == output_css


def test_build_css_outputnewerthaninput_skipsrebuildwithoutforce(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))
    input_css = tmp_path / "theme.css"
    input_css.write_text("old", encoding="utf-8")
    output_css = tmp_path / "static" / "app.css"
    output_css.parent.mkdir(parents=True)
    output_css.write_text("built", encoding="utf-8")
    binary = tmp_path / "tailwindcss"
    binary.write_text("", encoding="utf-8")

    result = build_css(binary=binary, input_css=input_css, output_css=output_css)

    assert (calls, result) == ([], output_css)


def test_build_css_forcetrue_rebuildseventhoughoutputnewer(tmp_path, monkeypatch):
    calls = []

    def _fake_run(args, check, capture_output, text):
        calls.append(tuple(args))
        Path(args[args.index("-o") + 1]).write_text("body{}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    input_css = tmp_path / "theme.css"
    input_css.write_text("old", encoding="utf-8")
    output_css = tmp_path / "static" / "app.css"
    output_css.parent.mkdir(parents=True)
    output_css.write_text("built", encoding="utf-8")
    binary = tmp_path / "tailwindcss"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = build_css(binary=binary, input_css=input_css, output_css=output_css, force=True)

    assert (len(calls), result) == (1, output_css)


def test_build_css_subprocessfails_raisesreporterror(tmp_path, monkeypatch):
    def _fake_run(args, check, capture_output, text):
        raise subprocess.CalledProcessError(1, args, output="", stderr="boom")

    input_css = tmp_path / "theme.css"
    input_css.write_text("@import 'tailwindcss';", encoding="utf-8")
    output_css = tmp_path / "static" / "app.css"
    binary = tmp_path / "tailwindcss"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(ReportError, match=re.escape("Tailwind CSS build failed")):
        build_css(binary=binary, input_css=input_css, output_css=output_css)


def test_build_css_missingbinary_raisesreporterror(tmp_path):
    input_css = tmp_path / "theme.css"
    input_css.write_text("@import 'tailwindcss';", encoding="utf-8")
    output_css = tmp_path / "static" / "app.css"
    missing_binary = tmp_path / "does-not-exist"
    expected_match = f"Tailwind binary not found: {missing_binary}"

    with pytest.raises(ReportError, match=re.escape(expected_match)):
        build_css(binary=missing_binary, input_css=input_css, output_css=output_css)


# ---------------------------------------------------------------------------
# render_report - Jinja2 render with testid hooks
# ---------------------------------------------------------------------------


def test_render_report_happypath_writesreportfileatlabelpath(tmp_path, report_context):
    result = render_report(report_context, tmp_path, label="2026-07")

    assert result == tmp_path / "reports" / "2026-07.html"
    assert result.is_file()


def test_render_report_happypath_prefixeschartsrcswithassetprefix(tmp_path, report_context):
    # The report lives one directory deep (out_dir/reports/), so chart <img>
    # srcs must be ../-prefixed to resolve against out_dir/charts -- a bare
    # "charts/..." src 404s under the served /reports/ path.
    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert 'src="../charts/2026-07-daily.svg"' in html
    assert 'src="../charts/2026-07-compare.svg"' in html
    assert 'src="charts/' not in html


def test_render_report_happypath_containskpitestidmarkers(tmp_path, report_context):
    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert 'data-testid="kpi-period-kwh"' in html
    assert 'data-testid="kpi-period-energy"' in html
    assert 'data-testid="kpi-predicted-energy"' in html
    assert 'data-testid="kpi-predicted-full"' in html
    assert 'data-testid="kpi-recommended-plan"' in html
    assert 'data-testid="device-cost-table"' in html
    assert 'data-testid="daily-trend-table"' in html
    assert 'data-testid="plan-comparison-chart"' in html


def test_render_report_happypath_containsrecommendedplanandsavings(tmp_path, report_context):
    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert "ulo" in html.lower()
    assert "20.00" in html


def test_render_report_emptydaystats_rendersemptytrendtable(tmp_path, report_context):
    report_context["day_stats"] = []

    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert 'data-testid="daily-trend-table"' in html


def test_render_report_singledaystat_rendersonerow(tmp_path, report_context):
    report_context["day_stats"] = [_day_stats()[0]]

    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert html.count("2026-07-01") == 1


def test_render_report_manydaystats_rendersallrows(tmp_path, report_context):
    result = render_report(report_context, tmp_path, label="2026-07")

    html = result.read_text(encoding="utf-8")
    assert ("2026-07-01" in html) and ("2026-07-02" in html)


# ---------------------------------------------------------------------------
# update_manifest - append semantics (T-4 collection boundaries)
# ---------------------------------------------------------------------------


def test_publish_css_copiesbuiltstylesheetintooutdirstatic(tmp_path, monkeypatch):
    built = tmp_path / "src" / "app.css"
    built.parent.mkdir(parents=True)
    built.write_text("/* built css */", encoding="utf-8")
    monkeypatch.setattr(report, "build_css", lambda *, force=False: built)

    dest = publish_css(tmp_path / "out")

    assert dest == tmp_path / "out" / "static" / "app.css"
    assert dest.read_text(encoding="utf-8") == "/* built css */"


def test_publish_css_forcetrue_passesforcethroughtobuildcss(tmp_path, monkeypatch):
    built = tmp_path / "app.css"
    built.write_text("x", encoding="utf-8")
    seen = {}

    def _fake_build_css(*, force=False):
        seen["force"] = force
        return built

    monkeypatch.setattr(report, "build_css", _fake_build_css)

    publish_css(tmp_path / "out", force=True)

    assert seen == {"force": True}


def test_update_manifest_missingfile_createsandreturnssingleentrylist(tmp_path):
    entry = {"label": "2026-07", "kwh": 500.0}

    result = update_manifest(entry, tmp_path)

    assert result == [entry]


def test_update_manifest_existingmanifest_appendsandreturnstwoentries(tmp_path):
    first = {"label": "2026-06", "kwh": 400.0}
    update_manifest(first, tmp_path)
    second = {"label": "2026-07", "kwh": 500.0}

    result = update_manifest(second, tmp_path)

    assert result == [first, second]


def test_update_manifest_writtenfile_matchesreturnedlist(tmp_path):
    entry = {"label": "2026-07", "kwh": 500.0}

    update_manifest(entry, tmp_path)

    on_disk = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == [entry]


def test_update_manifest_corruptmanifest_raisesreporterror(tmp_path):
    (tmp_path / "manifest.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ReportError, match=re.escape("Invalid JSON in manifest file")):
        update_manifest({"label": "2026-07"}, tmp_path)


def test_update_manifest_nonlistmanifest_raisesreporterror(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ReportError, match=re.escape("expected a JSON array")):
        update_manifest({"label": "2026-07"}, tmp_path)


# ---------------------------------------------------------------------------
# regenerate_index - empty-state vs populated
# ---------------------------------------------------------------------------


def test_regenerate_index_emptymanifest_rendersemptystateblock(tmp_path):
    result = regenerate_index([], tmp_path)

    html = result.read_text(encoding="utf-8")
    assert 'data-testid="index-empty-state"' in html


def test_regenerate_index_singlereport_linkstoreportandytd(tmp_path):
    manifest = [{"label": "2026-07", "kwh": 500.0}]

    result = regenerate_index(manifest, tmp_path)

    html = result.read_text(encoding="utf-8")
    assert 'href="reports/2026-07.html"' in html
    assert 'href="ytd.html"' in html


def test_regenerate_index_multiplereports_linksbothreports(tmp_path):
    manifest = [
        {"label": "2026-06", "kwh": 400.0},
        {"label": "2026-07", "kwh": 500.0},
    ]

    result = regenerate_index(manifest, tmp_path)

    html = result.read_text(encoding="utf-8")
    assert 'href="reports/2026-06.html"' in html
    assert 'href="reports/2026-07.html"' in html


def test_regenerate_index_populatedmanifest_writesindexatoutdirroot(tmp_path):
    manifest = [{"label": "2026-07", "kwh": 500.0}]

    result = regenerate_index(manifest, tmp_path)

    assert result == tmp_path / "index.html"


# ---------------------------------------------------------------------------
# render_ytd - writes ytd.html and appends to ytd_history.json
# ---------------------------------------------------------------------------


def test_render_ytd_happypath_writesytdhtmlfile(tmp_path):
    result = render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))

    assert result == tmp_path / "ytd.html"
    assert result.is_file()


def test_render_ytd_happypath_containsmonthlytabletestid(tmp_path):
    result = render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))

    html = result.read_text(encoding="utf-8")
    assert 'data-testid="ytd-monthly-table"' in html


def test_render_ytd_happypath_appendssnapshottohistory(tmp_path):
    render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))

    history = json.loads((tmp_path / "ytd_history.json").read_text(encoding="utf-8"))
    assert history == [{"date": "2026-01-31", "whole_home_kwh": 300.0, "full_total": 90.0}]


def test_render_ytd_calledtwice_appendstwosnapshots(tmp_path):
    render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))

    render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 2, 1))

    history = json.loads((tmp_path / "ytd_history.json").read_text(encoding="utf-8"))
    assert [row["date"] for row in history] == ["2026-01-31", "2026-02-01"]


def test_render_ytd_corrupthistory_raisesreporterror(tmp_path):
    (tmp_path / "ytd_history.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ReportError, match=re.escape("Invalid JSON in YTD history file")):
        render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))


def test_render_ytd_nonlisthistory_raisesreporterror(tmp_path):
    (tmp_path / "ytd_history.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ReportError, match=re.escape("expected a JSON array")):
        render_ytd(_ytd_summary(), tmp_path, snapshot_date=date(2026, 1, 31))


def test_render_ytd_emptydevicelist_rendersemptydevicetable(tmp_path):
    ytd = YtdSummary(
        year=2026,
        through=date(2026, 1, 1),
        plan="tou",
        whole_home_kwh=0.0,
        whole_home_commodity=0.0,
        full_total=0.0,
        by_device=[],
        months=[MonthCost(date(2026, 1, 1), "2026-01", 0.0, 0.0, 0.0)],
    )

    result = render_ytd(ytd, tmp_path, snapshot_date=date(2026, 1, 1))

    html = result.read_text(encoding="utf-8")
    assert 'data-testid="ytd-monthly-table"' in html


# ---------------------------------------------------------------------------
# generate_report - full orchestration (functional-style unit test)
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestration_kwargs(tmp_path) -> dict:
    """Kwargs for generate_report() built from small, known real dataclasses."""
    return {
        "cost": _cost_breakdown(),
        "comparison": _comparison_result(),
        "prediction": _bill_prediction(),
        "day_stats": _day_stats(),
        "ytd": _ytd_summary(),
        "settings": _settings(),
        "out_dir": tmp_path,
        "label": "2026-07",
        "snapshot_date": date(2026, 7, 31),
    }


def test_generate_report_happypath_returnsmanifestentrywithlabel(orchestration_kwargs):
    entry = generate_report(**orchestration_kwargs)

    assert entry["label"] == "2026-07"


def test_generate_report_happypath_returnsrecommendedplanandsavings(orchestration_kwargs):
    entry = generate_report(**orchestration_kwargs)

    assert (entry["recommended_plan"], entry["savings_vs_current"]) == ("ulo", 20.0)


def test_generate_report_happypath_writesreportfile(orchestration_kwargs, tmp_path):
    generate_report(**orchestration_kwargs)

    assert (tmp_path / "reports" / "2026-07.html").is_file()


def test_generate_report_happypath_writesindexandytdfiles(orchestration_kwargs, tmp_path):
    generate_report(**orchestration_kwargs)

    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "ytd.html").is_file()


def test_generate_report_happypath_writeschartsvgfiles(orchestration_kwargs, tmp_path):
    generate_report(**orchestration_kwargs)

    charts_dir = tmp_path / "charts"
    daily_svg = charts_dir / "2026-07-daily.svg"
    compare_svg = charts_dir / "2026-07-compare.svg"
    assert daily_svg.is_file() and daily_svg.stat().st_size > 0
    assert compare_svg.is_file() and compare_svg.stat().st_size > 0


def test_generate_report_happypath_updatesmanifestfile(orchestration_kwargs, tmp_path):
    generate_report(**orchestration_kwargs)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [row["label"] for row in manifest] == ["2026-07"]


def test_generate_report_calledtwice_manifesthastwoentries(orchestration_kwargs, tmp_path):
    generate_report(**orchestration_kwargs)
    second_kwargs = dict(orchestration_kwargs)
    second_kwargs["label"] = "2026-08"

    generate_report(**second_kwargs)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [row["label"] for row in manifest] == ["2026-07", "2026-08"]


def test_generate_report_customchartdir_writeschartsthere(orchestration_kwargs, tmp_path):
    custom_chart_dir = tmp_path / "my_charts"
    orchestration_kwargs["chart_dir"] = custom_chart_dir

    generate_report(**orchestration_kwargs)

    assert (custom_chart_dir / "2026-07-daily.svg").is_file()
    assert (custom_chart_dir / "2026-07-compare.svg").is_file()


def test_generate_report_emptydaystats_stillproducesreport(orchestration_kwargs, tmp_path):
    orchestration_kwargs["day_stats"] = []

    entry = generate_report(**orchestration_kwargs)

    assert entry["label"] == "2026-07"
    assert (tmp_path / "reports" / "2026-07.html").is_file()


def test_generate_report_emptycomparisoncycles_periodfullcostiszero(orchestration_kwargs):
    empty_comparison = ComparisonResult(
        cycles=[],
        totals_by_plan={},
        current_plan="tou",
        overall_cheapest_plan="tou",
        overall_savings_vs_current=0.0,
    )
    orchestration_kwargs["comparison"] = empty_comparison

    entry = generate_report(**orchestration_kwargs)

    assert entry["full_cost"] == 0.0
