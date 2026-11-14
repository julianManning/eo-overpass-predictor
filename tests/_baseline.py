"""
Frozen settings and the shared pipeline for the regression baseline.

Both ``make_reference.py`` (which writes the reference outputs) and the tests
import from here, so the reference and the tests always run the exact same steps.
Change nothing in this file unless you intend to regenerate the reference.
"""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
REFERENCE = TESTS_DIR / "reference"

# The package does not exist yet, so import the helper from Tools/ as the notebooks do.
sys.path.insert(0, str(REPO_DIR / "Tools"))
import overpass_helper_utility as ovp  # noqa: E402

# ---------------- Frozen baseline settings ----------------
START_UTC = "2026-10-24 00:00"          # TLE epochs are all on 24 Oct 2026
DAYS = 16                               # one Landsat repeat cycle (notebook default)
TIMEZONE = "Australia/Melbourne"
PREDICT_KWARGS = dict(                  # p01 notebook defaults
    daytime_descending_only=True,
    min_sun_elevation_deg=0.0,
    min_roi_coverage_pct=0.0,
    coarse_step_s=30.0,
    fine_step_s=1.0,
)

# Three ways of giving the same area to load_roi (shapefile, point, bounding box).
ROI_CASES = {
    "polygon": dict(source=FIXTURES / "roi" / "Port_Phillip_Bay_Extent_EPSG_32755.shp",
                    name="Port_Phillip_Bay"),
    "point":   dict(source=(144.96, -37.82), name="Melbourne_CBD"),
    "bbox":    dict(source=(144.35, -38.35, 145.15, -37.80), name="Port_Phillip_Bay_bbox"),
}


def load_fixture_tles(tle_cache_dir):
    """Load the frozen TLEs without any network access.

    Passing ``tle_text`` makes ``fetch_tles`` use these elements directly
    (source = "manual") and skip CelesTrak entirely.
    """
    text = (FIXTURES / "tle" / "tles_20261024.tle").read_text()
    return ovp.fetch_tles(tle_text=text, tle_dir=tle_cache_dir, verbose=False)


def load_fixture_plans():
    """Load the trimmed ESA Sentinel-2 acquisition-plan KMLs."""
    return ovp.load_s2_acquisition_plans(FIXTURES / "kml")


def run_case(case, tles, plans):
    """Run predict → ESA cross-check for one ROI case and return (roi, passes)."""
    roi = ovp.load_roi(**ROI_CASES[case])
    passes = ovp.predict_overpasses(
        roi, tles, start_utc=START_UTC, days=DAYS, timezone_name=TIMEZONE,
        verbose=False, **PREDICT_KWARGS,
    )
    passes = ovp.crosscheck_s2_acquisition_plan(passes, plans, roi)
    return roi, passes
