"""
Build the test fixtures from the local DATA/ folder (run once; the output is committed).

    python tests/build_fixtures.py

* TLEs: the five DATA/TLE/*.tle files (epochs 24 Oct 2026) combined into one file.
* ESA plans: the three Sentinel-2 KMLs, trimmed to the data-takes whose footprints
  come within ~1000 km of the test ROI. The document name, which holds the planning
  window, is kept, so "no plan loaded" vs "not planned" still behaves as with the
  full files. Trimming takes them from ~5.4 MB to a few hundred KB.
* ROI: the Port Phillip Bay shapefile from DATA/SHP.
"""

import re
import shutil
from pathlib import Path

from shapely.geometry import Polygon, box

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "DATA"
OUT = Path(__file__).resolve().parent / "fixtures"

# Port Phillip Bay is ~144.4–145.2 E, 38.4–37.8 S; keep footprints touching this wider box.
KEEP_BOX = box(133.0, -47.0, 157.0, -29.0)
PLACEMARK = re.compile(r"[ \t]*<Placemark>.*?</Placemark>\s*\n?", re.S)
COORDS = re.compile(r"<coordinates>(.*?)</coordinates>", re.S)


def _touches_box(block: str) -> bool:
    for raw in COORDS.findall(block):
        pts = [tuple(map(float, c.split(",")[:2])) for c in raw.split()]
        if len(pts) >= 3 and Polygon(pts).intersects(KEEP_BOX):
            return True
    return False


def trim_kml(src: Path, dst: Path) -> tuple:
    text = src.read_text(encoding="iso-8859-1")
    blocks = PLACEMARK.findall(text)
    kept = sum(_touches_box(b) for b in blocks)
    trimmed = PLACEMARK.sub(lambda m: m.group(0) if _touches_box(m.group(0)) else "", text)
    dst.write_text(trimmed, encoding="iso-8859-1")
    return len(blocks), kept


def main():
    (OUT / "tle").mkdir(parents=True, exist_ok=True)
    (OUT / "kml").mkdir(parents=True, exist_ok=True)
    (OUT / "roi").mkdir(parents=True, exist_ok=True)

    tle_files = sorted((DATA / "TLE").glob("*.tle"))
    (OUT / "tle" / "tles_20261024.tle").write_text(
        "".join(f.read_text().strip() + "\n" for f in tle_files))
    print(f"TLE: {len(tle_files)} satellites -> fixtures/tle/tles_20261024.tle")

    for src in sorted((DATA / "KML").glob("*.kml")):
        total, kept = trim_kml(src, OUT / "kml" / src.name)
        print(f"KML: {src.name}: kept {kept} of {total} data-takes")

    for src in (DATA / "SHP").glob("Port_Phillip_Bay_Extent_EPSG_32755.*"):
        shutil.copy2(src, OUT / "roi" / src.name)
    print("ROI: Port_Phillip_Bay_Extent_EPSG_32755.* copied")


if __name__ == "__main__":
    main()
