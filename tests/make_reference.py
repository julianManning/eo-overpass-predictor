"""
Generate the regression reference outputs from the CURRENT code (output is committed).

    python tests/make_reference.py

Run this once to create the baseline. Re-run it only when a change to the results
is intended and reviewed (e.g. a deliberate bug fix), and say so in the PR, because
it redefines what "correct" means for every test in test_regression.py.

Writes to tests/reference/:
    overpasses_<case>.csv          CSV exactly as export_overpass_csv writes it
    footprints_<case>.geojson      per-pass swath strips (export_overpass_footprints)
    provenance.json                settings, versions and a hash of the helper used
"""

import hashlib
import json
import platform
import shutil
import tempfile
from importlib.metadata import version
from pathlib import Path

import _baseline as b


def main():
    b.REFERENCE.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tles = b.load_fixture_tles(Path(tmp) / "tle")
        plans = b.load_fixture_plans()
        summary = {}
        for case in b.ROI_CASES:
            _, passes = b.run_case(case, tles, plans)
            b.ovp.export_overpass_csv(passes, b.REFERENCE, file_name=f"overpasses_{case}.csv")
            gj = b.REFERENCE / f"footprints_{case}.geojson"
            gj.unlink(missing_ok=True)
            b.ovp.export_overpass_footprints(passes, gj)
            summary[case] = int(len(passes))

    helper = b.REPO_DIR / "Tools" / "overpass_helper_utility.py"
    provenance = {
        "generated_from": "Tools/overpass_helper_utility.py",
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "start_utc": b.START_UTC, "days": b.DAYS, "timezone": b.TIMEZONE,
        "predict_kwargs": b.PREDICT_KWARGS,
        "roi_cases": {k: str(v["source"]) if isinstance(v["source"], tuple) else
                      Path(v["source"]).relative_to(b.REPO_DIR).as_posix()
                      for k, v in b.ROI_CASES.items()},
        "passes_per_case": summary,
        "python": platform.python_version(),
        "packages": {p: version(p) for p in
                     ["skyfield", "numpy", "pandas", "geopandas", "shapely", "pyproj", "timezonefinder"]},
    }
    (b.REFERENCE / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
