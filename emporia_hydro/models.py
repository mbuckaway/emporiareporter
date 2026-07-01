# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Shared data models: usage intervals and device channels.

This is a stable contract imported by ``ingest`` (produces the records) and by
``cost``/``compare``/``trends`` (consume them). Keep it small and stable.
"""

from dataclasses import dataclass
from datetime import datetime

__all__ = ["SCALE_HOURS", "BALANCE_CHANNEL", "Channel", "IntervalUsage"]

# Hours covered by one interval at each pyemvue Scale value. Used to derive
# average watts (kwh / hours * 1000) and to advance interval timestamps.
SCALE_HOURS: dict[str, float] = {
    "1S": 1.0 / 3600.0,
    "1MIN": 1.0 / 60.0,
    "15MIN": 0.25,
    "1H": 1.0,
    "1D": 24.0,
    "1W": 168.0,
}

# Synthetic channel label for whole-home usage not attributed to a monitored
# branch circuit: balance = mains - sum(branch circuits).
BALANCE_CHANNEL = "Balance/Other"


@dataclass(frozen=True)
class Channel:
    """Metadata for one measured channel/circuit or auxiliary device.

    ``role`` is one of ``"mains"`` (whole-home aggregate), ``"branch"`` (a CT
    circuit that is a subset of mains), or ``"aux"`` (a separate device such as
    an EV charger or smart plug, measured independently of the mains CTs).
    """

    device_gid: int
    device_name: str
    channel_num: str
    name: str
    role: str


@dataclass(frozen=True)
class IntervalUsage:
    """Energy consumed on one channel over one interval.

    ``ts`` is the timezone-aware UTC start of the interval; ``scale`` is the
    pyemvue Scale value (e.g. ``"1H"``) describing its duration.
    """

    ts: datetime
    scale: str
    device_gid: int
    channel: str
    kwh: float
