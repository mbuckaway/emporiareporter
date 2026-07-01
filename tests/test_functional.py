# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""End-to-end acceptance gate for emporia_hydro - COMPLETE test suite written FIRST.

Drives the REAL pipeline (rates -> cost -> compare -> billing -> trends -> ytd
-> report) against fixture usage data and the real, read-only ``config/``
directory. The only mocked boundary is the ``tailwindcss`` subprocess inside
:func:`emporia_hydro.report.build_css`; everything else -- Jinja2 rendering,
matplotlib SVG chart generation (headless, Agg backend), and JSON manifest/
history bookkeeping -- runs for real against ``tmp_path``.
"""

import json
import re
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from emporia_hydro.billing import Settings, load_settings, predict_bill
from emporia_hydro.compare import compare_plans
from emporia_hydro.cost import Tariff, load_tariff, price_usage
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.rates import RatesConfig, load_config
from emporia_hydro.report import ReportError, build_css, generate_report
from emporia_hydro.trends import daily_series
from emporia_hydro.ytd import ytd_summary

REPO_ROOT = Path(__file__).resolve().parents[1]

# The billing-cycle snapshot date this functional run reports "as of". Fixed
# (never today) so the pipeline output is deterministic across runs.
_ON_DATE = date(2026, 7, 13)

# Fixture usage spans July 2-13 2026 (12 consecutive local days): Jul 1 is the
# Canada Day statutory holiday (off-peak all day) and is deliberately excluded
# so weekday/weekend TOU buckets are unambiguous. The range covers two full
# weekends (4th-5th, 11th-12th) plus eight weekdays.
_FIXTURE_FIRST_DAY = date(2026, 7, 2)
_FIXTURE_DAY_COUNT = 12

_MAINS_DEVICE_GID = 1
_MAINS_CHANNEL_NUM = "1"
_BRANCH_DEVICE_GID = 1
_BRANCH_CHANNEL_NUM = "10"
_AUX_DEVICE_GID = 2
_AUX_CHANNEL_NUM = "1"

# Local wall-clock hours used to populate on/mid/off TOU buckets under the
# summer schedule (on=[11,17), mid=[7,11)+[17,19), off=[0,7)+[19,24)).
_OFF_HOUR = 2
_MID_HOUR_MORNING = 8
_ON_HOUR = 12
_MID_HOUR_EVENING = 18
_OFF_HOUR_NIGHT = 22


def _local_datetime(local_date: date, hour: int, config: RatesConfig) -> datetime:
    """Build an aware UTC datetime for a local wall-clock hour on ``local_date``."""
    local_naive = datetime(local_date.year, local_date.month, local_date.day, hour)
    local_aware = local_naive.replace(tzinfo=config.zone)
    return local_aware.astimezone(UTC)


def _day_usages(local_date: date, config: RatesConfig, *, is_weekend: bool) -> list[IntervalUsage]:
    """Build one day's worth of hourly usage across mains/branch/aux channels.

    Each day contributes readings at five representative local hours so the
    fixture lands kWh in every TOU bucket (on/mid/off). Weekend days use
    slightly higher mains draw and a longer EV-charger overnight session to
    keep the two day types visibly distinct in the trend/weekday-weekend
    summary output.
    """
    mains_kwh = 1.8 if is_weekend else 1.2
    branch_kwh = 0.6
    aux_kwh = 2.5 if is_weekend else 1.5

    usages: list[IntervalUsage] = []
    for hour in (_OFF_HOUR, _MID_HOUR_MORNING, _ON_HOUR, _MID_HOUR_EVENING, _OFF_HOUR_NIGHT):
        ts = _local_datetime(local_date, hour, config)
        usages.append(
            IntervalUsage(
                ts=ts,
                scale="1H",
                device_gid=_MAINS_DEVICE_GID,
                channel=_MAINS_CHANNEL_NUM,
                kwh=mains_kwh,
            )
        )
        usages.append(
            IntervalUsage(
                ts=ts,
                scale="1H",
                device_gid=_BRANCH_DEVICE_GID,
                channel=_BRANCH_CHANNEL_NUM,
                kwh=branch_kwh,
            )
        )
        usages.append(
            IntervalUsage(
                ts=ts,
                scale="1H",
                device_gid=_AUX_DEVICE_GID,
                channel=_AUX_CHANNEL_NUM,
                kwh=aux_kwh,
            )
        )
    return usages


def _fixture_channels() -> list[Channel]:
    """Build the mains/branch/aux Channel metadata matching the usage fixture."""
    return [
        Channel(
            device_gid=_MAINS_DEVICE_GID,
            device_name="Home",
            channel_num=_MAINS_CHANNEL_NUM,
            name="Mains",
            role="mains",
        ),
        Channel(
            device_gid=_BRANCH_DEVICE_GID,
            device_name="Home",
            channel_num=_BRANCH_CHANNEL_NUM,
            name="Dryer",
            role="branch",
        ),
        Channel(
            device_gid=_AUX_DEVICE_GID,
            device_name="EV Charger",
            channel_num=_AUX_CHANNEL_NUM,
            name="EV Charger",
            role="aux",
        ),
    ]


def _fixture_usages(config: RatesConfig) -> list[IntervalUsage]:
    """Build ~12 consecutive local days of hourly usage spanning weekdays+weekend."""
    usages: list[IntervalUsage] = []
    for offset in range(_FIXTURE_DAY_COUNT):
        local_date = date.fromordinal(_FIXTURE_FIRST_DAY.toordinal() + offset)
        is_weekend = local_date.weekday() >= 5
        usages.extend(_day_usages(local_date, config, is_weekend=is_weekend))
    return usages


@pytest.fixture
def rates_config() -> RatesConfig:
    """Load the REAL repo config/rates.json (read-only ground truth)."""
    return load_config(REPO_ROOT / "config")


@pytest.fixture
def tariff() -> Tariff:
    """Load the REAL repo config/tariff.json (read-only ground truth)."""
    return load_tariff(REPO_ROOT / "config")


@pytest.fixture
def settings() -> Settings:
    """Load the REAL repo config/settings.json (read-only ground truth)."""
    return load_settings(REPO_ROOT / "config")


@pytest.fixture
def channels() -> list[Channel]:
    """Mains + branch + aux Channel metadata for the fixture usage."""
    return _fixture_channels()


@pytest.fixture
def usages(rates_config: RatesConfig) -> list[IntervalUsage]:
    """Twelve consecutive local days of hourly usage across three channels."""
    return _fixture_usages(rates_config)


@pytest.fixture(autouse=True)
def _mock_tailwind_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Mock ONLY the tailwindcss subprocess boundary inside report.build_css.

    Every other I/O boundary (filesystem writes, Jinja2 rendering, matplotlib
    SVG rendering) runs for real against tmp_path. Writes a trivial but
    non-empty stylesheet at the requested output path so downstream file
    existence checks behave the same as a real Tailwind build.
    """
    calls: list[tuple] = []

    def _fake_run(
        args: list[str], check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess:
        calls.append((tuple(args), check, capture_output, text))
        Path(args[args.index("-o") + 1]).write_text("body{color:#000}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def _run_full_pipeline(
    tmp_path: Path,
    usages: list[IntervalUsage],
    channels: list[Channel],
    rates_config: RatesConfig,
    tariff: Tariff,
    settings: Settings,
) -> dict:
    """Drive rates -> cost -> compare -> billing -> trends -> ytd -> report for real."""
    period_start = date(_ON_DATE.year, _ON_DATE.month, 1)
    cost = price_usage(usages, channels, rates_config, plan="tou")
    comparison = compare_plans(
        usages,
        channels,
        rates_config,
        tariff,
        settings,
        start=period_start,
        end=_ON_DATE,
    )
    prediction = predict_bill(
        usages, channels, rates_config, tariff, settings, on=_ON_DATE, plan="tou"
    )
    day_stats = daily_series(usages, channels, rates_config)
    ytd = ytd_summary(usages, channels, rates_config, tariff, on=_ON_DATE, plan="tou")

    # Build into tmp_path (never the real repo asset tree) so the mocked
    # subprocess boundary is exercised on every run regardless of the
    # checked-in app.css's mtime relative to theme.css.
    build_css(output_css=tmp_path / "static" / "app.css", force=True)
    return generate_report(
        cost=cost,
        comparison=comparison,
        prediction=prediction,
        day_stats=day_stats,
        ytd=ytd,
        settings=settings,
        out_dir=tmp_path,
        label="functional",
        snapshot_date=_ON_DATE,
    )


# ---------------------------------------------------------------------------
# Full pipeline: report/index/ytd HTML pages actually produced
# ---------------------------------------------------------------------------


def test_full_pipeline_realusage_writesnontrivialreporthtml(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    report_html = tmp_path / "reports" / "functional.html"
    content = report_html.read_text(encoding="utf-8")
    assert report_html.is_file() and len(content) > 500 and "<html" in content.lower()


def test_full_pipeline_realusage_writesindexlinkingreportandytd(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    index_html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'href="reports/functional.html"' in index_html
    assert 'href="ytd.html"' in index_html


def test_full_pipeline_realusage_writesytdhtml(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    ytd_html = tmp_path / "ytd.html"
    assert ytd_html.is_file() and len(ytd_html.read_text(encoding="utf-8")) > 0


def test_full_pipeline_realusage_writesnonemptydailychartsvg(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    daily_svg = tmp_path / "charts" / "functional-daily.svg"
    assert daily_svg.is_file() and daily_svg.stat().st_size > 0


def test_full_pipeline_realusage_writesnonemptycomparechartsvg(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    compare_svg = tmp_path / "charts" / "functional-compare.svg"
    assert compare_svg.is_file() and compare_svg.stat().st_size > 0


def test_full_pipeline_realusage_manifestcontainsrecommendedplan(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 1
    assert manifest[0]["recommended_plan"] in {"tou", "ulo", "tiered"}


def test_full_pipeline_realusage_manifestentrymatchesreturnedentry(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    entry = _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == [entry]


def test_full_pipeline_realusage_ytdhistoryhasonesnapshot(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    history = json.loads((tmp_path / "ytd_history.json").read_text(encoding="utf-8"))
    assert history == [
        {
            "date": _ON_DATE.isoformat(),
            "whole_home_kwh": history[0]["whole_home_kwh"],
            "full_total": history[0]["full_total"],
        }
    ]
    assert len(history) == 1


def test_full_pipeline_realusage_priceduseragreeswithytdwholehomekwh(
    tmp_path, usages, channels, rates_config, tariff, settings
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    history = json.loads((tmp_path / "ytd_history.json").read_text(encoding="utf-8"))
    # Every fixture day falls within Jan 1..on_date of the same year, so the
    # YTD whole-home kWh must equal the whole-home total priced directly.
    direct_cost = price_usage(usages, channels, rates_config, plan="tou")
    assert history[0]["whole_home_kwh"] == pytest.approx(direct_cost.whole_home_kwh)


def test_full_pipeline_realusage_tailwindsubprocessinvokedonce(
    tmp_path, usages, channels, rates_config, tariff, settings, _mock_tailwind_subprocess
):
    _run_full_pipeline(tmp_path, usages, channels, rates_config, tariff, settings)

    assert len(_mock_tailwind_subprocess) == 1


def test_full_pipeline_realusage_dayscoverweekdayandweekend(
    tmp_path, usages, channels, rates_config
):
    stats = daily_series(usages, channels, rates_config)

    weekdays = [s for s in stats if s.day.weekday() < 5]
    weekends = [s for s in stats if s.day.weekday() >= 5]
    assert (len(stats), bool(weekdays), bool(weekends)) == (_FIXTURE_DAY_COUNT, True, True)


def test_full_pipeline_realusage_costsplitacrossonmidoffbuckets(
    tmp_path, usages, channels, rates_config
):
    breakdown = price_usage(usages, channels, rates_config, plan="tou")

    assert set(breakdown.by_bucket) == {"on", "mid", "off"}


# ---------------------------------------------------------------------------
# build_css real invocation against the repo's checked-in Tailwind assets
# ---------------------------------------------------------------------------


def test_build_css_realtheme_writesoutputundertmppath(tmp_path):
    output_css = tmp_path / "static" / "app.css"

    result = build_css(output_css=output_css, force=True)

    assert result == output_css and output_css.is_file()


def test_build_css_missingbinarywithinpipeline_raisesreporterror(tmp_path):
    missing_binary = tmp_path / "does-not-exist"
    expected_match = f"Tailwind binary not found: {missing_binary}"

    with pytest.raises(ReportError, match=re.escape(expected_match)):
        build_css(binary=missing_binary, output_css=tmp_path / "app.css")
