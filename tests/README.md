# Tests

Regression and unit tests for `Tools/overpass_helper_utility.py`. They run fully
offline from frozen inputs in `tests/fixtures/`, so the results never depend on
today's TLEs or ESA's current plans.

## Run them

From the repository root, in the `overpass` environment:

```bash
pip install pytest          # once
python -m pytest tests
```

The suite takes about a minute (each ROI case is predicted once and reused).

## What is tested

| File | Checks |
|---|---|
| `test_regression.py` | The current code reproduces `tests/reference/` for three ROI inputs (shapefile, point, bounding box): same passes, CSV columns and order, text fields exactly, times within 2 s, numbers and footprints within small tolerances, and all four ESA cross-check outcomes. |
| `test_time_handling.py` | UTC/local conversion across the Melbourne DST change (4 Oct 2026), a time zone whose local date differs from the UTC date, and the CSV time formats. |
| `test_inputs.py` | TLE parsing and checksums, offline TLE loading, satellite name aliases, ROI loading and time-zone resolution, and acquisition-plan parsing. |

Regression tests check that results stay **the same** as the baseline, not that the
baseline is physically correct. Tolerances absorb harmless floating-point differences
between operating systems and library versions.

## Baseline

| Setting | Value |
|---|---|
| TLEs | Landsat 8/9, Sentinel-2A/B/C, epochs 24 Oct 2026 (`fixtures/tle/`) |
| ESA plans | S2A/S2C 15 Oct – 2 Nov, S2B 8 – 26 Oct 2026, trimmed to data-takes near the ROI (`fixtures/kml/`) |
| ROI | Port Phillip Bay (`fixtures/roi/`), plus point and bounding-box versions |
| Window | 2026-10-24 00:00 UTC + 16 days, Australia/Melbourne, p01 notebook defaults |

Settings live in `_baseline.py`, which both the tests and `make_reference.py` use.

## Regenerating the reference

Only when a change in results is **intended** (for example a deliberate bug fix):

```bash
python tests/make_reference.py
```

Then review the diff of `tests/reference/` and explain it in the pull request.
`build_fixtures.py` rebuilds `fixtures/` from a local `DATA/` folder and is only
needed if the frozen inputs themselves change.
