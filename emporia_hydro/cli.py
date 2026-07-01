# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""``python -m emporia_hydro`` command-line dispatcher.

This module is the single argparse entry point for every subcommand:
``list-devices``, ``pull``, ``rates {show|set|import|update|check}``, ``cost``,
``compare``, ``predict``, ``trends``, ``ytd``, ``report``, and ``serve``.

Every handler is intentionally thin. Offline subcommands (``cost``,
``compare``, ``predict``, ``trends``, ``ytd``, ``report``, and the offline
``rates`` actions) load configuration via :mod:`emporia_hydro.rates`,
:mod:`emporia_hydro.cost`, and :mod:`emporia_hydro.billing`, read cached usage
via :func:`emporia_hydro.ingest.read_csv`, and delegate to the matching
business-logic module. Network subcommands (``list-devices``, ``pull``, and
``rates update``/``rates check``) go through :mod:`emporia_hydro.ingest` and
:mod:`emporia_hydro.ratesio`, which are the only I/O boundaries this module
ever touches directly.

Every known application error (:class:`~emporia_hydro.ingest.IngestError`,
:class:`~emporia_hydro.ratesio.RatesIoError`,
:class:`~emporia_hydro.cost.CostConfigError`,
:class:`~emporia_hydro.billing.BillingError`,
:class:`~emporia_hydro.rates.RatesConfigError`,
:class:`~emporia_hydro.report.ReportError`,
:class:`~emporia_hydro.compare.CompareError`) is caught by :func:`main` and
printed as a single clean line to stderr; anything else propagates.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from emporia_hydro.billing import BillingError, load_settings, predict_bill
from emporia_hydro.billing import current_period as billing_current_period
from emporia_hydro.compare import CompareError, compare_plans
from emporia_hydro.cost import CostConfigError, bill_estimate, load_tariff, price_usage
from emporia_hydro.ingest import (
    IngestError,
    connect,
    discover_channels,
    pull_usage,
    read_channels,
    read_csv,
    write_channels,
    write_csv,
)
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.rates import RatesConfig, RatesConfigError, load_config
from emporia_hydro.ratesio import (
    CheckResult,
    RatesIoError,
    UpdateDiff,
    rates_check,
    rates_import,
    rates_set,
    rates_show,
    rates_update,
)
from emporia_hydro.report import ReportError, generate_report, publish_css
from emporia_hydro.server import serve
from emporia_hydro.trends import daily_series
from emporia_hydro.ytd import ytd_summary

__all__ = ["build_parser", "main"]

_DEFAULT_CONFIG_DIR = "config"
_DEFAULT_CSV = "data/usage.csv"
_DEFAULT_CHANNELS = "data/channels.json"
_DEFAULT_OUT_DIR = "reports"
_DEFAULT_SCALE = "1H"

_KNOWN_ERRORS: tuple[type[Exception], ...] = (
    IngestError,
    RatesIoError,
    CostConfigError,
    BillingError,
    RatesConfigError,
    ReportError,
    CompareError,
)


def _today() -> date:
    """Return today's local date. The single clock read, kept mockable in tests."""
    return date.today()


def _add_config_dir_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the global ``--config-dir`` option to ``parser``."""
    parser.add_argument(
        "--config-dir",
        default=_DEFAULT_CONFIG_DIR,
        help=f"Config directory with rates/tariff/settings.json (default: {_DEFAULT_CONFIG_DIR})",
    )


def _add_csv_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--csv`` cached-usage-file option to ``parser``."""
    parser.add_argument(
        "--csv", default=_DEFAULT_CSV, help=f"Usage CSV cache file (default: {_DEFAULT_CSV})"
    )


def _add_channels_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--channels`` channel-metadata-cache option to ``parser``.

    Holds the channel roles (mains/branch/aux) ``pull`` discovers; offline
    commands read it to attribute the whole-home Mains total and per-device
    costs. Absent -> offline pricing sees no roles and whole-home totals are 0.
    """
    parser.add_argument(
        "--channels",
        default=_DEFAULT_CHANNELS,
        help=f"Channel metadata cache file (default: {_DEFAULT_CHANNELS})",
    )


def _add_start_end_args(parser: argparse.ArgumentParser) -> None:
    """Attach required ``--start``/``--end`` ISO date options to ``parser``."""
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end", required=True, type=date.fromisoformat, help="End date (YYYY-MM-DD)"
    )


def _add_out_dir_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--out-dir`` dashboard-output-directory option."""
    parser.add_argument(
        "--out-dir",
        default=_DEFAULT_OUT_DIR,
        help=f"Dashboard output directory (default: {_DEFAULT_OUT_DIR})",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``python -m emporia_hydro`` argument parser.

    Returns:
        A fully configured :class:`argparse.ArgumentParser` with every
        subcommand registered.
    """
    parser = argparse.ArgumentParser(prog="emporia_hydro", description="Emporia hydro cost tool")
    _add_config_dir_arg(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-devices", help="List every discovered Emporia device/channel")

    pull_parser = subparsers.add_parser("pull", help="Pull usage from the Emporia cloud")
    _add_csv_arg(pull_parser)
    _add_channels_arg(pull_parser)
    pull_parser.add_argument(
        "--scale", default=_DEFAULT_SCALE, help="pyemvue usage scale (default: 1H)"
    )
    pull_parser.add_argument("--start", type=date.fromisoformat, help="Start date (YYYY-MM-DD)")
    pull_parser.add_argument("--end", type=date.fromisoformat, help="End date (YYYY-MM-DD)")

    _build_rates_subparser(subparsers)

    cost_parser = subparsers.add_parser("cost", help="Price cached usage under a plan")
    _add_csv_arg(cost_parser)
    _add_channels_arg(cost_parser)
    _add_start_end_args(cost_parser)
    cost_parser.add_argument("--plan", default="tou", choices=["tou", "ulo"], help="Pricing plan")
    cost_parser.add_argument(
        "--full", action="store_true", help="Also print the full bill estimate"
    )

    compare_parser = subparsers.add_parser("compare", help="Compare TOU/ULO/Tiered plans")
    _add_csv_arg(compare_parser)
    _add_channels_arg(compare_parser)
    _add_start_end_args(compare_parser)

    predict_parser = subparsers.add_parser("predict", help="Predict the in-progress bill")
    _add_csv_arg(predict_parser)
    _add_channels_arg(predict_parser)
    predict_parser.add_argument(
        "--plan", default="tou", choices=["tou", "ulo"], help="Pricing plan"
    )

    trends_parser = subparsers.add_parser("trends", help="Show daily usage/cost trends")
    _add_csv_arg(trends_parser)
    _add_channels_arg(trends_parser)

    ytd_parser = subparsers.add_parser("ytd", help="Show the year-to-date rollup")
    _add_csv_arg(ytd_parser)
    _add_channels_arg(ytd_parser)
    ytd_parser.add_argument("--plan", default="tou", choices=["tou", "ulo"], help="Pricing plan")

    report_parser = subparsers.add_parser("report", help="Generate the HTML dashboard report")
    _add_csv_arg(report_parser)
    _add_channels_arg(report_parser)
    _add_out_dir_arg(report_parser)
    report_parser.add_argument(
        "--label", default=None, help="Billing-cycle label (default: current period)"
    )

    serve_parser = subparsers.add_parser("serve", help="Serve the generated dashboard over HTTP")
    _add_out_dir_arg(serve_parser)
    serve_parser.add_argument(
        "--port", type=int, default=None, help="TCP port (default: settings.json)"
    )

    return parser


def _build_rates_subparser(subparsers: Any) -> None:
    """Register the ``rates {show|set|import|update|check}`` subcommand tree."""
    rates_parser = subparsers.add_parser("rates", help="Show/edit/sync stored rate rows")
    rates_sub = rates_parser.add_subparsers(dest="rates_action", required=True)

    rates_sub.add_parser("show", help="Show the active buckets/rates right now")

    set_parser = rates_sub.add_parser("set", help="Append or replace a price row")
    set_parser.add_argument("--plan", required=True, help="Pricing plan: tou, ulo, or tiered")
    set_parser.add_argument("--effective", required=True, help="Effective date (YYYY-MM-DD)")
    set_parser.add_argument("--expiry", default=None, help="Expiry date (YYYY-MM-DD)")
    set_parser.add_argument(
        "--rate",
        action="append",
        required=True,
        metavar="KEY=VALUE",
        help="Rate key=value pair; repeat for each required rate key",
    )

    import_parser = rates_sub.add_parser("import", help="Import an external rates file")
    import_parser.add_argument("--src", required=True, help="Path to the candidate rates file")

    update_parser = rates_sub.add_parser("update", help="Diff stored rates against a fetched file")
    update_parser.add_argument("--apply", action="store_true", help="Write the merged rows back")

    rates_sub.add_parser("check", help="Report whether stored rates are stale")


def _parse_rate_pairs(pairs: list[str]) -> dict[str, float]:
    """Parse ``["key=value", ...]`` CLI pairs into a ``{key: float}`` mapping.

    Raises:
        RatesIoError: If a pair is missing its ``=`` separator/key or its value
            is not a number, so the error is reported cleanly rather than
            surfacing as an uncaught ``ValueError``.
    """
    rates: dict[str, float] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise RatesIoError(f"Malformed --rate pair {pair!r}: expected KEY=VALUE")
        try:
            rates[key] = float(value)
        except ValueError as exc:
            raise RatesIoError(
                f"Invalid --rate value for {key!r}: {value!r} is not a number"
            ) from exc
    return rates


def _not_implemented_fetcher() -> dict[str, Any]:
    """Placeholder fetcher for ``rates update``/``rates check``.

    No OEB/Alectra scraping endpoint is wired up yet; the CLI supplies a real
    fetcher when that source is implemented (see :mod:`emporia_hydro.ratesio`).
    """
    raise RatesIoError("rates update/check has no fetcher configured yet")


def _handle_list_devices(args: argparse.Namespace) -> int:
    """Handle ``list-devices``: connect to the Emporia cloud and print channels."""
    vue = connect(args.config_dir)
    for channel, _channel_obj in discover_channels(vue):
        print(f"{channel.device_name}\t{channel.channel_num}\t{channel.name}\t{channel.role}")
    return 0


def _handle_pull(args: argparse.Namespace) -> int:
    """Handle ``pull``: fetch usage from the Emporia cloud and cache it to CSV."""
    vue = connect(args.config_dir)
    channels_with_objs = discover_channels(vue)
    end = args.end if args.end is not None else _today()
    start = args.start if args.start is not None else end - timedelta(days=1)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    # --end is an inclusive local calendar day, but pull_usage's end is the
    # EXCLUSIVE window boundary. Advance one day so the whole --end day is
    # pulled -- otherwise the entire end day (notably "today") is dropped.
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    usages = pull_usage(vue, channels_with_objs, start_dt, end_dt, scale=args.scale)
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(usages, csv_path)
    # Persist the discovered channel roles so offline commands can reconstruct
    # the whole-home Mains total and per-device breakdown without the cloud.
    channels_path = Path(args.channels)
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    write_channels([channel for channel, _obj in channels_with_objs], channels_path)
    print(f"Wrote {len(usages)} usage intervals to {csv_path}")
    return 0


def _handle_rates_show(args: argparse.Namespace) -> int:
    """Handle ``rates show``: print the active TOU/ULO buckets right now."""
    config = load_config(args.config_dir)
    result = rates_show(config)
    for plan, active in result.active.items():
        print(f"{plan}: bucket={active.bucket} rate={active.rate}")
    print(f"tiered_threshold_kwh={result.tiered_threshold_kwh}")
    return 0


def _handle_rates_set(args: argparse.Namespace) -> int:
    """Handle ``rates set``: append or replace a price row in rates.json."""
    rates_set(
        args.config_dir,
        plan=args.plan,
        effective=args.effective,
        rates=_parse_rate_pairs(args.rate),
        expiry=args.expiry,
    )
    print(f"Set {args.plan} rate row effective {args.effective}")
    return 0


def _handle_rates_import(args: argparse.Namespace) -> int:
    """Handle ``rates import``: validate and import an external rates file."""
    result = rates_import(args.src, args.config_dir)
    print(f"Imported plans={result['plans']} rows={result['rows']}")
    return 0


def _handle_rates_update(args: argparse.Namespace) -> int:
    """Handle ``rates update``: diff stored rates against a fetched file."""
    diff: UpdateDiff = rates_update(
        args.config_dir, fetcher=_not_implemented_fetcher, apply=args.apply
    )
    print(f"added={len(diff.added)} changed={len(diff.changed)} unchanged={diff.unchanged}")
    return 0


def _handle_rates_check(args: argparse.Namespace) -> int:
    """Handle ``rates check``: report whether stored rates are stale."""
    result: CheckResult = rates_check(args.config_dir, fetcher=_not_implemented_fetcher)
    print("Rates are up to date" if not result.stale else "Rates are STALE")
    return 0


_RATES_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "show": _handle_rates_show,
    "set": _handle_rates_set,
    "import": _handle_rates_import,
    "update": _handle_rates_update,
    "check": _handle_rates_check,
}


def _handle_rates(args: argparse.Namespace) -> int:
    """Dispatch ``rates <action>`` to the matching handler."""
    return _RATES_HANDLERS[args.rates_action](args)


def _load_usage(args: argparse.Namespace) -> list[IntervalUsage]:
    """Read cached usage from ``args.csv``."""
    return read_csv(args.csv)


def _load_channels(args: argparse.Namespace) -> list[Channel]:
    """Read cached channel metadata from ``args.channels`` if it exists.

    Returns an empty list when the cache is absent, so a run with only a usage
    CSV still works -- every channel is then treated as a branch circuit and
    the whole-home Mains total is reported as 0 until ``pull`` caches the roles.
    """
    if Path(args.channels).is_file():
        return read_channels(args.channels)
    return []


def _filter_by_local_date(
    usages: list[IntervalUsage], config: RatesConfig, start: date, end: date
) -> list[IntervalUsage]:
    """Return the usages whose local date falls within ``[start, end]`` inclusive."""
    return [u for u in usages if start <= u.ts.astimezone(config.zone).date() <= end]


def _handle_cost(args: argparse.Namespace) -> int:
    """Handle ``cost``: price cached usage under a plan, optionally with a full bill."""
    config = load_config(args.config_dir)
    tariff = load_tariff(args.config_dir)
    usages = _filter_by_local_date(_load_usage(args), config, args.start, args.end)
    breakdown = price_usage(usages, _load_channels(args), config, plan=args.plan)

    print(f"Whole-home: {breakdown.whole_home_kwh:.2f} kWh, ${breakdown.whole_home_cost:.2f}")
    for bucket, (kwh, cost) in sorted(breakdown.by_bucket.items()):
        print(f"  {bucket}: {kwh:.2f} kWh, ${cost:.2f}")

    if args.full:
        estimate = bill_estimate(
            whole_home_kwh=breakdown.whole_home_kwh,
            commodity_cost=breakdown.whole_home_cost,
            on=args.start,
            tariff=tariff,
        )
        print(f"Full bill total: ${estimate.total:.2f}")
    return 0


def _handle_compare(args: argparse.Namespace) -> int:
    """Handle ``compare``: reprice cached usage under all three plans."""
    config = load_config(args.config_dir)
    tariff = load_tariff(args.config_dir)
    settings = load_settings(args.config_dir)
    usages = _load_usage(args)
    result = compare_plans(
        usages, _load_channels(args), config, tariff, settings, start=args.start, end=args.end
    )

    for plan, total in result.totals_by_plan.items():
        print(f"{plan}: ${total.commodity_cost:.2f} commodity, ${total.full_total:.2f} full")
    savings = result.overall_savings_vs_current
    print(f"Cheapest overall: {result.overall_cheapest_plan}, savings ${savings:.2f}")
    return 0


def _handle_predict(args: argparse.Namespace) -> int:
    """Handle ``predict``: predict the full-cycle bill for the current period."""
    config = load_config(args.config_dir)
    tariff = load_tariff(args.config_dir)
    settings = load_settings(args.config_dir)
    usages = _load_usage(args)
    result = predict_bill(
        usages, _load_channels(args), config, tariff, settings, on=_today(), plan=args.plan
    )

    print(f"Predicted: {result.predicted_kwh:.2f} kWh, ${result.predicted_full.total:.2f}")
    return 0


def _handle_trends(args: argparse.Namespace) -> int:
    """Handle ``trends``: print per-day whole-home usage/cost stats."""
    config = load_config(args.config_dir)
    usages = _load_usage(args)
    stats = daily_series(usages, _load_channels(args), config)

    if not stats:
        print("No usage data available")
        return 0
    for stat in stats:
        print(f"{stat.day.isoformat()}: {stat.kwh:.2f} kWh, ${stat.cost:.2f}")
    return 0


def _handle_ytd(args: argparse.Namespace) -> int:
    """Handle ``ytd``: print the year-to-date whole-home rollup."""
    config = load_config(args.config_dir)
    tariff = load_tariff(args.config_dir)
    usages = _load_usage(args)
    summary = ytd_summary(
        usages, _load_channels(args), config, tariff, on=_today(), plan=args.plan
    )

    kwh, total = summary.whole_home_kwh, summary.full_total
    print(f"Year {summary.year} to date: {kwh:.2f} kWh, ${total:.2f}")
    return 0


def _handle_report(args: argparse.Namespace) -> int:
    """Handle ``report``: orchestrate a full dashboard refresh for the current cycle."""
    config = load_config(args.config_dir)
    tariff = load_tariff(args.config_dir)
    settings = load_settings(args.config_dir)
    usages = _load_usage(args)
    channels = _load_channels(args)
    on = _today()
    period = billing_current_period(on, settings)
    label = args.label if args.label is not None else period.label
    plan = settings.current_plan

    period_usages = _filter_by_local_date(usages, config, period.start, period.end)
    cost = price_usage(period_usages, channels, config, plan=plan)
    comparison = compare_plans(
        usages, channels, config, tariff, settings, start=period.start, end=period.end
    )
    prediction = predict_bill(usages, channels, config, tariff, settings, on=on, plan=plan)
    day_stats = daily_series(period_usages, channels, config)
    ytd = ytd_summary(usages, channels, config, tariff, on=on, plan=plan)

    out_dir = Path(args.out_dir)
    generate_report(
        cost=cost,
        comparison=comparison,
        prediction=prediction,
        day_stats=day_stats,
        ytd=ytd,
        settings=settings,
        out_dir=out_dir,
        label=label,
        snapshot_date=on,
    )
    # Publish the stylesheet into out_dir/static so the served dashboard is styled.
    publish_css(out_dir)
    print(f"Generated report {label}")
    return 0


def _handle_serve(args: argparse.Namespace) -> int:
    """Handle ``serve``: serve the generated dashboard over local HTTP."""
    settings = load_settings(args.config_dir)
    host = settings.server.get("host", "127.0.0.1")
    port = args.port if args.port is not None else settings.server.get("port", 8765)
    serve(Path(args.out_dir), host=host, port=port)
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "list-devices": _handle_list_devices,
    "pull": _handle_pull,
    "rates": _handle_rates,
    "cost": _handle_cost,
    "compare": _handle_compare,
    "predict": _handle_predict,
    "trends": _handle_trends,
    "ytd": _handle_ytd,
    "report": _handle_report,
    "serve": _handle_serve,
}


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand handler.

    Args:
        argv: Command-line arguments (excluding the program name). Defaults
            to :data:`sys.argv[1:]` when None.

    Returns:
        ``0`` on success; ``1`` when a known application error is caught and
        printed as a clean message to stderr. Argparse usage errors raise
        :class:`SystemExit` with code ``2``, which propagates uncaught.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return _HANDLERS[args.command](args)
    except _KNOWN_ERRORS as exc:
        print(str(exc), file=sys.stderr)
        return 1
