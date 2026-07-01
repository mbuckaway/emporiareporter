# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.ingest - COMPLETE test suite written FIRST.

The pyemvue cloud client is the only I/O boundary in this module, so every
test here mocks ``PyEmVue``/its methods and never performs real network I/O.
Fake ``VueDevice``/``VueDeviceChannel`` stand-ins are simple dataclasses since
the real pyemvue classes are plain attribute bags with no behavior we need.
"""

import csv
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from emporia_hydro.ingest import (
    IngestError,
    channel_role,
    connect,
    discover_channels,
    load_keys,
    pull_usage,
    read_channels,
    read_csv,
    write_channels,
    write_csv,
)
from emporia_hydro.models import Channel, IntervalUsage

_SAFE_TEXT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789,_- "


@dataclass
class FakeChannel:
    """Minimal stand-in for pyemvue.device.VueDeviceChannel."""

    device_gid: int = 1
    channel_num: str = "1,2,3"
    name: str = ""
    channel_multiplier: float = 1.0
    channel_type_gid: int = 0
    parent_channel_num: str | None = None
    nested_devices: dict = field(default_factory=dict)


@dataclass
class FakeDevice:
    """Minimal stand-in for pyemvue.device.VueDevice."""

    device_gid: int = 1
    device_name: str = "raw-name"
    display_name: str = ""
    channels: list = field(default_factory=list)
    ev_charger: object | None = None
    outlet: object | None = None


_TS = datetime(2026, 1, 1, 5, tzinfo=UTC)
USAGE_A = IntervalUsage(ts=_TS, scale="1H", device_gid=1, channel="1,2,3", kwh=1.25)
USAGE_B = IntervalUsage(
    ts=_TS + timedelta(hours=1), scale="1H", device_gid=1, channel="1,2,3", kwh=2.5
)
USAGE_C = IntervalUsage(
    ts=_TS + timedelta(hours=2), scale="1H", device_gid=2, channel="5", kwh=0.0
)


# ---------------------------------------------------------------------------
# load_keys
# ---------------------------------------------------------------------------


def test_load_keys_file_present_returns_parsed_dict(tmp_path):
    keys_path = tmp_path / "keys.json"
    keys_path.write_text('{"username": "u", "password": "p"}', encoding="utf-8")

    result = load_keys(config_dir=tmp_path)

    assert result == {"username": "u", "password": "p"}


def test_load_keys_file_missing_raises_ingest_error(tmp_path):
    with pytest.raises(IngestError, match=re.escape("Keys file not found")):
        load_keys(config_dir=tmp_path)


def test_load_keys_custom_filename_reads_that_file(tmp_path):
    custom_path = tmp_path / "custom.json"
    custom_path.write_text('{"a": 1}', encoding="utf-8")

    result = load_keys(config_dir=tmp_path, filename="custom.json")

    assert result == {"a": 1}


def test_load_keys_empty_json_object_returns_empty_dict(tmp_path):
    keys_path = tmp_path / "keys.json"
    keys_path.write_text("{}", encoding="utf-8")

    result = load_keys(config_dir=tmp_path)

    assert result == {}


# ---------------------------------------------------------------------------
# connect
#
# Credentials (keys.json) and the mutable pyemvue token cache (token_cache.json)
# are DELIBERATELY separate files. pyemvue rewrites its token_storage_file on
# every login and does NOT persist the password, so pointing it at keys.json
# would strip the password after the first login and leave the tool unable to
# re-authenticate once the cached refresh token expires. connect() therefore
# points pyemvue at token_cache.json and keeps keys.json read-only.
# ---------------------------------------------------------------------------


def _write_keys(config_dir, **fields):
    (config_dir / "keys.json").write_text(json.dumps(fields), encoding="utf-8")


def _write_token_cache(config_dir):
    (config_dir / "token_cache.json").write_text(
        '{"id_token": "i", "access_token": "a", "refresh_token": "r"}', encoding="utf-8"
    )


def test_connect_cached_token_valid_returns_vue_without_reading_credentials(tmp_path):
    _write_token_cache(tmp_path)  # no keys.json on disk at all
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.return_value = True

        result = connect(config_dir=tmp_path)

    mock_vue.login.assert_called_once_with(token_storage_file=str(tmp_path / "token_cache.json"))
    assert result is mock_vue


def test_connect_no_cache_uses_credentials_and_caches_token(tmp_path):
    _write_keys(tmp_path, username="u@example.com", password="secret")
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.return_value = True

        result = connect(config_dir=tmp_path)

    mock_vue.login.assert_called_once_with(
        username="u@example.com",
        password="secret",
        token_storage_file=str(tmp_path / "token_cache.json"),
    )
    assert result is mock_vue


def test_connect_expired_cache_falls_back_to_stored_credentials(tmp_path):
    _write_token_cache(tmp_path)
    _write_keys(tmp_path, username="u@example.com", password="secret")
    cache = str(tmp_path / "token_cache.json")
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.side_effect = [False, True]

        result = connect(config_dir=tmp_path)

    mock_vue.login.assert_has_calls(
        [
            call(token_storage_file=cache),
            call(username="u@example.com", password="secret", token_storage_file=cache),
        ]
    )
    assert result is mock_vue


def test_connect_corrupt_cache_falls_back_to_stored_credentials(tmp_path):
    _write_token_cache(tmp_path)
    _write_keys(tmp_path, username="u@example.com", password="secret")
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.side_effect = [ValueError("bad token cache"), True]

        result = connect(config_dir=tmp_path)

    assert mock_vue.login.call_count == 2
    assert result is mock_vue


def test_connect_credentials_login_fails_raises_ingest_error(tmp_path):
    _write_keys(tmp_path, username="u@example.com", password="secret")
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.return_value = False

        with pytest.raises(IngestError, match=re.escape("Emporia login failed")):
            connect(config_dir=tmp_path)


def test_connect_missing_credentials_file_raises_ingest_error(tmp_path):
    with (
        patch("emporia_hydro.ingest.PyEmVue"),
        pytest.raises(IngestError, match=re.escape("Keys file not found")),
    ):
        connect(config_dir=tmp_path)


def test_connect_credentials_missing_password_raises_ingest_error(tmp_path):
    _write_keys(tmp_path, username="u@example.com")
    with (
        patch("emporia_hydro.ingest.PyEmVue"),
        pytest.raises(IngestError, match=re.escape("Emporia credentials missing")),
    ):
        connect(config_dir=tmp_path)


def test_connect_credentials_missing_username_raises_ingest_error(tmp_path):
    _write_keys(tmp_path, password="secret")
    with (
        patch("emporia_hydro.ingest.PyEmVue"),
        pytest.raises(IngestError, match=re.escape("Emporia credentials missing")),
    ):
        connect(config_dir=tmp_path)


def test_connect_default_config_dir_uses_config_token_cache():
    with patch("emporia_hydro.ingest.PyEmVue") as mock_pyemvue_cls:
        mock_vue = mock_pyemvue_cls.return_value
        mock_vue.login.return_value = True
        with patch(
            "emporia_hydro.ingest.load_keys",
            return_value={"username": "u@example.com", "password": "secret"},
        ):
            connect()

    mock_vue.login.assert_called_once_with(
        username="u@example.com",
        password="secret",
        token_storage_file=str(Path("config") / "token_cache.json"),
    )


# ---------------------------------------------------------------------------
# channel_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev_charger, outlet, channel_num, expected_role",
    [
        (object(), None, "1", "aux"),
        (None, object(), "1", "aux"),
        (object(), object(), "1", "aux"),
        (None, None, "1,2,3", "mains"),
        (None, None, "Mains", "mains"),
        (None, None, "5", "branch"),
        (None, None, "", "branch"),
    ],
)
def test_channel_role_decision_table_returns_expected_role(
    ev_charger, outlet, channel_num, expected_role
):
    device = FakeDevice(ev_charger=ev_charger, outlet=outlet)

    result = channel_role(device, channel_num)

    assert result == expected_role


@given(
    ev_charger_truthy=st.booleans(),
    outlet_truthy=st.booleans(),
    channel_num=st.text(max_size=20),
)
def test_channel_role_property_always_returns_known_role(
    ev_charger_truthy, outlet_truthy, channel_num
):
    device = FakeDevice(
        ev_charger=object() if ev_charger_truthy else None,
        outlet=object() if outlet_truthy else None,
    )

    result = channel_role(device, channel_num)

    assert result in {"aux", "mains", "branch"}


# ---------------------------------------------------------------------------
# discover_channels
# ---------------------------------------------------------------------------


def test_discover_channels_no_devices_returns_empty_list():
    vue = MagicMock()
    vue.get_devices.return_value = []

    result = discover_channels(vue)

    vue.get_devices.assert_called_once_with()
    assert result == []


def test_discover_channels_device_without_channels_returns_empty_list():
    device = FakeDevice(device_gid=7, device_name="raw", channels=[])
    vue = MagicMock()
    vue.get_devices.return_value = [device]
    vue.populate_device_properties.return_value = device

    result = discover_channels(vue)

    vue.populate_device_properties.assert_called_once_with(device)
    assert result == []


def test_discover_channels_single_channel_builds_one_pair():
    channel_obj = FakeChannel(device_gid=7, channel_num="1,2,3", name="Mains")
    device = FakeDevice(device_gid=7, device_name="Home", channels=[channel_obj])
    vue = MagicMock()
    vue.get_devices.return_value = [device]
    vue.populate_device_properties.return_value = device

    result = discover_channels(vue)

    expected_channel = Channel(
        device_gid=7, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    assert result == [(expected_channel, channel_obj)]


def test_discover_channels_many_channels_builds_multiple_pairs():
    mains = FakeChannel(device_gid=7, channel_num="1,2,3", name="Mains")
    branch = FakeChannel(device_gid=7, channel_num="5", name="Fridge")
    device = FakeDevice(device_gid=7, device_name="Home", channels=[mains, branch])
    vue = MagicMock()
    vue.get_devices.return_value = [device]
    vue.populate_device_properties.return_value = device

    result = discover_channels(vue)

    assert [pair[0].role for pair in result] == ["mains", "branch"]


def test_discover_channels_uses_display_name_when_present():
    channel_obj = FakeChannel(channel_num="1,2,3")
    device = FakeDevice(device_name="raw", display_name="Pretty Name", channels=[channel_obj])
    vue = MagicMock()
    vue.get_devices.return_value = [device]
    vue.populate_device_properties.return_value = device

    result = discover_channels(vue)

    assert result[0][0].device_name == "Pretty Name"


def test_discover_channels_falls_back_to_device_name_when_display_name_blank():
    channel_obj = FakeChannel(channel_num="1,2,3")
    device = FakeDevice(device_name="raw-name", display_name="", channels=[channel_obj])
    vue = MagicMock()
    vue.get_devices.return_value = [device]
    vue.populate_device_properties.return_value = device

    result = discover_channels(vue)

    assert result[0][0].device_name == "raw-name"


# ---------------------------------------------------------------------------
# pull_usage
# ---------------------------------------------------------------------------


def test_pull_usage_unknown_scale_raises_ingest_error():
    vue = MagicMock()

    with pytest.raises(IngestError, match=re.escape("Unknown pyemvue usage scale")):
        pull_usage(
            vue,
            [],
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            scale="1FOO",
        )


def test_pull_usage_no_channels_returns_empty_list():
    vue = MagicMock()

    result = pull_usage(
        vue, [], datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    )

    vue.get_chart_usage.assert_not_called()
    assert result == []


def test_pull_usage_start_dt_none_produces_no_records():
    channel = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    channel_obj = FakeChannel(device_gid=1, channel_num="1,2,3")
    vue = MagicMock()
    vue.get_chart_usage.return_value = ([1.0, 2.0], None)

    result = pull_usage(
        vue,
        [(channel, channel_obj)],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert result == []


@pytest.mark.parametrize(
    "values, expected_kwh",
    [
        ([], []),
        ([1.5], [1.5]),
        ([1.0, None, 2.0], [1.0, 2.0]),
    ],
)
def test_pull_usage_values_series_skips_none_and_builds_records(values, expected_kwh):
    channel = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    channel_obj = FakeChannel(device_gid=1, channel_num="1,2,3")
    start_dt = datetime(2026, 1, 1, tzinfo=UTC)
    vue = MagicMock()
    vue.get_chart_usage.return_value = (values, start_dt)

    result = pull_usage(
        vue, [(channel, channel_obj)], start_dt, start_dt + timedelta(hours=len(values))
    )

    assert [usage.kwh for usage in result] == expected_kwh


def test_pull_usage_naive_start_dt_normalized_to_utc():
    channel = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    channel_obj = FakeChannel(device_gid=1, channel_num="1,2,3")
    naive_start = datetime(2026, 1, 1, 5)
    vue = MagicMock()
    vue.get_chart_usage.return_value = ([1.0], naive_start)

    result = pull_usage(
        vue,
        [(channel, channel_obj)],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert result[0].ts == datetime(2026, 1, 1, 5, tzinfo=UTC)


def test_pull_usage_aware_start_dt_preserved_as_utc():
    channel = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    channel_obj = FakeChannel(device_gid=1, channel_num="1,2,3")
    aware_start = datetime(2026, 1, 1, 5, tzinfo=UTC)
    vue = MagicMock()
    vue.get_chart_usage.return_value = ([1.0], aware_start)

    result = pull_usage(
        vue,
        [(channel, channel_obj)],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert result[0].ts == aware_start


def test_pull_usage_many_channels_calls_get_chart_usage_for_each_channel():
    channel_a = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    obj_a = FakeChannel(device_gid=1, channel_num="1,2,3")
    channel_b = Channel(
        device_gid=1, device_name="Home", channel_num="5", name="Fridge", role="branch"
    )
    obj_b = FakeChannel(device_gid=1, channel_num="5")
    start_dt = datetime(2026, 1, 1, tzinfo=UTC)
    end_dt = datetime(2026, 1, 2, tzinfo=UTC)
    vue = MagicMock()
    vue.get_chart_usage.return_value = ([1.0], start_dt)

    result = pull_usage(
        vue, [(channel_a, obj_a), (channel_b, obj_b)], start_dt, end_dt, scale="1H"
    )

    vue.get_chart_usage.assert_has_calls(
        [
            call(obj_a, start_dt, end_dt, scale="1H", unit="KilowattHours"),
            call(obj_b, start_dt, end_dt, scale="1H", unit="KilowattHours"),
        ]
    )
    assert [usage.channel for usage in result] == ["1,2,3", "5"]


def test_pull_usage_interval_ts_advances_by_scale_hours():
    channel = Channel(
        device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"
    )
    channel_obj = FakeChannel(device_gid=1, channel_num="1,2,3")
    start_dt = datetime(2026, 1, 1, tzinfo=UTC)
    vue = MagicMock()
    vue.get_chart_usage.return_value = ([1.0, 2.0], start_dt)

    result = pull_usage(
        vue, [(channel, channel_obj)], start_dt, start_dt + timedelta(hours=2), scale="1H"
    )

    assert [usage.ts for usage in result] == [start_dt, start_dt + timedelta(hours=1)]


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "usages, expected_row_count",
    [([], 0), ([USAGE_A], 1), ([USAGE_A, USAGE_B, USAGE_C], 3)],
)
def test_write_csv_row_count_matches_usages_length(tmp_path, usages, expected_row_count):
    path = tmp_path / "cache.csv"

    write_csv(usages, path)

    with path.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert len(rows) == expected_row_count


def test_write_csv_single_usage_row_content_matches_fields(tmp_path):
    path = tmp_path / "cache.csv"

    write_csv([USAGE_A], path)

    with path.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert rows[0] == {
        "ts_utc": USAGE_A.ts.isoformat(),
        "scale": "1H",
        "device_gid": "1",
        "channel": "1,2,3",
        "kwh": "1.25",
    }


# ---------------------------------------------------------------------------
# read_csv
# ---------------------------------------------------------------------------


def test_read_csv_file_missing_raises_ingest_error(tmp_path):
    missing_path = tmp_path / "missing.csv"

    with pytest.raises(IngestError, match=re.escape("CSV cache file not found")):
        read_csv(missing_path)


def test_read_csv_header_only_file_returns_empty_list(tmp_path):
    path = tmp_path / "cache.csv"
    write_csv([], path)

    result = read_csv(path)

    assert result == []


def test_read_csv_single_row_returns_one_usage(tmp_path):
    path = tmp_path / "cache.csv"
    write_csv([USAGE_A], path)

    result = read_csv(path)

    assert result == [USAGE_A]


def test_read_csv_many_rows_returns_all_usages_in_order(tmp_path):
    path = tmp_path / "cache.csv"
    write_csv([USAGE_A, USAGE_B, USAGE_C], path)

    result = read_csv(path)

    assert result == [USAGE_A, USAGE_B, USAGE_C]


# ---------------------------------------------------------------------------
# write_csv / read_csv round trip (Hypothesis property test)
# ---------------------------------------------------------------------------


@st.composite
def _interval_usage_strategy(draw):
    return IntervalUsage(
        ts=draw(st.datetimes(timezones=st.just(UTC))),
        scale=draw(st.text(alphabet=_SAFE_TEXT_ALPHABET, min_size=1, max_size=6)),
        device_gid=draw(st.integers(min_value=0, max_value=2_000_000_000)),
        channel=draw(st.text(alphabet=_SAFE_TEXT_ALPHABET, min_size=1, max_size=12)),
        kwh=draw(st.floats(allow_nan=False, allow_infinity=False, width=64)),
    )


@given(usages=st.lists(_interval_usage_strategy(), max_size=5))
def test_write_csv_read_csv_round_trip_preserves_records(usages):
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "cache.csv"
        write_csv(usages, path)
        result = read_csv(path)

    assert result == usages


# ---------------------------------------------------------------------------
# write_channels / read_channels
# ---------------------------------------------------------------------------


def test_read_channels_missing_file_raises_ingest_error(tmp_path):
    with pytest.raises(IngestError, match=re.escape("Channels cache file not found")):
        read_channels(tmp_path / "channels.json")


def test_write_channels_read_channels_round_trip_preserves_roles(tmp_path):
    channels = [
        Channel(device_gid=1, device_name="Home", channel_num="1,2,3", name="Mains", role="mains"),
        Channel(device_gid=1, device_name="Home", channel_num="5", name="Dryer", role="branch"),
        Channel(device_gid=2, device_name="EVSE", channel_num="1", name="EV", role="aux"),
    ]
    path = tmp_path / "channels.json"

    write_channels(channels, path)
    result = read_channels(path)

    assert result == channels


def test_write_channels_empty_list_round_trips_to_empty(tmp_path):
    path = tmp_path / "channels.json"

    write_channels([], path)

    assert read_channels(path) == []
