# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Pull energy usage from the Emporia cloud (via pyemvue) and cache it to CSV.

This module is the sole I/O boundary for the Emporia Vue cloud API. Every
network call goes through a :class:`~pyemvue.PyEmVue` client that callers
obtain from :func:`connect`; everything else here (channel classification,
usage expansion, CSV read/write) is a pure transformation of the data that
client returns, so it stays easy to test without a live network connection.
"""

import csv
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import VueDevice, VueDeviceChannel

from emporia_hydro.models import SCALE_HOURS, Channel, IntervalUsage

__all__ = [
    "IngestError",
    "channel_role",
    "connect",
    "discover_channels",
    "load_keys",
    "pull_usage",
    "read_channels",
    "read_csv",
    "write_channels",
    "write_csv",
]

_CSV_FIELDS = ("ts_utc", "scale", "device_gid", "channel", "kwh")


class IngestError(Exception):
    """Raised when reading Emporia keys/cache or talking to the pyemvue cloud fails."""


def load_keys(
    config_dir: str | os.PathLike = "config", filename: str = "keys.json"
) -> dict[str, Any]:
    """Read and parse the Emporia keys/token cache file.

    Args:
        config_dir: Directory containing the keys file. Defaults to ``"config"``
            relative to the current working directory.
        filename: Name of the keys file within ``config_dir``. Defaults to
            ``"keys.json"``.

    Returns:
        The parsed JSON contents as a dict.

    Raises:
        IngestError: If the keys file does not exist.
    """
    path = Path(config_dir) / filename
    if not path.is_file():
        raise IngestError(f"Keys file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def connect(config_dir: str | os.PathLike = "config") -> PyEmVue:
    """Log in to the Emporia cloud and return an authenticated pyemvue client.

    Credentials and the pyemvue token cache live in **separate files** on
    purpose. ``keys.json`` holds only the ``username``/``password`` and is never
    written by pyemvue; ``token_cache.json`` is the mutable
    ``token_storage_file`` pyemvue reads and rewrites on every login.

    Keeping them apart is what fixes the "can't find the correct token"
    failure: pyemvue's ``_store_tokens`` rewrites its token file with the
    id/access/refresh tokens but **drops the password**, so once the cached
    refresh token expires there would be no credential left to re-authenticate
    with. connect() therefore (1) tries the cached token first, then (2) falls
    back to the stored credentials, which pyemvue can always use to mint a
    fresh token.

    Args:
        config_dir: Directory containing ``keys.json`` and ``token_cache.json``.
            Defaults to ``"config"`` relative to the current working directory.

    Returns:
        An authenticated :class:`~pyemvue.PyEmVue` client.

    Raises:
        IngestError: If ``keys.json`` is missing, the credentials are
            incomplete, or pyemvue's credential login returns a falsy result.
    """
    token_cache = Path(config_dir) / "token_cache.json"
    vue = PyEmVue()
    if token_cache.is_file() and _login_with_cached_token(vue, token_cache):
        return vue
    return _login_with_credentials(vue, config_dir, token_cache)


def _login_with_cached_token(vue: PyEmVue, token_cache: Path) -> bool:
    """Attempt a best-effort login from the cached token file.

    Returns ``True`` only if pyemvue reports a successful login. A corrupt or
    unreadable cache is swallowed and reported as a failed attempt so the
    caller can fall back to the stored credentials; an expired token is already
    surfaced by pyemvue as a falsy return, not an exception.
    """
    try:
        return bool(vue.login(token_storage_file=str(token_cache)))
    except OSError, ValueError, KeyError:
        return False


def _login_with_credentials(
    vue: PyEmVue, config_dir: str | os.PathLike, token_cache: Path
) -> PyEmVue:
    """Log in with the stored username/password, caching the resulting token.

    Raises:
        IngestError: If credentials are missing/incomplete or the login fails.
    """
    keys = load_keys(config_dir)
    username = keys.get("username")
    password = keys.get("password")
    if not username or not password:
        raise IngestError(
            "Emporia credentials missing: set both 'username' and 'password' in "
            f"{Path(config_dir) / 'keys.json'}"
        )
    logged_in = vue.login(
        username=username, password=password, token_storage_file=str(token_cache)
    )
    if not logged_in:
        raise IngestError(f"Emporia login failed for user {username!r}")
    return vue


def channel_role(device: VueDevice, channel_num: str) -> str:
    """Classify a channel as an aux device, the mains aggregate, or a branch circuit.

    Args:
        device: The parent :class:`~pyemvue.device.VueDevice`.
        channel_num: The pyemvue channel number string (e.g. ``"1,2,3"``).

    Returns:
        ``"aux"`` if ``device`` is an EV charger or smart outlet; else
        ``"mains"`` if ``channel_num`` names the whole-home aggregate; else
        ``"branch"``.
    """
    if device.ev_charger or device.outlet:
        return "aux"
    if channel_num in {"1,2,3", "Mains"}:
        return "mains"
    return "branch"


def discover_channels(vue: PyEmVue) -> list[tuple[Channel, VueDeviceChannel]]:
    """Discover every monitored channel across all Emporia devices on the account.

    Args:
        vue: An authenticated pyemvue client (see :func:`connect`).

    Returns:
        ``(Channel, VueDeviceChannel)`` pairs: the parsed metadata alongside
        the underlying pyemvue channel object :func:`pull_usage` needs to
        fetch that channel's usage.
    """
    channels: list[tuple[Channel, VueDeviceChannel]] = []
    for device in vue.get_devices():
        device = vue.populate_device_properties(device)
        device_name = device.display_name or device.device_name
        for channel_obj in device.channels:
            role = channel_role(device, channel_obj.channel_num)
            channel = Channel(
                device_gid=device.device_gid,
                device_name=device_name,
                channel_num=channel_obj.channel_num,
                name=channel_obj.name,
                role=role,
            )
            channels.append((channel, channel_obj))
    return channels


def pull_usage(
    vue: PyEmVue,
    channels_with_objs: Iterable[tuple[Channel, VueDeviceChannel]],
    start: datetime,
    end: datetime,
    scale: str = "1H",
) -> list[IntervalUsage]:
    """Fetch and expand interval usage for a set of discovered channels.

    Args:
        vue: An authenticated pyemvue client (see :func:`connect`).
        channels_with_objs: Channel/pyemvue-object pairs from
            :func:`discover_channels`.
        start: Inclusive UTC start of the usage window.
        end: Exclusive UTC end of the usage window.
        scale: A pyemvue chart-usage scale, e.g. ``"1H"``. Must be a key of
            :data:`emporia_hydro.models.SCALE_HOURS`.

    Returns:
        One :class:`~emporia_hydro.models.IntervalUsage` per non-``None``
        value pyemvue returns, in chronological order per channel.

    Raises:
        IngestError: If ``scale`` is not a recognized pyemvue scale.
    """
    if scale not in SCALE_HOURS:
        raise IngestError(f"Unknown pyemvue usage scale: {scale!r}")
    interval = timedelta(hours=SCALE_HOURS[scale])

    usages: list[IntervalUsage] = []
    for channel, channel_obj in channels_with_objs:
        values, start_dt = vue.get_chart_usage(
            channel_obj, start, end, scale=scale, unit="KilowattHours"
        )
        if start_dt is None:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        for index, value in enumerate(values):
            if value is None:
                continue
            usages.append(
                IntervalUsage(
                    ts=start_dt + interval * index,
                    scale=scale,
                    device_gid=channel.device_gid,
                    channel=channel.channel_num,
                    kwh=value,
                )
            )
    return usages


def write_csv(usages: Iterable[IntervalUsage], path: str | os.PathLike) -> None:
    """Write interval usage records to a CSV cache file.

    Args:
        usages: The records to write, in the order given.
        path: Destination CSV file path (overwritten if it already exists).
    """
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(_CSV_FIELDS)
        for usage in usages:
            writer.writerow(
                [usage.ts.isoformat(), usage.scale, usage.device_gid, usage.channel, usage.kwh]
            )


def read_csv(path: str | os.PathLike) -> list[IntervalUsage]:
    """Read interval usage records back from a CSV cache file.

    Args:
        path: Source CSV file path, as written by :func:`write_csv`.

    Returns:
        The parsed records, in file order.

    Raises:
        IngestError: If ``path`` does not exist.
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise IngestError(f"CSV cache file not found: {csv_path}")
    usages: list[IntervalUsage] = []
    with open(csv_path, newline="", encoding="utf-8") as csv_file:
        for row in csv.DictReader(csv_file):
            usages.append(
                IntervalUsage(
                    ts=datetime.fromisoformat(row["ts_utc"]),
                    scale=row["scale"],
                    device_gid=int(row["device_gid"]),
                    channel=row["channel"],
                    kwh=float(row["kwh"]),
                )
            )
    return usages


def write_channels(channels: Sequence[Channel], path: str | os.PathLike) -> None:
    """Write discovered channel metadata to a JSON cache file.

    The usage CSV records only ``(device_gid, channel)``; channel *roles*
    (``mains``/``branch``/``aux``) come from :func:`discover_channels` and are
    what let offline pricing tell the whole-home Mains total from branch
    circuits. Persisting them here lets every offline command reconstruct the
    breakdown without touching the cloud.

    Args:
        channels: The channels to cache, in the order given.
        path: Destination JSON file path (overwritten if it already exists).
    """
    data = [asdict(channel) for channel in channels]
    with open(path, "w", encoding="utf-8") as channels_file:
        json.dump(data, channels_file, indent=2)


def read_channels(path: str | os.PathLike) -> list[Channel]:
    """Read channel metadata back from a JSON cache file.

    Args:
        path: Source JSON file path, as written by :func:`write_channels`.

    Returns:
        The parsed channels, in file order.

    Raises:
        IngestError: If ``path`` does not exist.
    """
    channels_path = Path(path)
    if not channels_path.is_file():
        raise IngestError(f"Channels cache file not found: {channels_path}")
    data = json.loads(channels_path.read_text(encoding="utf-8"))
    return [Channel(**item) for item in data]
