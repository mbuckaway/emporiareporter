# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.cli - COMPLETE test suite written FIRST."""

import argparse
import json
import re
import runpy
import sys
from datetime import UTC, date, datetime

import pytest

from emporia_hydro import cli
from emporia_hydro.billing import BillingError
from emporia_hydro.compare import CompareError
from emporia_hydro.cost import CostConfigError
from emporia_hydro.ingest import IngestError
from emporia_hydro.models import Channel, IntervalUsage
from emporia_hydro.ratesio import RatesIoError
from emporia_hydro.report import ReportError

RATES_JSON = {
    "timezone": "America/Toronto",
    "plans": {
        "tou": {
            "prices": [
                {
                    "effective": "2026-01-01",
                    "expiry": None,
                    "off": 0.076,
                    "mid": 0.122,
                    "on": 0.158,
                }
            ]
        },
        "ulo": {
            "prices": [
                {
                    "effective": "2026-01-01",
                    "expiry": None,
                    "overnight": 0.024,
                    "weekend_off": 0.076,
                    "mid": 0.122,
                    "on": 0.284,
                }
            ]
        },
        "tiered": {
            "prices": [
                {"effective": "2026-01-01", "expiry": None, "tier1": 0.076, "tier2": 0.092}
            ],
            "thresholds": {"summer_kwh": 600, "winter_kwh": 1000},
        },
    },
    "schedule": {
        "tou": {
            "summer": {"off": [[19, 24], [0, 7]], "mid": [[7, 11], [17, 19]], "on": [[11, 17]]},
            "winter": {"off": [[19, 24], [0, 7]], "mid": [[7, 11], [17, 19]], "on": [[11, 17]]},
        },
        "ulo": {
            "overnight": [[23, 24], [0, 7]],
            "weekend_off": [],
            "mid": [[7, 11], [17, 23]],
            "on": [[11, 17]],
        },
    },
    "holidays": {"rules": [], "overrides": []},
}

TARIFF_JSON = {
    "delivery": [
        {
            "effective": "2026-01-01",
            "expiry": None,
            "fixed_monthly": 25.0,
            "smart_metering_monthly": 1.0,
            "sss_monthly": 0.5,
            "variable_per_kwh": 0.02,
        }
    ],
    "oer": [{"effective": "2026-01-01", "expiry": None, "rate": 0.11}],
    "hst": 0.13,
}

SETTINGS_JSON = {
    "timezone": "America/Toronto",
    "current_plan": "tou",
    "billing_cycle": {"mode": "calendar_month"},
    "server": {"host": "127.0.0.1", "port": 8765},
    "output": {"reports_dir": "reports", "data_dir": "data"},
}


@pytest.fixture
def config_dir(tmp_path):
    """Write a minimal, valid rates/tariff/settings config trio to tmp_path."""
    (tmp_path / "rates.json").write_text(json.dumps(RATES_JSON), encoding="utf-8")
    (tmp_path / "tariff.json").write_text(json.dumps(TARIFF_JSON), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(SETTINGS_JSON), encoding="utf-8")
    return tmp_path


@pytest.fixture
def usage_csv(tmp_path):
    """Write a minimal, valid usage CSV cache to tmp_path/usage.csv."""
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text(
        "ts_utc,scale,device_gid,channel,kwh\n2026-07-06T16:00:00+00:00,1H,1,1,10.0\n",
        encoding="utf-8",
    )
    return csv_path


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """Pin cli._today() to a fixed date so date-default tests are deterministic."""
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 7, 15))
    return date(2026, 7, 15)


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_called_returnsargumentparserinstance():
    parser = cli.build_parser()

    assert isinstance(parser, argparse.ArgumentParser)


@pytest.mark.parametrize(
    "argv",
    [
        ["list-devices"],
        ["pull"],
        ["rates", "show"],
        ["cost", "--start", "2026-07-01", "--end", "2026-07-31"],
        ["compare", "--start", "2026-07-01", "--end", "2026-07-31"],
        ["predict"],
        ["trends"],
        ["ytd"],
        ["report"],
        ["serve"],
    ],
    ids=[
        "list-devices",
        "pull",
        "rates-show",
        "cost",
        "compare",
        "predict",
        "trends",
        "ytd",
        "report",
        "serve",
    ],
)
def test_build_parser_everysubcommand_parsesknownargswithoutraising(argv):
    parser = cli.build_parser()

    namespace = parser.parse_args(argv)

    assert namespace.command == argv[0]


def test_build_parser_noargs_raisessystemexitcode2():
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])

    assert exc_info.value.code == 2


def test_build_parser_unknownsubcommand_raisessystemexitcode2():
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["bogus-command"])

    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# main() - usage errors (argparse SystemExit code 2 propagates)
# ---------------------------------------------------------------------------


def test_main_noargs_returnsexitcode2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])

    assert exc_info.value.code == 2


def test_main_unknownsubcommand_returnsexitcode2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["bogus-command"])

    assert exc_info.value.code == 2


def test_main_ratesmissingaction_returnsexitcode2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["rates"])

    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# main() - list-devices (network I/O boundary: ingest.connect/discover_channels)
# ---------------------------------------------------------------------------


def test_main_listdevices_callsconnectanddiscoverchannels_returns0(
    config_dir, monkeypatch, capsys
):
    fake_vue = object()
    connect_calls = []
    discover_calls = []

    def _fake_connect(config_dir_arg):
        connect_calls.append(config_dir_arg)
        return fake_vue

    def _fake_discover(vue):
        discover_calls.append(vue)
        return [
            (
                Channel(
                    device_gid=1,
                    device_name="Home",
                    channel_num="1",
                    name="Mains",
                    role="mains",
                ),
                object(),
            )
        ]

    monkeypatch.setattr(cli, "connect", _fake_connect)
    monkeypatch.setattr(cli, "discover_channels", _fake_discover)

    exit_code = cli.main(["--config-dir", str(config_dir), "list-devices"])

    assert exit_code == 0
    assert connect_calls == [str(config_dir)]
    assert discover_calls == [fake_vue]
    assert "Mains" in capsys.readouterr().out


def test_main_listdevices_ingesterror_returns1andprintsmessage(config_dir, monkeypatch, capsys):
    def _raise_ingest_error(config_dir_arg):
        raise IngestError("Emporia login failed for user 'bob'")

    monkeypatch.setattr(cli, "connect", _raise_ingest_error)

    exit_code = cli.main(["--config-dir", str(config_dir), "list-devices"])

    assert exit_code == 1
    assert "Emporia login failed for user 'bob'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - pull (network I/O boundary: connect/discover_channels/pull_usage/write_csv)
# ---------------------------------------------------------------------------


def test_main_pull_callsconnectdiscoverpullandwrite_returns0(config_dir, monkeypatch, tmp_path):
    fake_vue = object()
    fake_channels = [
        (
            Channel(device_gid=1, device_name="Home", channel_num="1", name="Mains", role="mains"),
            object(),
        )
    ]
    fake_usages = [
        IntervalUsage(
            ts=datetime(2026, 7, 15, 16, tzinfo=UTC),
            scale="1H",
            device_gid=1,
            channel="1",
            kwh=1.5,
        )
    ]
    pull_calls = []
    write_calls = []
    channels_calls = []

    monkeypatch.setattr(cli, "connect", lambda config_dir_arg: fake_vue)
    monkeypatch.setattr(cli, "discover_channels", lambda vue: fake_channels)

    def _fake_pull_usage(vue, channels_with_objs, start, end, scale="1H"):
        pull_calls.append((vue, channels_with_objs, start, end, scale))
        return fake_usages

    def _fake_write_csv(usages, path):
        write_calls.append((usages, path))

    def _fake_write_channels(channels, path):
        channels_calls.append((channels, path))

    monkeypatch.setattr(cli, "pull_usage", _fake_pull_usage)
    monkeypatch.setattr(cli, "write_csv", _fake_write_csv)
    monkeypatch.setattr(cli, "write_channels", _fake_write_channels)

    csv_path = tmp_path / "data" / "usage.csv"
    channels_path = tmp_path / "data" / "channels.json"
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "pull",
            "--scale",
            "1H",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-15",
            "--csv",
            str(csv_path),
            "--channels",
            str(channels_path),
        ]
    )

    assert exit_code == 0
    assert channels_calls == [
        (
            [
                Channel(
                    device_gid=1, device_name="Home", channel_num="1", name="Mains", role="mains"
                )
            ],
            channels_path,
        )
    ]
    # --end 2026-07-15 is inclusive; pull_usage's end is exclusive, so the
    # whole 07-15 day is covered by passing 07-16T00:00 as the boundary.
    assert pull_calls == [
        (
            fake_vue,
            fake_channels,
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 16, tzinfo=UTC),
            "1H",
        )
    ]
    assert write_calls == [(fake_usages, csv_path)]


def test_main_pull_defaultdates_usestodayforend(config_dir, monkeypatch, tmp_path, fixed_today):
    fake_vue = object()
    pull_calls = []

    monkeypatch.setattr(cli, "connect", lambda config_dir_arg: fake_vue)
    monkeypatch.setattr(cli, "discover_channels", lambda vue: [])

    def _fake_pull_usage(vue, channels_with_objs, start, end, scale="1H"):
        pull_calls.append((start, end, scale))
        return []

    monkeypatch.setattr(cli, "pull_usage", _fake_pull_usage)
    monkeypatch.setattr(cli, "write_csv", lambda usages, path: None)
    monkeypatch.setattr(cli, "write_channels", lambda channels, path: None)

    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "pull",
            "--csv",
            str(tmp_path / "usage.csv"),
            "--channels",
            str(tmp_path / "channels.json"),
        ]
    )

    assert exit_code == 0
    # Default end is today (2026-07-15); the exclusive boundary is advanced one
    # day so today's usage is included, and the default start is one day back.
    assert pull_calls == [
        (datetime(2026, 7, 14, tzinfo=UTC), datetime(2026, 7, 16, tzinfo=UTC), "1H")
    ]


def test_main_cost_withchannelscache_reportsnonzerowholehome(
    config_dir, usage_csv, tmp_path, capsys
):
    # A channels cache marking channel "1" as mains lets offline cost attribute
    # the whole-home total, instead of the 0 kWh seen with no roles.
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "device_gid": 1,
                    "device_name": "Home",
                    "channel_num": "1",
                    "name": "Mains",
                    "role": "mains",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "cost",
            "--csv",
            str(usage_csv),
            "--channels",
            str(channels_path),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 0
    assert "Whole-home: 10.00 kWh" in capsys.readouterr().out


def test_main_cost_nochannelscache_reportszerowholehome(config_dir, usage_csv, tmp_path, capsys):
    # Without a channels cache, every channel defaults to a branch circuit, so
    # the mains-based whole-home total is 0 (documents the fallback behaviour).
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "cost",
            "--csv",
            str(usage_csv),
            "--channels",
            str(tmp_path / "absent.json"),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 0
    assert "Whole-home: 0.00 kWh" in capsys.readouterr().out


def test_main_pull_ingesterror_returns1andprintsmessage(config_dir, monkeypatch, tmp_path):
    def _raise_ingest_error(config_dir_arg):
        raise IngestError("Keys file not found: config/keys.json")

    monkeypatch.setattr(cli, "connect", _raise_ingest_error)

    exit_code = cli.main(
        ["--config-dir", str(config_dir), "pull", "--csv", str(tmp_path / "usage.csv")]
    )

    assert exit_code == 1


# ---------------------------------------------------------------------------
# main() - rates show/set/import (offline; ratesio module functions)
# ---------------------------------------------------------------------------


def test_main_ratesshow_printsactivebucketsandreturns0(config_dir, capsys):
    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "show"])

    assert exit_code == 0
    assert "tou" in capsys.readouterr().out


def test_main_ratesshow_ratesconfigerror_returns1(tmp_path, capsys):
    (tmp_path / "rates.json").write_text("{not valid json", encoding="utf-8")

    exit_code = cli.main(["--config-dir", str(tmp_path), "rates", "show"])

    assert exit_code == 1
    assert "Invalid JSON in rates config file" in capsys.readouterr().err


def test_main_ratesset_writesrowandreturns0(config_dir):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "rates",
            "set",
            "--plan",
            "tou",
            "--effective",
            "2027-01-01",
            "--rate",
            "off=0.08",
            "--rate",
            "mid=0.13",
            "--rate",
            "on=0.17",
        ]
    )

    assert exit_code == 0
    written = json.loads((config_dir / "rates.json").read_text(encoding="utf-8"))
    assert written["plans"]["tou"]["prices"][0]["effective"] == "2027-01-01"


def test_main_ratesset_ratesioerror_returns1(config_dir, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "rates",
            "set",
            "--plan",
            "bogus",
            "--effective",
            "2027-01-01",
            "--rate",
            "off=0.08",
        ]
    )

    assert exit_code == 1
    assert "Unknown plan: 'bogus'" in capsys.readouterr().err


@pytest.mark.parametrize("bad_rate", ["off", "off=", "off=abc"])
def test_main_ratesset_malformedrate_returns1(config_dir, bad_rate):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "rates",
            "set",
            "--plan",
            "tou",
            "--effective",
            "2027-01-01",
            "--rate",
            bad_rate,
        ]
    )

    assert exit_code == 1


def test_main_ratesimport_copiesfileandreturns0(config_dir, tmp_path):
    src = tmp_path / "external_rates.json"
    src.write_text(json.dumps(RATES_JSON), encoding="utf-8")

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "import", "--src", str(src)])

    assert exit_code == 0


def test_main_ratesimport_ratesioerror_returns1(config_dir, tmp_path, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "rates",
            "import",
            "--src",
            str(tmp_path / "missing.json"),
        ]
    )

    assert exit_code == 1
    assert "Rates import file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - rates update/check (network I/O boundary: injected fetcher via ratesio)
# ---------------------------------------------------------------------------


def test_main_ratesupdate_callsratesupdatewithfetcher_returns0(config_dir, monkeypatch):
    calls = []

    def _fake_rates_update(config_dir_arg, *, fetcher, apply=False):
        calls.append((config_dir_arg, apply, fetcher))
        return cli.UpdateDiff(added=[], changed=[], unchanged=1, applied=apply)

    monkeypatch.setattr(cli, "rates_update", _fake_rates_update)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "update"])

    assert exit_code == 0
    assert [(config_dir_arg, apply) for config_dir_arg, apply, _fetcher in calls] == [
        (str(config_dir), False)
    ]


def test_notimplementedfetcher_called_raisesratesioerror():
    with pytest.raises(
        RatesIoError, match=re.escape("rates update/check has no fetcher configured yet")
    ):
        cli._not_implemented_fetcher()


# ---------------------------------------------------------------------------
# __main__.py - module entry point guard
# ---------------------------------------------------------------------------


def test_dundermain_norealargs_raisessystemexitcode2(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["emporia_hydro"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("emporia_hydro.__main__", run_name="__main__")

    assert exc_info.value.code == 2


def test_dundermain_importedasplainmodule_doesnotinvokemain(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "main", lambda argv=None: calls.append(argv) or 0)

    module_globals = runpy.run_module("emporia_hydro.__main__", run_name="not_main")

    assert (calls, module_globals["main"]) == ([], cli.main)


def test_main_ratesupdate_applyflag_passesapplytrue(config_dir, monkeypatch):
    calls = []

    def _fake_rates_update(config_dir_arg, *, fetcher, apply=False):
        calls.append(apply)
        return cli.UpdateDiff(added=[], changed=[], unchanged=1, applied=apply)

    monkeypatch.setattr(cli, "rates_update", _fake_rates_update)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "update", "--apply"])

    assert exit_code == 0
    assert calls == [True]


def test_main_ratesupdate_ratesioerror_returns1(config_dir, monkeypatch, capsys):
    def _raise_ratesio_error(config_dir_arg, *, fetcher, apply=False):
        raise RatesIoError("Rates file not found: rates.json")

    monkeypatch.setattr(cli, "rates_update", _raise_ratesio_error)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "update"])

    assert exit_code == 1
    assert "Rates file not found" in capsys.readouterr().err


def test_main_ratescheck_stalefalse_returns0(config_dir, monkeypatch, capsys):
    def _fake_rates_check(config_dir_arg, *, fetcher):
        return cli.CheckResult(
            stale=False, diff=cli.UpdateDiff(added=[], changed=[], unchanged=3, applied=False)
        )

    monkeypatch.setattr(cli, "rates_check", _fake_rates_check)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "check"])

    assert exit_code == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_main_ratescheck_staletrue_returns0andprintsstalenotice(config_dir, monkeypatch, capsys):
    def _fake_rates_check(config_dir_arg, *, fetcher):
        return cli.CheckResult(
            stale=True,
            diff=cli.UpdateDiff(added=[{"plan": "tou"}], changed=[], unchanged=0, applied=False),
        )

    monkeypatch.setattr(cli, "rates_check", _fake_rates_check)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "check"])

    assert exit_code == 0
    assert "stale" in capsys.readouterr().out.lower()


def test_main_ratescheck_ratesioerror_returns1(config_dir, monkeypatch, capsys):
    def _raise_ratesio_error(config_dir_arg, *, fetcher):
        raise RatesIoError("Rates file not found: rates.json")

    monkeypatch.setattr(cli, "rates_check", _raise_ratesio_error)

    exit_code = cli.main(["--config-dir", str(config_dir), "rates", "check"])

    assert exit_code == 1


# ---------------------------------------------------------------------------
# main() - cost (offline: rates.load_config + cost.load_tariff + ingest.read_csv)
# ---------------------------------------------------------------------------


def test_main_cost_pricesusageandreturns0(config_dir, usage_csv, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "cost",
            "--csv",
            str(usage_csv),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 0
    assert "kWh" in capsys.readouterr().out


def test_main_cost_fullflag_printsbillestimate(config_dir, usage_csv, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "cost",
            "--csv",
            str(usage_csv),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
            "--full",
        ]
    )

    assert exit_code == 0
    assert "total" in capsys.readouterr().out.lower()


def test_main_cost_missingcsv_returns1(config_dir, tmp_path, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "cost",
            "--csv",
            str(tmp_path / "missing.csv"),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 1
    assert "CSV cache file not found" in capsys.readouterr().err


def test_main_cost_missingratesconfig_returns1(tmp_path, usage_csv, capsys):
    (tmp_path / "tariff.json").write_text(json.dumps(TARIFF_JSON), encoding="utf-8")

    exit_code = cli.main(
        [
            "--config-dir",
            str(tmp_path),
            "cost",
            "--csv",
            str(usage_csv),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 1
    assert "Rates config file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - compare (offline)
# ---------------------------------------------------------------------------


def test_main_compare_printsmatrixandreturns0(config_dir, usage_csv, capsys):
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "compare",
            "--csv",
            str(usage_csv),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 0
    assert "tiered" in capsys.readouterr().out.lower()


def test_main_compare_compareerror_returns1(config_dir, usage_csv, monkeypatch, capsys):
    def _raise_compare_error(*args, **kwargs):
        raise CompareError("current_plan must be one of tou/ulo/tiered, got 'bogus'")

    monkeypatch.setattr(cli, "compare_plans", _raise_compare_error)

    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "compare",
            "--csv",
            str(usage_csv),
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-31",
        ]
    )

    assert exit_code == 1
    assert "current_plan must be one of tou/ulo/tiered" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - predict (offline; uses _today() default)
# ---------------------------------------------------------------------------


def test_main_predict_printspredictionandreturns0(config_dir, usage_csv, capsys):
    exit_code = cli.main(["--config-dir", str(config_dir), "predict", "--csv", str(usage_csv)])

    assert exit_code == 0
    assert "predicted" in capsys.readouterr().out.lower()


def test_main_predict_billingerror_returns1(config_dir, usage_csv, monkeypatch, capsys):
    def _raise_billing_error(*args, **kwargs):
        raise BillingError("No billing period covers date 2026-07-15")

    monkeypatch.setattr(cli, "predict_bill", _raise_billing_error)

    exit_code = cli.main(["--config-dir", str(config_dir), "predict", "--csv", str(usage_csv)])

    assert exit_code == 1
    assert "No billing period covers date" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - trends (offline)
# ---------------------------------------------------------------------------


def test_main_trends_printsdailyseriesandreturns0(config_dir, usage_csv, capsys):
    exit_code = cli.main(["--config-dir", str(config_dir), "trends", "--csv", str(usage_csv)])

    assert exit_code == 0
    assert "2026-07-06" in capsys.readouterr().out


def test_main_trends_norows_printsnodatamessagereturns0(config_dir, tmp_path, capsys):
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("ts_utc,scale,device_gid,channel,kwh\n", encoding="utf-8")

    exit_code = cli.main(["--config-dir", str(config_dir), "trends", "--csv", str(empty_csv)])

    assert exit_code == 0
    assert "no usage data" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# main() - ytd (offline)
# ---------------------------------------------------------------------------


def test_main_ytd_printssummaryandreturns0(config_dir, usage_csv, capsys):
    exit_code = cli.main(["--config-dir", str(config_dir), "ytd", "--csv", str(usage_csv)])

    assert exit_code == 0
    assert "year" in capsys.readouterr().out.lower()


def test_main_ytd_costconfigerror_returns1(config_dir, usage_csv, monkeypatch, capsys):
    def _raise_cost_config_error(*args, **kwargs):
        raise CostConfigError("No delivery row covers date 2026-07-15")

    monkeypatch.setattr(cli, "ytd_summary", _raise_cost_config_error)

    exit_code = cli.main(["--config-dir", str(config_dir), "ytd", "--csv", str(usage_csv)])

    assert exit_code == 1
    assert "No delivery row covers date" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - report (calls report.generate_report; I/O boundary mocked)
# ---------------------------------------------------------------------------


def test_main_report_callsgeneratereportwithexactkwargs_returns0(
    config_dir, usage_csv, monkeypatch, tmp_path
):
    calls = []
    publish_calls = []

    def _fake_generate_report(**kwargs):
        calls.append(kwargs)
        return {"label": kwargs["label"]}

    monkeypatch.setattr(cli, "generate_report", _fake_generate_report)
    monkeypatch.setattr(cli, "publish_css", lambda out_dir: publish_calls.append(out_dir))

    out_dir = tmp_path / "reports"
    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "report",
            "--csv",
            str(usage_csv),
            "--out-dir",
            str(out_dir),
            "--label",
            "2026-07",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["label"] == "2026-07"
    assert calls[0]["out_dir"] == out_dir
    assert calls[0]["snapshot_date"] == date(2026, 7, 15)
    assert publish_calls == [out_dir]


def test_main_report_defaultlabel_usescurrentperiodlabel(
    config_dir, usage_csv, monkeypatch, tmp_path
):
    calls = []

    def _fake_generate_report(**kwargs):
        calls.append(kwargs)
        return {"label": kwargs["label"]}

    monkeypatch.setattr(cli, "generate_report", _fake_generate_report)
    monkeypatch.setattr(cli, "publish_css", lambda out_dir: None)

    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "report",
            "--csv",
            str(usage_csv),
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 0
    assert calls[0]["label"] == "2026-07"


def test_main_report_reporterror_returns1(config_dir, usage_csv, monkeypatch, tmp_path, capsys):
    def _raise_report_error(**kwargs):
        raise ReportError("Tailwind CSS build failed: boom")

    monkeypatch.setattr(cli, "generate_report", _raise_report_error)

    exit_code = cli.main(
        [
            "--config-dir",
            str(config_dir),
            "report",
            "--csv",
            str(usage_csv),
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 1
    assert "Tailwind CSS build failed" in capsys.readouterr().err


def test_main_report_ratesconfigerror_fromloadconfig_returns1(tmp_path, usage_csv, capsys):
    (tmp_path / "tariff.json").write_text(json.dumps(TARIFF_JSON), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(SETTINGS_JSON), encoding="utf-8")

    exit_code = cli.main(
        [
            "--config-dir",
            str(tmp_path),
            "report",
            "--csv",
            str(usage_csv),
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 1
    assert "Rates config file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - serve (network I/O boundary: server.serve)
# ---------------------------------------------------------------------------


def test_main_serve_callsservewithexactkwargs_returns0(config_dir, monkeypatch, tmp_path):
    calls = []

    def _fake_serve(directory, host="127.0.0.1", port=8765):
        calls.append((directory, host, port))

    monkeypatch.setattr(cli, "serve", _fake_serve)

    out_dir = tmp_path / "reports"
    exit_code = cli.main(
        ["--config-dir", str(config_dir), "serve", "--out-dir", str(out_dir), "--port", "9999"]
    )

    assert exit_code == 0
    assert calls == [(out_dir, "127.0.0.1", 9999)]


def test_main_serve_defaultport_usessettingsport(config_dir, monkeypatch, tmp_path):
    calls = []

    def _fake_serve(directory, host="127.0.0.1", port=8765):
        calls.append(port)

    monkeypatch.setattr(cli, "serve", _fake_serve)

    exit_code = cli.main(
        ["--config-dir", str(config_dir), "serve", "--out-dir", str(tmp_path / "reports")]
    )

    assert exit_code == 0
    assert calls == [8765]


def test_main_serve_billingerror_missingsettings_returns1(tmp_path, capsys):
    (tmp_path / "rates.json").write_text(json.dumps(RATES_JSON), encoding="utf-8")
    (tmp_path / "tariff.json").write_text(json.dumps(TARIFF_JSON), encoding="utf-8")

    exit_code = cli.main(
        ["--config-dir", str(tmp_path), "serve", "--out-dir", str(tmp_path / "reports")]
    )

    assert exit_code == 1
    assert "Settings config file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() - date flag parsing (ISO via date.fromisoformat)
# ---------------------------------------------------------------------------


def test_main_cost_invalidisodate_returnsexitcode2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["cost", "--csv", "x.csv", "--start", "not-a-date", "--end", "2026-07-31"])

    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _today() - the one real clock read, isolated from the autouse monkeypatch
# ---------------------------------------------------------------------------


def test_today_unpatched_returnsrealcurrentlocaldate(monkeypatch):
    monkeypatch.undo()  # restore the module's real date.today()-backed _today

    result = cli._today()

    assert result == date.today()
