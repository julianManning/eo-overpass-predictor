"""
UTC / local-time handling.

The baseline window (24 Oct – 9 Nov 2026) contains no daylight-saving change and
every pass falls on the same calendar day in UTC and Melbourne time, so these
tests cover the cases it cannot: a DST transition inside the window, and a time
zone where the local date differs from the UTC date.
"""

from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import _baseline as b
from conftest import read_csv_as_text

MELBOURNE_POINT = (144.96, -37.82)
# Melbourne DST began on Sun 4 Oct 2026 at 02:00 AEST = Sat 3 Oct 16:00 UTC.
DST_START_UTC = pd.Timestamp("2026-10-03 16:00", tz="UTC")


def test_tzdata_available():
    """Windows has no system time-zone database; the tzdata package must be installed."""
    assert ZoneInfo("Australia/Melbourne").utcoffset(pd.Timestamp("2026-01-01")) == pd.Timedelta(hours=11)


@pytest.fixture(scope="module")
def dst_passes(tles):
    roi = b.ovp.load_roi(MELBOURNE_POINT)
    return b.ovp.predict_overpasses(roi, tles, start_utc="2026-10-01 00:00", days=5,
                                    timezone_name="Australia/Melbourne", verbose=False)


def test_window_spans_the_dst_change(dst_passes):
    """Guards the test itself: there must be passes on both sides of the change."""
    assert (dst_passes["overpass_utc"] < DST_START_UTC).any()
    assert (dst_passes["overpass_utc"] > DST_START_UTC).any()


def test_offset_switches_at_dst(dst_passes):
    before = dst_passes[dst_passes["overpass_utc"] < DST_START_UTC]
    after = dst_passes[dst_passes["overpass_utc"] > DST_START_UTC]
    assert (before["utc_offset"] == "+10:00").all()
    assert (after["utc_offset"] == "+11:00").all()


def test_local_time_is_same_instant_as_utc(dst_passes):
    for col in ["roi_entry", "overpass", "roi_exit"]:
        assert (dst_passes[f"{col}_local"] == dst_passes[f"{col}_utc"]).all()


@pytest.fixture(scope="module")
def la_csv(tles, tmp_path_factory):
    """Melbourne passes (~00:00–00:30 UTC) reported in Los Angeles time fall on the previous day."""
    roi = b.ovp.load_roi(MELBOURNE_POINT)
    passes = b.ovp.predict_overpasses(roi, tles, start_utc="2026-10-25 00:00", days=3,
                                      timezone_name="America/Los_Angeles", verbose=False)
    assert len(passes), "expected at least one pass in the window"
    path = b.ovp.export_overpass_csv(passes, tmp_path_factory.mktemp("la"), file_name="la.csv")
    return read_csv_as_text(path)


def test_local_date_rolls_back_a_day(la_csv):
    utc_date = la_csv["overpass_utc"].str[:10]
    assert (la_csv["local_date"] == la_csv["overpass_local"].str[:10]).all()
    assert (la_csv["local_date"] != utc_date).all()
    assert (la_csv["utc_offset"] == "-07:00").all()


def test_csv_time_formats(la_csv):
    for col in ["roi_entry_utc", "overpass_utc", "roi_exit_utc", "tle_epoch_utc"]:
        assert la_csv[col].str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$").all(), col
    for col in ["roi_entry_local", "overpass_local", "roi_exit_local"]:
        assert la_csv[col].str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$").all(), col
    assert (la_csv["local_time"] == la_csv["overpass_local"].str[11:19]).all()
