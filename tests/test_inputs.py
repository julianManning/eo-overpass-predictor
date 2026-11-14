"""
Inputs: TLE parsing, satellite names, ROI loading and acquisition-plan parsing.
"""

import pandas as pd
import pytest

import _baseline as b

FIXTURE_NORAD_IDS = {39084, 49260, 40697, 42063, 60989}


def test_tle_fixture_parses_with_valid_checksums():
    recs = b.ovp.parse_tle_text((b.FIXTURES / "tle" / "tles_20261024.tle").read_text())
    assert set(recs) == FIXTURE_NORAD_IDS
    for rec in recs.values():
        for line in (rec["line1"], rec["line2"]):
            assert len(line) == 69
            assert b.ovp._tle_checksum(line) == int(line[68])


def test_fixture_tles_load_offline(tles):
    """The autouse no_network fixture would raise if fetch_tles tried to download."""
    assert set(tles) == set(b.ovp.SATELLITE_CATALOGUE)
    for rec in tles.values():
        assert rec["source"] == "manual"
        assert rec["epoch_utc"].strftime("%Y-%m-%d") == "2026-10-24"


@pytest.mark.parametrize("alias, name", [
    ("L8", "LANDSAT 8"), ("l9", "LANDSAT 9"), ("S2A", "SENTINEL-2A"),
    ("s2b", "SENTINEL-2B"), (60989, "SENTINEL-2C"),
])
def test_satellite_aliases(alias, name):
    assert b.ovp._resolve_satellites([alias]) == [name]


def test_unknown_satellite_rejected():
    with pytest.raises(ValueError):
        b.ovp._resolve_satellites(["LANDSAT 7"])


@pytest.mark.parametrize("case", list(b.ROI_CASES))
def test_roi_loading(case):
    roi = b.ovp.load_roi(**b.ROI_CASES[case])
    assert len(roi) == 1
    assert roi.crs.to_epsg() == 4326
    assert roi.geometry.iloc[0].is_valid
    assert roi["name"].iloc[0] == b.ROI_CASES[case]["name"]
    assert b.ovp.resolve_roi_timezone(roi, verbose=False) == "Australia/Melbourne"


def test_plan_windows_and_counts(plans):
    assert plans["satellite"].value_counts().to_dict() == {
        "SENTINEL-2B": 20, "SENTINEL-2C": 20, "SENTINEL-2A": 8}
    spans = plans.groupby("satellite")[["plan_start_utc", "plan_end_utc"]].first()
    assert spans.loc["SENTINEL-2B", "plan_end_utc"] == pd.Timestamp("2026-10-26 15:00", tz="UTC")
    assert spans.loc["SENTINEL-2A", "plan_start_utc"] == pd.Timestamp("2026-10-15 15:00", tz="UTC")
    assert set(plans["plan_category"]) == {"NOMINAL"}
