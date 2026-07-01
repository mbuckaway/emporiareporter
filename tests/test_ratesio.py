# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.ratesio - COMPLETE test suite written FIRST."""

import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from emporia_hydro.rates import load_config
from emporia_hydro.ratesio import (
    ActiveBucket,
    CheckResult,
    RatesIoError,
    RowDiff,
    ShowResult,
    UpdateDiff,
    rates_check,
    rates_import,
    rates_set,
    rates_show,
    rates_update,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rates_dir(tmp_path) -> Path:
    """Copy the real repo config/rates.json into an isolated tmp_path dir."""
    dest = tmp_path / "config"
    dest.mkdir()
    shutil.copy(REPO_ROOT / "config" / "rates.json", dest / "rates.json")
    return dest


@pytest.fixture
def rates_config(rates_dir):
    """Load the isolated tmp_path rates.json into a RatesConfig for rates_show."""
    return load_config(rates_dir)


@pytest.fixture
def rates_dict() -> dict:
    """Raw parsed JSON of the real config/rates.json, used as a fetcher base."""
    raw = (REPO_ROOT / "config" / "rates.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build a timezone-aware UTC datetime for a given wall time."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# Known fixed instants (verified against config/rates.json + tests/test_rates.py):
# local America/Toronto wall time -> UTC, and the expected TOU/ULO bucket+rate.
WINTER_WEEKDAY_ON_PEAK_UTC = _utc(2026, 1, 13, 13, 0)  # local 08:00, Tue Jan 13 (winter)
SUMMER_WEEKDAY_MID_UTC = _utc(2026, 6, 16, 12, 0)  # local 08:00, Tue Jun 16 (summer)
SATURDAY_OFF_PEAK_UTC = _utc(2026, 6, 20, 16, 0)  # local 12:00, Sat Jun 20
ULO_OVERNIGHT_UTC = _utc(2026, 6, 16, 10, 0)  # local 06:00, Tue Jun 16


# ---------------------------------------------------------------------------
# rates_show - known bucket/rate facts (offline)
# ---------------------------------------------------------------------------


def test_rates_show_winterweekdayonpeak_returnstoubucketon(rates_config):
    result = rates_show(rates_config, at=WINTER_WEEKDAY_ON_PEAK_UTC)

    assert result.active["tou"] == ActiveBucket("tou", "on", 0.203)


def test_rates_show_summerweekdaymid_returnstoubucketmid(rates_config):
    result = rates_show(rates_config, at=SUMMER_WEEKDAY_MID_UTC)

    assert result.active["tou"] == ActiveBucket("tou", "mid", 0.157)


def test_rates_show_saturdayoffpeak_returnstoubucketoff(rates_config):
    result = rates_show(rates_config, at=SATURDAY_OFF_PEAK_UTC)

    assert result.active["tou"] == ActiveBucket("tou", "off", 0.098)


def test_rates_show_ulovernighthour_returnsulobucketovernight(rates_config):
    result = rates_show(rates_config, at=ULO_OVERNIGHT_UTC)

    assert result.active["ulo"] == ActiveBucket("ulo", "overnight", 0.039)


def test_rates_show_winterweekdayonpeak_uloismidbucket(rates_config):
    result = rates_show(rates_config, at=WINTER_WEEKDAY_ON_PEAK_UTC)

    assert result.active["ulo"] == ActiveBucket("ulo", "mid", 0.157)


def test_rates_show_anygiveninstant_returnsexpectedrowsandtimezone(rates_config):
    result = rates_show(rates_config, at=WINTER_WEEKDAY_ON_PEAK_UTC)

    assert result.timezone == "America/Toronto"
    assert (result.tou_row["off"], result.tou_row["mid"], result.tou_row["on"]) == (
        0.098,
        0.157,
        0.203,
    )
    assert (
        result.ulo_row["overnight"],
        result.ulo_row["weekend_off"],
        result.ulo_row["mid"],
        result.ulo_row["on"],
    ) == (0.039, 0.098, 0.157, 0.391)
    assert (result.tiered_row["tier1"], result.tiered_row["tier2"]) == (0.120, 0.142)
    assert result.tiered_threshold_kwh == 1000  # WINTER_WEEKDAY_ON_PEAK_UTC local date is winter
    assert result.on == WINTER_WEEKDAY_ON_PEAK_UTC.astimezone(result.at.tzinfo).date()


def test_rates_show_atomitted_usesnowhelper(rates_config, monkeypatch):
    fixed_now = WINTER_WEEKDAY_ON_PEAK_UTC

    monkeypatch.setattr("emporia_hydro.ratesio._now", lambda config: fixed_now)

    result = rates_show(rates_config)

    assert result.at == fixed_now


def test_rates_show_returnsfrozenshowresult(rates_config):
    result = rates_show(rates_config, at=WINTER_WEEKDAY_ON_PEAK_UTC)

    assert isinstance(result, ShowResult)


# ---------------------------------------------------------------------------
# rates_set - append / replace / validation
# ---------------------------------------------------------------------------


def test_rates_set_appendnewrow_writesrowtofile(rates_dir):
    row = rates_set(
        rates_dir,
        plan="tou",
        effective="2026-11-01",
        rates={"off": 0.100, "mid": 0.160, "on": 0.210},
    )

    assert (row["effective"], row["off"], row["mid"], row["on"]) == (
        "2026-11-01",
        0.100,
        0.160,
        0.210,
    )
    data = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    effectives = [r["effective"] for r in data["plans"]["tou"]["prices"]]
    assert "2026-11-01" in effectives


def test_rates_set_replaceexistingeffective_overwritesrow(rates_dir):
    rates_set(
        rates_dir,
        plan="tou",
        effective="2025-11-01",
        rates={"off": 0.999, "mid": 0.888, "on": 0.777},
    )

    data = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    matching = [r for r in data["plans"]["tou"]["prices"] if r["effective"] == "2025-11-01"]
    assert len(matching) == 1
    assert (matching[0]["off"], matching[0]["mid"], matching[0]["on"]) == (0.999, 0.888, 0.777)


def test_rates_set_missingrequiredkey_raisesratesioerror(rates_dir):
    expected_match = "Missing required rate key 'on' for plan 'tou'"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_set(rates_dir, plan="tou", effective="2026-11-01", rates={"off": 0.1, "mid": 0.2})


def test_rates_set_badplan_raisesratesioerror(rates_dir):
    expected_match = "Unknown plan: 'bogus'; must be one of 'tou', 'ulo', 'tiered'"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_set(rates_dir, plan="bogus", effective="2026-11-01", rates={"off": 0.1})


@pytest.mark.parametrize(
    ("plan", "rates"),
    [
        ("tou", {"off": 0.1, "mid": 0.2, "on": 0.3}),
        ("ulo", {"overnight": 0.04, "weekend_off": 0.1, "mid": 0.16, "on": 0.4}),
        ("tiered", {"tier1": 0.13, "tier2": 0.15}),
    ],
    ids=["tou", "ulo", "tiered"],
)
def test_rates_set_validplanrequiredkeys_writessuccessfully(rates_dir, plan, rates):
    row = rates_set(rates_dir, plan=plan, effective="2027-11-01", rates=rates)

    assert row["effective"] == "2027-11-01"
    for key, value in rates.items():
        assert row[key] == value


def test_rates_set_otherfilekeys_survivewrite(rates_dir):
    before = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))

    rates_set(
        rates_dir, plan="tou", effective="2026-11-01", rates={"off": 0.1, "mid": 0.2, "on": 0.3}
    )

    after = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    assert after["_comment"] == before["_comment"]
    assert after["seasons"] == before["seasons"]
    assert after["schedule"] == before["schedule"]
    assert after["holidays"] == before["holidays"]
    assert after["plans"]["tou"]["prices"] != before["plans"]["tou"]["prices"]
    assert after["plans"]["ulo"] == before["plans"]["ulo"]
    assert after["plans"]["tiered"] == before["plans"]["tiered"]


def test_rates_set_pricessortedbyeffectivedescending(rates_dir):
    rates_set(
        rates_dir, plan="tou", effective="2026-11-01", rates={"off": 0.1, "mid": 0.2, "on": 0.3}
    )

    data = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    effectives = [r["effective"] for r in data["plans"]["tou"]["prices"]]
    assert effectives == sorted(effectives, reverse=True)


def test_rates_set_expiryandconfidence_writtenintorow(rates_dir):
    row = rates_set(
        rates_dir,
        plan="tou",
        effective="2026-11-01",
        rates={"off": 0.1, "mid": 0.2, "on": 0.3},
        expiry="2027-10-31",
        confidence="low",
    )

    assert (row["expiry"], row["confidence"]) == ("2027-10-31", "low")


def test_rates_set_expiryomitted_defaultsnone(rates_dir):
    row = rates_set(
        rates_dir, plan="tou", effective="2026-11-01", rates={"off": 0.1, "mid": 0.2, "on": 0.3}
    )

    assert row["expiry"] is None


def test_rates_set_confidenceomitted_defaultshigh(rates_dir):
    row = rates_set(
        rates_dir, plan="tou", effective="2026-11-01", rates={"off": 0.1, "mid": 0.2, "on": 0.3}
    )

    assert row["confidence"] == "high"


# ---------------------------------------------------------------------------
# rates_import - valid / missing / invalid
# ---------------------------------------------------------------------------


def test_rates_import_validfile_replacesconfigandreturnssummary(tmp_path, rates_dir):
    src = tmp_path / "incoming.json"
    shutil.copy(REPO_ROOT / "config" / "rates.json", src)

    result = rates_import(src, rates_dir)

    assert result == {"plans": ["tou", "ulo", "tiered"], "rows": 10}
    imported = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    original = json.loads(src.read_text(encoding="utf-8"))
    assert imported == original


def test_rates_import_missingfile_raisesratesioerror(tmp_path, rates_dir):
    missing = tmp_path / "does_not_exist.json"
    expected_match = f"Rates import file not found: {missing}"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_import(missing, rates_dir)


def test_rates_import_invalidjson_raisesratesioerror(tmp_path, rates_dir):
    src = tmp_path / "bad.json"
    src.write_text("{not valid json", encoding="utf-8")
    expected_match = "Invalid rates file"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_import(src, rates_dir)


def test_rates_import_missingrequiredkey_raisesratesioerror(tmp_path, rates_dir, rates_dict):
    del rates_dict["timezone"]
    src = tmp_path / "incomplete.json"
    src.write_text(json.dumps(rates_dict), encoding="utf-8")
    expected_match = "Invalid rates file"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_import(src, rates_dir)


def test_rates_import_failure_leavesdestinationfileuntouched(tmp_path, rates_dir):
    before = (rates_dir / "rates.json").read_bytes()
    missing = tmp_path / "does_not_exist.json"

    with pytest.raises(RatesIoError, match=re.escape("not found")):
        rates_import(missing, rates_dir)

    assert (rates_dir / "rates.json").read_bytes() == before


# ---------------------------------------------------------------------------
# rates_update - stored-file read failures (missing / invalid JSON)
# ---------------------------------------------------------------------------


def test_rates_update_storedfilemissing_raisesratesioerror(tmp_path, rates_dict):
    missing_dir = tmp_path / "no_config_here"
    missing_dir.mkdir()
    expected_match = f"Rates file not found: {missing_dir / 'rates.json'}"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_update(missing_dir, fetcher=lambda: rates_dict, apply=False)


def test_rates_update_storedfileinvalidjson_raisesratesioerror(tmp_path, rates_dict):
    bad_dir = tmp_path / "bad_config"
    bad_dir.mkdir()
    (bad_dir / "rates.json").write_text("{not valid json", encoding="utf-8")
    expected_match = "Invalid JSON in rates file"

    with pytest.raises(RatesIoError, match=re.escape(expected_match)):
        rates_update(bad_dir, fetcher=lambda: rates_dict, apply=False)


# ---------------------------------------------------------------------------
# rates_update - diff, apply=False (no write), apply=True (write merged)
# ---------------------------------------------------------------------------


def _fetched_with_added_and_changed(rates_dict: dict) -> dict:
    """Return a mutated copy of rates_dict with one added row and one changed field."""
    fetched = json.loads(json.dumps(rates_dict))  # deep copy via round-trip
    fetched["plans"]["tou"]["prices"].insert(
        0,
        {
            "effective": "2026-11-01",
            "expiry": "2027-10-31",
            "off": 0.101,
            "mid": 0.161,
            "on": 0.211,
            "confidence": "low",
        },
    )
    fetched["plans"]["ulo"]["prices"][0] = {
        **fetched["plans"]["ulo"]["prices"][0],
        "on": 0.999,
    }
    return fetched


def test_rates_update_fetcherwithchanges_diffreportsaddedandchanged(rates_dir, rates_dict):
    fetched = _fetched_with_added_and_changed(rates_dict)

    diff = rates_update(rates_dir, fetcher=lambda: fetched, apply=False)

    assert len(diff.added) == 1
    assert diff.added[0]["effective"] == "2026-11-01"
    assert RowDiff("ulo", "2025-11-01", "on", 0.391, 0.999) in diff.changed
    assert diff.applied is False


def test_rates_update_fetcheddropsfield_reportedaschanged(rates_dir, rates_dict):
    # A fetched row that OMITS a field the stored row had must surface as a
    # change (old value -> None), not be silently counted 'unchanged'.
    fetched = json.loads(json.dumps(rates_dict))
    del fetched["plans"]["tou"]["prices"][0]["confidence"]

    diff = rates_update(rates_dir, fetcher=lambda: fetched, apply=False)

    assert RowDiff("tou", "2025-11-01", "confidence", "high", None) in diff.changed


def test_rates_update_applytrue_newplan_upsertswithoutkeyerror(rates_dir, rates_dict):
    # A plan present only in the fetched file must be upserted, not KeyError.
    fetched = json.loads(json.dumps(rates_dict))
    fetched["plans"]["flat"] = {"prices": [{"effective": "2026-11-01", "flat": 0.11}]}

    diff = rates_update(rates_dir, fetcher=lambda: fetched, apply=True)

    assert any(row["plan"] == "flat" for row in diff.added)
    written = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    assert written["plans"]["flat"]["prices"][0]["effective"] == "2026-11-01"


def test_rates_update_applyfalse_writesnothing(rates_dir, rates_dict):
    before = (rates_dir / "rates.json").read_bytes()
    fetched = _fetched_with_added_and_changed(rates_dict)

    rates_update(rates_dir, fetcher=lambda: fetched, apply=False)

    assert (rates_dir / "rates.json").read_bytes() == before


def test_rates_update_applytrue_writesmergedfile(rates_dir, rates_dict):
    fetched = _fetched_with_added_and_changed(rates_dict)

    diff = rates_update(rates_dir, fetcher=lambda: fetched, apply=True)

    assert diff.applied is True
    written = json.loads((rates_dir / "rates.json").read_text(encoding="utf-8"))
    tou_effectives = [r["effective"] for r in written["plans"]["tou"]["prices"]]
    assert "2026-11-01" in tou_effectives
    ulo_current = next(
        r for r in written["plans"]["ulo"]["prices"] if r["effective"] == "2025-11-01"
    )
    assert ulo_current["on"] == 0.999


def test_rates_update_noactualchanges_diffreportsunchangedcount(rates_dir, rates_dict):
    fetched = json.loads(json.dumps(rates_dict))
    total_rows = sum(len(plan["prices"]) for plan in fetched["plans"].values())

    diff = rates_update(rates_dir, fetcher=lambda: fetched, apply=False)

    assert (len(diff.added), len(diff.changed), diff.unchanged) == (0, 0, total_rows)


def test_rates_update_fetcherinjected_calledexactlyonce(rates_dir, rates_dict):
    calls: list[int] = []

    def fetcher() -> dict:
        calls.append(1)
        return rates_dict

    diff = rates_update(rates_dir, fetcher=fetcher, apply=False)

    assert len(calls) == 1
    assert diff.unchanged > 0


# ---------------------------------------------------------------------------
# rates_check - stale True/False, never mutates
# ---------------------------------------------------------------------------


def test_rates_check_fetcherwithchanges_returnsstaletrue(rates_dir, rates_dict):
    fetched = _fetched_with_added_and_changed(rates_dict)

    result = rates_check(rates_dir, fetcher=lambda: fetched)

    assert result.stale is True
    assert len(result.diff.added) == 1


def test_rates_check_fetchermatchesstored_returnsstalefalse(rates_dir, rates_dict):
    fetched = json.loads(json.dumps(rates_dict))

    result = rates_check(rates_dir, fetcher=lambda: fetched)

    assert result.stale is False


def test_rates_check_nevermutatesstoredfile(rates_dir, rates_dict):
    before = (rates_dir / "rates.json").read_bytes()
    fetched = _fetched_with_added_and_changed(rates_dict)

    rates_check(rates_dir, fetcher=lambda: fetched)

    assert (rates_dir / "rates.json").read_bytes() == before


def test_rates_check_returnsfrozencheckresult(rates_dir, rates_dict):
    fetched = json.loads(json.dumps(rates_dict))

    result = rates_check(rates_dir, fetcher=lambda: fetched)

    assert isinstance(result, CheckResult)


def test_rates_check_diffisupdatediffinstance(rates_dir, rates_dict):
    fetched = json.loads(json.dumps(rates_dict))

    result = rates_check(rates_dir, fetcher=lambda: fetched)

    assert isinstance(result.diff, UpdateDiff)


# ---------------------------------------------------------------------------
# Hypothesis property tests - rates_set round-trip invariants (T-7)
# ---------------------------------------------------------------------------


def _isolated_rates_dir(tmp_dir_name: str) -> Path:
    """Copy the real repo config/rates.json into a fresh isolated directory."""
    dest = Path(tmp_dir_name) / "config"
    dest.mkdir()
    shutil.copy(REPO_ROOT / "config" / "rates.json", dest / "rates.json")
    return dest


@given(
    off=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    mid=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    on=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
)
def test_rates_set_anyvalidrates_writingtwiceisidempotent(off, mid, on):
    rates = {"off": off, "mid": mid, "on": on}

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        dest = _isolated_rates_dir(tmp_dir_name)
        rates_set(dest, plan="tou", effective="2030-11-01", rates=rates)
        after_first = (dest / "rates.json").read_text(encoding="utf-8")
        rates_set(dest, plan="tou", effective="2030-11-01", rates=rates)
        after_second = (dest / "rates.json").read_text(encoding="utf-8")

    assert after_first == after_second


@given(
    tier1=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    tier2=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
)
def test_rates_set_anyvalidtieredrates_otherplansrowcountunchanged(tier1, tier2):
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        dest = _isolated_rates_dir(tmp_dir_name)
        before = json.loads((dest / "rates.json").read_text(encoding="utf-8"))
        before_tou_count = len(before["plans"]["tou"]["prices"])
        before_ulo_count = len(before["plans"]["ulo"]["prices"])

        rates_set(
            dest, plan="tiered", effective="2030-11-01", rates={"tier1": tier1, "tier2": tier2}
        )

        after = json.loads((dest / "rates.json").read_text(encoding="utf-8"))

    assert len(after["plans"]["tou"]["prices"]) == before_tou_count
    assert len(after["plans"]["ulo"]["prices"]) == before_ulo_count
