"""
Regression tests: the current code must reproduce tests/reference/.

These check that results stay the SAME as the baseline, not that the baseline is
physically right. Text fields must match exactly; computed numbers must match
within small tolerances, because tiny floating-point differences between operating
systems and library versions are expected and harmless.
"""

import geopandas as gpd
import pandas as pd
import pytest

import _baseline as b
from conftest import read_csv_as_text

CASES = list(b.ROI_CASES)

# Must match character for character.
EXACT_COLUMNS = [
    "satellite", "mission", "sensor", "norad_id", "direction", "imaging_expected",
    "roi_side_of_track", "local_date", "local_weekday", "timezone", "utc_offset",
    "tle_epoch_utc", "note", "esa_plan_status", "esa_datatake_id", "esa_relative_orbit",
]
# Compared as instants, within this many seconds (the search step is 1 s).
TIME_COLUMNS = ["roi_entry_utc", "overpass_utc", "roi_exit_utc",
                "roi_entry_local", "overpass_local", "roi_exit_local"]
TIME_TOLERANCE_S = 2.0
# Compared as numbers, within these absolute tolerances.
NUMERIC_TOLERANCES = {
    "roi_coverage_pct": 0.5, "duration_s": 2.0, "nadir_offset_km": 0.5,
    "edge_margin_km": 0.5, "view_angle_deg": 0.05, "max_elevation_deg": 0.2,
    "sun_elevation_deg": 0.2, "sat_altitude_km": 0.5, "tle_age_days": 0.1,
}
FOOTPRINT_TOLERANCE_DEG = 0.002          # ~200 m


@pytest.fixture(scope="module")
def outputs(results, tmp_path_factory):
    """Export each case with the real CSV writer and read it back alongside the reference."""
    out_dir = tmp_path_factory.mktemp("csv")
    data = {}
    for case in CASES:
        _, passes = results(case)
        path = b.ovp.export_overpass_csv(passes, out_dir, file_name=f"overpasses_{case}.csv")
        data[case] = (read_csv_as_text(path),
                      read_csv_as_text(b.REFERENCE / f"overpasses_{case}.csv"),
                      passes)
    return data


@pytest.mark.parametrize("case", CASES)
def test_columns_and_order_unchanged(outputs, case):
    new, ref, _ = outputs[case]
    assert list(new.columns) == list(ref.columns)


@pytest.mark.parametrize("case", CASES)
def test_same_number_of_passes(outputs, case):
    new, ref, _ = outputs[case]
    assert len(new) == len(ref), (
        f"{case}: {len(new)} passes, reference has {len(ref)}.\n"
        f"new: {new[['satellite', 'overpass_utc']].values.tolist()}\n"
        f"ref: {ref[['satellite', 'overpass_utc']].values.tolist()}")


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("column", EXACT_COLUMNS)
def test_text_fields_identical(outputs, case, column):
    new, ref, _ = outputs[case]
    pd.testing.assert_series_equal(new[column], ref[column], check_names=False)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("column", TIME_COLUMNS)
def test_times_within_tolerance(outputs, case, column):
    new, ref, _ = outputs[case]
    t_new = pd.to_datetime(new[column], utc=True, format="ISO8601")
    t_ref = pd.to_datetime(ref[column], utc=True, format="ISO8601")
    diff_s = (t_new - t_ref).abs().dt.total_seconds()
    assert diff_s.max() <= TIME_TOLERANCE_S, f"max difference {diff_s.max()} s"
    # Local columns must also keep the same written UTC offset (e.g. +11:00).
    if column.endswith("_local"):
        assert (new[column].str[-6:] == ref[column].str[-6:]).all()


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("column", list(NUMERIC_TOLERANCES))
def test_numbers_within_tolerance(outputs, case, column):
    new, ref, _ = outputs[case]
    diff = (new[column].astype(float) - ref[column].astype(float)).abs()
    assert diff.max() <= NUMERIC_TOLERANCES[column], f"max difference {diff.max()}"


@pytest.mark.parametrize("case", CASES)
def test_footprints_match(outputs, case):
    _, _, passes = outputs[case]
    ref = gpd.read_file(b.REFERENCE / f"footprints_{case}.geojson")
    assert len(passes) == len(ref)
    for i, (g_new, g_ref) in enumerate(zip(passes.geometry, ref.geometry)):
        d = g_new.hausdorff_distance(g_ref)
        assert d <= FOOTPRINT_TOLERANCE_DEG, f"pass {i}: footprint moved by {d:.4f} deg"


def test_baseline_exercises_every_esa_outcome(outputs):
    """Guards the fixture itself: the baseline must keep covering all cross-check branches."""
    _, ref, _ = outputs["polygon"]
    assert set(ref["esa_plan_status"]) == {
        "planned (NOMINAL)", "not planned", "no plan loaded", "n/a"}
