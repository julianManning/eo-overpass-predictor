"""
Landsat / Sentinel-2 Overpass Prediction Workflow

A transparent, notebook-driven engine for predicting when Landsat 8/9 and
Sentinel-2A/B/C will image a Region of Interest (ROI), tracking them live, and
reporting each overpass in UTC and in the ROI's own local time.

Key Features:
- CelesTrak GP (TLE) retrieval with on-disk caching, age-based refresh and offline fallback.
- SGP4 orbit propagation (Skyfield) with a coarse-to-fine search (30 s scan → 1 s refinement).
- Swath-aware imaging test: each instantaneous cross-track scan line (MSI 290 km, OLI 185 km)
  is intersected with the ROI in an ROI-centred equal-area projection — not a naive
  "above the horizon" test.
- Per-pass ROI coverage %, entry/exit times, nadir offset, view angle, sun elevation,
  pass direction and an "imaging expected" flag (daytime descending, latitude limits).
- Automatic IANA time-zone resolution from the ROI location (DST-correct conversion).
- Optional cross-check against ESA's published Sentinel-2 acquisition plans (KML).
- CSV / GeoPackage reporting, Esri footprint maps, a pass timeline chart, an ipyleaflet
  live tracker for Jupyter, and a standalone live-tracker HTML page.

Author: Julian Manning
Created: October 2026
Email: julian.manning@outlook.com
LinkedIn: https://www.linkedin.com/in/julian-manning/
"""

# ---------------- Core / Standard Library ----------------
import re
import json
import math
import time
import asyncio
import zipfile
import warnings
from pathlib import Path
from datetime import datetime
from typing import Optional, Union, Iterable
from zoneinfo import ZoneInfo
from contextlib import contextmanager
import xml.etree.ElementTree as ET

# ---------------- Data Handling ----------------
import numpy as np
import pandas as pd

# ---------------- Geospatial ----------------
import geopandas as gpd
import shapely
from shapely import wkt as shp_wkt
from shapely.geometry import Point, Polygon, box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from pyproj import CRS, Transformer, Geod

# ---------------- Orbit Mechanics ----------------
from skyfield.api import EarthSatellite, load, wgs84

# ---------------- Time Zones ----------------
from timezonefinder import TimezoneFinder

# ---------------- Visualisation / Mapping ----------------
import folium

# ---------------- Networking / IO ----------------
import requests

# ---------------- Constants ----------------
CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=TLE"
TLE_MIRROR_URL = "https://tle.ivanstanojevic.me/api/tle/{norad_id}"     # community CelesTrak mirror (fallback)
ESA_S2_PLAN_PAGE = "https://sentinels.copernicus.eu/copernicus/sentinel-2/acquisition-plans"
ESA_BASE_URL = "https://sentinels.copernicus.eu"

EARTH_RADIUS_KM = 6371.0088
GEOD = Geod(ellps="WGS84")

# Mission catalogue. Add rows to track other nadir-looking optical sensors
# (swath_km is the full cross-track width of the instrument footprint).
SATELLITE_CATALOGUE = {
    "LANDSAT 8": {
        "norad_id": 39084, "mission": "Landsat", "sensor": "OLI/TIRS",
        "swath_km": 185.0, "lat_limits": (-81.8, 81.8), "colour": "#F28E2B",
        "note": "USGS acquires ~all daytime land and near-coastal WRS-2 scenes.",
    },
    "LANDSAT 9": {
        "norad_id": 49260, "mission": "Landsat", "sensor": "OLI-2/TIRS-2",
        "swath_km": 185.0, "lat_limits": (-81.8, 81.8), "colour": "#E15759",
        "note": "USGS acquires ~all daytime land and near-coastal WRS-2 scenes.",
    },
    "SENTINEL-2A": {
        "norad_id": 40697, "mission": "Sentinel-2", "sensor": "MSI",
        "swath_km": 290.0, "lat_limits": (-56.0, 82.8), "colour": "#76B7B2",
        "note": "Extension campaign (to 31 Dec 2026): selected regions only - check the ESA plan.",
    },
    "SENTINEL-2B": {
        "norad_id": 42063, "mission": "Sentinel-2", "sensor": "MSI",
        "swath_km": 290.0, "lat_limits": (-56.0, 82.8), "colour": "#4E79A7",
        "note": "Nominal constellation (with S2C). Open ocean is not acquired.",
    },
    "SENTINEL-2C": {
        "norad_id": 60989, "mission": "Sentinel-2", "sensor": "MSI",
        "swath_km": 290.0, "lat_limits": (-56.0, 82.8), "colour": "#59A14F",
        "note": "Nominal constellation (with S2B). Open ocean is not acquired.",
    },
}

_TIMESCALE = None
_TZ_FINDER = None

# ====================================================================================================

def _ts():
    """Return a cached Skyfield timescale built from bundled data (no network needed)."""
    global _TIMESCALE
    if _TIMESCALE is None:
        _TIMESCALE = load.timescale(builtin=True)
    return _TIMESCALE


def _to_utc_timestamp(value) -> pd.Timestamp:
    """Coerce str / datetime / Timestamp (naive = UTC) to a tz-aware UTC Timestamp."""
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _resolve_satellites(satellites) -> list:
    """Normalise a satellite selection to catalogue keys (accepts names or NORAD ids)."""
    if satellites is None:
        return list(SATELLITE_CATALOGUE)
    by_id = {v["norad_id"]: k for k, v in SATELLITE_CATALOGUE.items()}
    out = []
    for s in satellites:
        if isinstance(s, (int, np.integer)) and int(s) in by_id:
            out.append(by_id[int(s)])
            continue
        key = str(s).upper().replace("_", " ").strip()
        key = {"L8": "LANDSAT 8", "L9": "LANDSAT 9", "S2A": "SENTINEL-2A",
               "S2B": "SENTINEL-2B", "S2C": "SENTINEL-2C"}.get(key, key)
        if key not in SATELLITE_CATALOGUE:
            raise ValueError(f"Unknown satellite '{s}'. Known: {list(SATELLITE_CATALOGUE)}")
        out.append(key)
    return out

# ====================================================================================================

@contextmanager
def process_timer(process_name="Process"):

    """
    Measure and report the execution time of a code block using a context manager.

    Records start and end wall-clock timestamps, prints readable timing
    information and the total elapsed duration in a human-friendly format.

    Args:
        process_name (str, optional): Descriptive name of the process being timed.
            Defaults to "Process".

    Yields:
        None: Control is passed to the wrapped block inside the ``with`` statement.
    """

    start_time = time.time()
    print(f"\n--- Starting {process_name} ---")
    print(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}\n")
    try:
        yield
    finally:
        elapsed = time.time() - start_time
        h, m, s = int(elapsed // 3600), int((elapsed % 3600) // 60), elapsed % 60
        print(f"\n--- {process_name} Complete ---")
        if h:
            msg = f"{h}h {m}m {int(s)}s"
        elif m:
            msg = f"{m}m {int(s)}s"
        else:
            msg = f"{s:.1f}s"
        print("\x1b[92m" + f"Elapsed time: {msg}" + "\x1b[0m")

# ====================================================================================================
# TLE RETRIEVAL
# ====================================================================================================

def _tle_checksum(line: str) -> int:
    """Modulo-10 TLE checksum over the first 68 characters ('-' counts as 1)."""
    total = 0
    for c in line[:68]:
        if c.isdigit():
            total += int(c)
        elif c == "-":
            total += 1
    return total % 10


def normalise_tle_line(line: str) -> str:

    """
    Validate a TLE data line and, if needed, rebuild its fixed-width column layout.

    TLEs copied from web pages (e.g. n2yo.com) often have their column spacing
    collapsed to single spaces, which breaks SGP4 parsers that rely on fixed
    column positions. This function re-pads each field to the NORAD two-line
    element specification and verifies the modulo-10 checksum, so a corrupted
    or mistyped line is rejected rather than silently producing a wrong orbit.

    Args:
        line (str): A TLE line starting with "1" or "2".

    Returns:
        str: A 69-character, checksum-verified TLE line.

    Raises:
        ValueError: If the line cannot be rebuilt or its checksum does not match.
    """

    line = line.strip()
    if len(line) == 69 and line[68].isdigit() and _tle_checksum(line) == int(line[68]):
        return line

    t = line.split()
    if t and t[0] == "1":
        if len(t) == 8:                      # missing international designator
            t.insert(2, "")
        if len(t) != 9:
            raise ValueError(f"Cannot parse TLE line 1: {line!r}")
        rebuilt = (f"1 {t[1]:<6} {t[2]:<8} {t[3]:>14} {t[4]:>10} "
                   f"{t[5]:>8} {t[6]:>8} {t[7]:>1} {t[8]:>5}")
    elif t and t[0] == "2":
        if len(t) == 8:
            tail = t[7]
        elif len(t) == 9:                    # mean motion and revolution number split
            tail = f"{t[7]:>11}{t[8]:>6}"
        else:
            raise ValueError(f"Cannot parse TLE line 2: {line!r}")
        rebuilt = (f"2 {t[1]:>5} {t[2]:>8} {t[3]:>8} {t[4]:>7} "
                   f"{t[5]:>8} {t[6]:>8} {tail:>17}")
    else:
        raise ValueError(f"Not a TLE data line: {line!r}")

    if len(rebuilt) != 69 or _tle_checksum(rebuilt) != int(rebuilt[68]):
        raise ValueError(f"TLE checksum failed after re-formatting: {line!r}")
    return rebuilt


def parse_tle_text(text: str) -> dict:

    """
    Parse TLE text (2-line or 3-line format, any spacing) into a NORAD-keyed dictionary.

    Args:
        text (str): Raw TLE text, e.g. a CelesTrak response or a pasted block.

    Returns:
        dict: ``{norad_id: {"name": str | None, "line1": str, "line2": str}}``.
    """

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out, i = {}, 0
    while i < len(lines):
        ln = lines[i].strip()
        name = None
        if not ln.startswith("1 "):
            name = ln.lstrip("0 ").strip() or None
            i += 1
            if i >= len(lines):
                break
            ln = lines[i].strip()
        if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].strip().startswith("2 "):
            l1 = normalise_tle_line(ln)
            l2 = normalise_tle_line(lines[i + 1])
            out[int(l1[2:7])] = {"name": name, "line1": l1, "line2": l2}
            i += 2
        else:
            i += 1
    return out


def _download_tle(norad: int, timeout: int):
    """Download one TLE: CelesTrak GP first, then the tle.ivanstanojevic.me mirror."""
    errors = []
    try:
        r = requests.get(CELESTRAK_GP_URL.format(norad_id=norad), timeout=timeout)
        r.raise_for_status()
        rec = parse_tle_text(r.text).get(norad)
        if rec:
            return rec, "CelesTrak"
        errors.append(f"CelesTrak: {r.text.strip()[:80] or 'empty response'}")
    except Exception as exc:
        errors.append(f"CelesTrak: {type(exc).__name__}")
    try:
        r = requests.get(TLE_MIRROR_URL.format(norad_id=norad), timeout=timeout)
        r.raise_for_status()
        j = r.json()
        rec = parse_tle_text(f"{j['line1']}\n{j['line2']}").get(norad)
        if rec:
            return rec, "TLE API mirror"
        errors.append("mirror: unparseable")
    except Exception as exc:
        errors.append(f"mirror: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors))


def fetch_tles(
    satellites: Optional[Iterable] = None,
    tle_dir: Union[str, Path] = Path("DATA") / "TLE",
    max_age_hours: float = 12.0,
    tle_text: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = True,
) -> dict:

    """
    Retrieve current TLEs for the selected satellites, with caching and offline fallback.

    For each satellite the function first looks for a cached ``<norad>.tle`` file
    in ``tle_dir``. If the cache is younger than ``max_age_hours`` it is reused;
    otherwise a fresh element set is requested from the CelesTrak GP service
    (falling back to the tle.ivanstanojevic.me mirror) and written back to the cache. If the network request fails, a stale cache is
    used with a warning, so predictions still run offline. CelesTrak asks users
    not to poll faster than its ~2-hourly update cycle — the cache honours that.

    Args:
        satellites (Iterable, optional): Catalogue names, short codes ("S2B", "L9")
            or NORAD ids. Defaults to all entries in ``SATELLITE_CATALOGUE``.
        tle_dir (str | Path, optional): Cache directory. Defaults to ``DATA/TLE``.
        max_age_hours (float, optional): Cache lifetime before re-download. Defaults to 12.
        tle_text (str, optional): Manually supplied TLE text (e.g. pasted from n2yo
            or Space-Track). When given, it overrides the network for the satellites
            it contains and is written to the cache.
        timeout (int, optional): HTTP timeout in seconds. Defaults to 30.
        verbose (bool, optional): Print a per-satellite status line. Defaults to True.

    Returns:
        dict: ``{name: {"norad_id", "line1", "line2", "epoch_utc", "source"}}``.

    Raises:
        RuntimeError: If no TLE can be obtained for a requested satellite.
    """

    names = _resolve_satellites(satellites)
    tle_dir = Path(tle_dir)
    tle_dir.mkdir(parents=True, exist_ok=True)
    manual = parse_tle_text(tle_text) if tle_text else {}

    out = {}
    for name in names:
        norad = SATELLITE_CATALOGUE[name]["norad_id"]
        cache = tle_dir / f"{norad}.tle"
        rec, source = None, None

        if norad in manual:
            rec, source = manual[norad], "manual"
            cache.write_text(f"{name}\n{rec['line1']}\n{rec['line2']}\n")
        else:
            age_h = (time.time() - cache.stat().st_mtime) / 3600 if cache.exists() else None
            if age_h is not None and age_h < max_age_hours:
                rec, source = parse_tle_text(cache.read_text()).get(norad), f"cache ({age_h:.1f} h old)"
            if rec is None:
                try:
                    rec, source = _download_tle(norad, timeout)
                    cache.write_text(f"{name}\n{rec['line1']}\n{rec['line2']}\n")
                except Exception as exc:
                    if cache.exists():
                        rec = parse_tle_text(cache.read_text()).get(norad)
                        source = f"stale cache ({age_h:.1f} h old; download failed: {type(exc).__name__})"
                    if rec is None:
                        raise RuntimeError(f"No TLE available for {name} ({norad}): {exc}") from exc

        sat = EarthSatellite(rec["line1"], rec["line2"], name, _ts())
        epoch = pd.Timestamp(sat.epoch.utc_datetime())
        out[name] = {"norad_id": norad, "line1": rec["line1"], "line2": rec["line2"],
                     "epoch_utc": epoch, "source": source}
        if verbose:
            age_d = (pd.Timestamp.now(tz="UTC") - epoch).total_seconds() / 86400
            flag = "\x1b[93m" if age_d > 7 else "\x1b[92m"
            print(f"{flag}✔ {name:<12} NORAD {norad:<6} epoch {epoch:%Y-%m-%d %H:%M} UTC "
                  f"({age_d:4.1f} d old) — {source}\x1b[0m")
    return out

# ====================================================================================================
# REGION OF INTEREST & TIME ZONE
# ====================================================================================================

def load_roi(
    source,
    use_bbox: bool = False,
    buffer_km: float = 0.0,
    name: Optional[str] = None,
) -> gpd.GeoDataFrame:

    """
    Load a Region of Interest from almost any input and return it as a single EPSG:4326 geometry.

    Accepted inputs:
        * Path / str to any vector file GeoPandas can read (any CRS — reprojected).
        * A GeoDataFrame or GeoSeries (any CRS).
        * A shapely geometry or WKT string (assumed EPSG:4326).
        * ``(lon, lat)`` for a point ROI.
        * ``(min_lon, min_lat, max_lon, max_lat)`` for a bounding box.

    Multi-feature inputs are dissolved into one geometry. Invalid geometries are
    repaired. A point ROI is valid — the pass test then answers "is this point
    inside the swath" and coverage is reported as 0 or 100 %.

    Args:
        source: The ROI input (see above).
        use_bbox (bool, optional): Replace the geometry by its bounding box. Defaults to False.
        buffer_km (float, optional): Geodesic-ish buffer applied in a local equal-area
            projection, e.g. to catch scenes that only clip the ROI edge. Defaults to 0.
        name (str, optional): Label stored in the ``name`` column (used in output file names).

    Returns:
        gpd.GeoDataFrame: One row, EPSG:4326, columns ``name`` and ``geometry``.
    """

    if isinstance(source, gpd.GeoDataFrame):
        gdf = source.copy()
    elif isinstance(source, gpd.GeoSeries):
        gdf = gpd.GeoDataFrame(geometry=source)
    elif isinstance(source, BaseGeometry):
        gdf = gpd.GeoDataFrame(geometry=[source], crs="EPSG:4326")
    elif isinstance(source, (tuple, list)) and len(source) in (2, 4) and all(
            isinstance(v, (int, float, np.floating)) for v in source):
        geom = Point(source) if len(source) == 2 else box(*source)
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    elif isinstance(source, str) and source.strip().upper().startswith(
            ("POINT", "POLYGON", "MULTIPOLYGON", "LINESTRING")):
        gdf = gpd.GeoDataFrame(geometry=[shp_wkt.loads(source)], crs="EPSG:4326")
    else:
        path = Path(source)
        gdf = gpd.read_file(path)
        name = name or path.stem

    if gdf.crs is None:
        warnings.warn("ROI has no CRS; assuming EPSG:4326.")
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")
    geom = shapely.make_valid(unary_union([g for g in gdf.geometry if g is not None and not g.is_empty]))
    if geom.is_empty:
        raise ValueError("ROI geometry is empty after cleaning.")

    if use_bbox:
        geom = box(*geom.bounds)
    if buffer_km:
        fwd, inv = _local_equal_area(geom)
        geom = transform(inv, transform(fwd, geom).buffer(buffer_km * 1000.0))

    return gpd.GeoDataFrame({"name": [name or "ROI"]}, geometry=[geom], crs="EPSG:4326")


def _roi_anchor(geom: BaseGeometry) -> Point:
    """Centroid if it lies inside the ROI, otherwise a guaranteed interior point."""
    c = geom.centroid
    return c if geom.covers(c) else geom.representative_point()


def _local_equal_area(geom: BaseGeometry):
    """Forward/inverse transformers for a Lambert azimuthal equal-area CRS centred on the ROI (metres)."""
    a = _roi_anchor(geom)
    crs = CRS.from_proj4(f"+proj=laea +lat_0={a.y} +lon_0={a.x} +datum=WGS84 +units=m +no_defs")
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return fwd, inv


def resolve_roi_timezone(roi_gdf: gpd.GeoDataFrame, override: Optional[str] = None, verbose: bool = True) -> str:

    """
    Resolve the IANA time zone of the ROI (e.g. "Australia/Melbourne").

    The zone is looked up at the ROI anchor point with ``timezonefinder`` (offline
    polygon database). Using an IANA zone rather than a fixed offset means
    daylight-saving transitions inside the prediction window are handled
    per-pass. The ROI corners are also checked: if they fall in different zones
    a warning is printed and the anchor-point zone is used. Offshore ROIs
    resolve to the nautical ``Etc/GMT±N`` zone.

    Args:
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        override (str, optional): Force a specific IANA zone name.
        verbose (bool, optional): Print the resolved zone. Defaults to True.

    Returns:
        str: IANA time-zone name.
    """

    global _TZ_FINDER
    if override:
        ZoneInfo(override)                      # validates the name
        tz_name = override
    else:
        if _TZ_FINDER is None:
            _TZ_FINDER = TimezoneFinder()
        geom = roi_gdf.geometry.iloc[0]
        a = _roi_anchor(geom)
        tz_name = _TZ_FINDER.timezone_at(lng=a.x, lat=a.y)
        if tz_name is None:
            offset = int(round(a.x / 15.0))
            tz_name = "Etc/GMT" + (f"{-offset:+d}" if offset else "")
        minx, miny, maxx, maxy = geom.bounds
        corners = {_TZ_FINDER.timezone_at(lng=x, lat=y) for x, y in
                   [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]} - {None}
        if len(corners | {tz_name}) > 1 and verbose:
            print(f"\x1b[93m⚠ ROI spans several time zones {sorted(corners | {tz_name})}; "
                  f"using '{tz_name}' (ROI anchor). Pass override= to choose another.\x1b[0m")
    if verbose:
        now = datetime.now(ZoneInfo(tz_name))
        print(f"🕒 ROI time zone: {tz_name} (currently UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}, {now.tzname()})")
    return tz_name


def visualise_roi_on_map(
    roi_gdf: gpd.GeoDataFrame,
    zoom_start: int = 8,
    tiles: str = "Esri.WorldImagery",
    polygon_color: str = "#FF3B30",
    save_path: Optional[str] = None,
) -> folium.Map:

    """
    Generate an interactive Folium map of the ROI for spatial verification.

    Args:
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        zoom_start (int, optional): Initial zoom (overridden by fit-to-bounds). Defaults to 8.
        tiles (str, optional): Folium / xyzservices tile name. Defaults to "Esri.WorldImagery".
        polygon_color (str, optional): ROI outline colour. Defaults to "#FF3B30".
        save_path (str, optional): Export the map to a standalone HTML file.

    Returns:
        folium.Map: Interactive map centred on the ROI.
    """

    geom = roi_gdf.geometry.iloc[0]
    a = _roi_anchor(geom)
    m = folium.Map(location=[a.y, a.x], zoom_start=zoom_start, tiles=tiles)
    if geom.geom_type == "Point":
        folium.CircleMarker([a.y, a.x], radius=7, color=polygon_color, fill=True,
                            tooltip="ROI point").add_to(m)
    else:
        folium.GeoJson(mapping(geom), name="ROI", tooltip="ROI",
                       style_function=lambda _: {"color": polygon_color, "weight": 2,
                                                 "fillOpacity": 0.1}).add_to(m)
        minx, miny, maxx, maxy = geom.bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(40, 40))
    if save_path:
        m.save(save_path)
    return m

# ====================================================================================================
# SOLAR GEOMETRY (analytic, ~0.01° — no ephemeris download required)
# ====================================================================================================

def _solar_ra_dec_gmst(times_utc):
    """Return (right ascension °, declination °, GMST °) for an array of UTC datetimes."""
    t = pd.DatetimeIndex(pd.to_datetime(times_utc, utc=True))
    # resolution-agnostic (pandas ≥2 may store s/ms/us/ns): seconds since the Unix epoch
    secs = (t - pd.Timestamp("1970-01-01", tz="UTC")).total_seconds().to_numpy()
    n = secs / 86400.0 + 2440587.5 - 2451545.0
    L = np.deg2rad((280.460 + 0.9856474 * n) % 360)
    g = np.deg2rad((357.528 + 0.9856003 * n) % 360)
    lam = L + np.deg2rad(1.915) * np.sin(g) + np.deg2rad(0.020) * np.sin(2 * g)
    eps = np.deg2rad(23.439 - 0.0000004 * n)
    ra = np.rad2deg(np.arctan2(np.cos(eps) * np.sin(lam), np.cos(lam)))
    dec = np.rad2deg(np.arcsin(np.sin(eps) * np.sin(lam)))
    gmst = (280.46061837 + 360.98564736629 * n) % 360
    return np.asarray(ra), np.asarray(dec), np.asarray(gmst)


def solar_elevation_deg(times_utc, lat: float, lon: float) -> np.ndarray:
    """Solar elevation angle (degrees, geometric) at (lat, lon) for one or many UTC times."""
    ra, dec, gmst = _solar_ra_dec_gmst(np.atleast_1d(times_utc))
    H = np.deg2rad(gmst + lon - ra)
    la, de = np.deg2rad(lat), np.deg2rad(dec)
    return np.rad2deg(np.arcsin(np.sin(la) * np.sin(de) + np.cos(la) * np.cos(de) * np.cos(H)))


def subsolar_point(when_utc) -> tuple:
    """(lat, lon) of the sub-solar point at a UTC time."""
    ra, dec, gmst = _solar_ra_dec_gmst([when_utc])
    lon = ((ra[0] - gmst[0] + 180) % 360) - 180
    return float(dec[0]), float(lon)

# ====================================================================================================
# ORBIT PROPAGATION & OVERPASS PREDICTION
# ====================================================================================================

def _build_satellite(name: str, tle: dict) -> EarthSatellite:
    return EarthSatellite(tle["line1"], tle["line2"], name, _ts())


def _propagate(sat: EarthSatellite, t0: pd.Timestamp, offsets_s: np.ndarray):
    """Vectorised SGP4 propagation → geodetic sub-satellite lat, lon (deg) and altitude (km)."""
    t = _ts().utc(t0.year, t0.month, t0.day, t0.hour, t0.minute,
                  t0.second + t0.microsecond / 1e6 + offsets_s)
    geo = sat.at(t)
    sp = wgs84.subpoint_of(geo)
    return sp.latitude.degrees, sp.longitude.degrees, wgs84.height_of(geo).km


def predict_overpasses(
    roi_gdf: gpd.GeoDataFrame,
    tles: dict,
    start_utc=None,
    days: float = 16.0,
    satellites: Optional[Iterable] = None,
    timezone_name: Optional[str] = None,
    daytime_descending_only: bool = True,
    min_sun_elevation_deg: float = 0.0,
    min_roi_coverage_pct: float = 0.0,
    coarse_step_s: float = 30.0,
    fine_step_s: float = 1.0,
    verbose: bool = True,
) -> gpd.GeoDataFrame:

    """
    Predict when each satellite's instrument swath will image the ROI.

    This is the core engine. Unlike horizon-based pass predictors (n2yo, Heavens-
    Above), which report when a satellite is merely *above the horizon*, this
    function asks whether the ROI falls inside the *instrument footprint*:

        1. **Coarse scan** — the orbit is propagated (SGP4) every ``coarse_step_s``
           seconds across the window and the geodesic distance from each sub-satellite
           point to the ROI anchor is computed. Samples closer than
           half-swath + ROI radius + 150 km are grouped into candidate windows (the
           margin guarantees no imaging pass can fall between two coarse samples).
        2. **Fine refinement** — each candidate is re-propagated every ``fine_step_s``
           seconds. The ground-track heading is derived from successive sub-points and
           the instantaneous cross-track scan line (±half swath, perpendicular to the
           ground track, as for yaw-steered pushbroom imagers) is built geodesically.
        3. **Intersection** — scan lines and the swath strip are projected into an
           ROI-centred Lambert azimuthal equal-area CRS and intersected with the ROI,
           giving ROI entry/exit times and the true fraction of the ROI imaged.
        4. **Attribution** — direction (ascending/descending), nadir offset, view angle,
           peak elevation seen from the ROI, solar elevation, TLE age, and an
           ``imaging_expected`` flag (descending + sun above threshold + inside the
           mission's latitude limits) are attached; times are converted to local time.

    Args:
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        tles (dict): Output of ``fetch_tles``.
        start_utc (str | datetime, optional): Window start (naive = UTC). Defaults to now.
        days (float, optional): Window length in days. Defaults to 16 (one Landsat
            repeat cycle; Sentinel-2 repeats every 10 days per unit).
        satellites (Iterable, optional): Subset of satellites. Defaults to all in ``tles``.
        timezone_name (str, optional): IANA zone for local times. Defaults to the
            zone resolved from the ROI.
        daytime_descending_only (bool, optional): Keep only passes where imaging is
            expected (descending, sunlit). Set False to also list night/ascending
            geometric passes. Defaults to True.
        min_sun_elevation_deg (float, optional): Sun-elevation threshold at the ROI for
            ``imaging_expected``. Defaults to 0.
        min_roi_coverage_pct (float, optional): Drop passes covering less of the ROI.
            Defaults to 0 (any intersection).
        coarse_step_s (float, optional): Coarse search step. Defaults to 30.
        fine_step_s (float, optional): Refinement step (time resolution). Defaults to 1.
        verbose (bool, optional): Print a per-satellite summary. Defaults to True.

    Returns:
        gpd.GeoDataFrame: One row per overpass, sorted by time, EPSG:4326 swath strip
        geometry (clipped to ±~150 km around the ROI) and the columns:
        ``satellite, mission, sensor, norad_id, direction, imaging_expected,
        roi_coverage_pct, roi_entry_utc, overpass_utc, roi_exit_utc, duration_s,
        roi_entry_local, overpass_local, roi_exit_local, timezone, utc_offset,
        nadir_offset_km, roi_side_of_track, edge_margin_km, view_angle_deg, max_elevation_deg,
        sun_elevation_deg, sat_altitude_km, tle_epoch_utc, tle_age_days, note``.

    Notes:
        - ``overpass_utc`` is the closest approach to the ROI anchor point
          (≈ scene centre time for a small ROI), refined below the time step.
        - ``edge_margin_km`` = half swath − nadir offset of the ROI anchor. Values
          within ~±5 km of zero are uncertain: the real swath edge differs slightly
          from the nominal width (it varies with altitude and detector layout).
        - SGP4 + fresh TLEs typically place a sun-synchronous EO satellite within a
          few km / ~1 s along-track; accuracy degrades with TLE age (see
          ``tle_age_days``) and around orbit-maintenance manoeuvres. Refresh TLEs
          before relying on predictions more than ~2 weeks ahead.
        - Geometry says *can* image. Whether data are actually *acquired* depends on
          the mission plan — use ``crosscheck_s2_acquisition_plan`` for Sentinel-2.
    """

    roi = roi_gdf.geometry.iloc[0]
    anchor = _roi_anchor(roi)
    lat0, lon0 = anchor.y, anchor.x
    fwd, inv = _local_equal_area(roi)
    roi_eq = transform(fwd, roi)
    shapely.prepare(roi_eq)
    roi_area = roi_eq.area
    xs, ys = (np.asarray(roi_eq.exterior.coords).T if roi_eq.geom_type == "Polygon"
              else np.asarray(shapely.get_coordinates(roi_eq)).T)
    roi_radius_km = float(np.max(np.hypot(xs, ys))) / 1000.0 if len(xs) else 0.0

    tz_name = timezone_name or resolve_roi_timezone(roi_gdf, verbose=verbose)
    tz = ZoneInfo(tz_name)
    t0 = _to_utc_timestamp(start_utc) if start_utc is not None else pd.Timestamp.now(tz="UTC").floor("min")
    total_s = float(days) * 86400.0
    topos = wgs84.latlon(lat0, lon0)

    names = _resolve_satellites(satellites) if satellites else list(tles)
    rows = []
    for name in names:
        if name not in tles:
            raise KeyError(f"No TLE loaded for {name}; include it in fetch_tles().")
        cat = SATELLITE_CATALOGUE[name]
        sat = _build_satellite(name, tles[name])
        half_km = cat["swath_km"] / 2.0
        thresh_km = half_km + roi_radius_km + 150.0

        # ---- 1. coarse scan
        off = np.arange(0.0, total_s + coarse_step_s, coarse_step_s)
        clat, clon, _ = _propagate(sat, t0, off)
        _, _, d = GEOD.inv(np.full_like(clon, lon0), np.full_like(clat, lat0), clon, clat)
        idx = np.flatnonzero(d / 1000.0 < thresh_km)
        if idx.size == 0:
            if verbose:
                print(f"  {name:<12} 0 candidate passes")
            continue
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)

        kept = 0
        for run in runs:
            # ---- 2. fine refinement
            f_off = np.arange(max(off[run[0]] - 2 * coarse_step_s, 0.0),
                              min(off[run[-1]] + 2 * coarse_step_s, total_s) + fine_step_s,
                              fine_step_s)
            lat, lon, alt = _propagate(sat, t0, f_off)
            az, _, _ = GEOD.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
            az = np.append(az, az[-1])
            llon, llat, _ = GEOD.fwd(lon, lat, az - 90.0, np.full_like(lon, half_km * 1000.0))
            rlon, rlat, _ = GEOD.fwd(lon, lat, az + 90.0, np.full_like(lon, half_km * 1000.0))
            lx, ly = fwd(llon, llat)
            rx, ry = fwd(rlon, rlat)
            nx, ny = fwd(lon, lat)

            # ---- 3. intersection
            # swath cells: quadrilateral between consecutive scan lines (gap-free, works for point ROIs)
            L_ = np.column_stack([lx, ly])
            R_ = np.column_stack([rx, ry])
            quads = shapely.polygons(np.stack([L_[:-1], L_[1:], R_[1:], R_[:-1], L_[:-1]], axis=1))
            hits = np.flatnonzero(shapely.intersects(quads, roi_eq))
            if hits.size == 0:
                continue
            i_in, i_out = hits[0], hits[-1] + 1
            d2 = nx ** 2 + ny ** 2
            i_ca = int(np.argmin(d2))
            frac = 0.0                                # sub-step closest approach (parabolic fit on d²)
            if 0 < i_ca < len(d2) - 1:
                den = d2[i_ca - 1] - 2 * d2[i_ca] + d2[i_ca + 1]
                if den > 0:
                    frac = float(np.clip(0.5 * (d2[i_ca - 1] - d2[i_ca + 1]) / den, -0.5, 0.5))
            t_ca_off = float(f_off[i_ca]) + frac * fine_step_s
            if roi_eq.geom_type == "Point":          # a point is imaged at a single instant
                i_in = i_out = i_ca

            strip = Polygon(np.vstack([np.column_stack([lx, ly]),
                                       np.column_stack([rx, ry])[::-1]]))
            strip = shapely.make_valid(strip)
            if roi_area > 0:
                coverage = 100.0 * strip.intersection(roi_eq).area / roi_area
            else:
                coverage = 100.0
            if coverage < min_roi_coverage_pct:
                continue

            # ---- 4. attribution
            i_prev, i_next = max(i_ca - 1, 0), min(i_ca + 1, len(lat) - 1)
            direction = "Ascending" if lat[i_next] > lat[i_prev] else "Descending"
            az_to_roi, _, d_ca = GEOD.inv(lon[i_ca], lat[i_ca], lon0, lat0)
            d_ca_km = d_ca / 1000.0
            gamma = d_ca_km / EARTH_RADIUS_KM
            view = math.degrees(math.atan2(EARTH_RADIUS_KM * math.sin(gamma),
                                           EARTH_RADIUS_KM + alt[i_ca] - EARTH_RADIUS_KM * math.cos(gamma)))
            side = "nadir" if d_ca_km < 1.0 else ("E" if 0.0 < (az_to_roi % 360.0) < 180.0 else "W")

            t_in = t0 + pd.Timedelta(seconds=float(f_off[i_in]))
            t_ca = t0 + pd.Timedelta(milliseconds=int(round(t_ca_off * 10)) * 100)
            t_out = t0 + pd.Timedelta(seconds=float(f_off[i_out]))
            sun_el = float(solar_elevation_deg([t_ca], lat0, lon0)[0])
            t_sf = _ts().from_datetime(t_ca.to_pydatetime())
            max_el = float((sat - topos).at(t_sf).altaz()[0].degrees)
            lo, hi = cat["lat_limits"]
            expected = bool(direction == "Descending" and sun_el >= min_sun_elevation_deg
                            and lo <= lat0 <= hi)
            if daytime_descending_only and not expected:
                continue

            # display strip: ROI crossing ±~150 km along track
            pad = int(round(20.0 / fine_step_s))
            j0, j1 = max(i_in - pad, 0), min(i_out + pad, len(lat) - 1)
            disp = Polygon(np.vstack([np.column_stack([lx[j0:j1 + 1], ly[j0:j1 + 1]]),
                                      np.column_stack([rx[j0:j1 + 1], ry[j0:j1 + 1]])[::-1]]))
            disp = transform(inv, shapely.make_valid(disp))

            epoch = tles[name]["epoch_utc"]
            loc_ca = t_ca.tz_convert(tz)
            off_s = loc_ca.utcoffset().total_seconds()
            rows.append({
                "satellite": name, "mission": cat["mission"], "sensor": cat["sensor"],
                "norad_id": cat["norad_id"], "direction": direction,
                "imaging_expected": expected, "roi_coverage_pct": round(coverage, 1),
                "roi_entry_utc": t_in, "overpass_utc": t_ca, "roi_exit_utc": t_out,
                "duration_s": round(float(f_off[i_out] - f_off[i_in]), 1),
                "roi_entry_local": t_in.tz_convert(tz), "overpass_local": loc_ca,
                "roi_exit_local": t_out.tz_convert(tz),
                "timezone": tz_name,
                "utc_offset": f"{'+' if off_s >= 0 else '-'}{int(abs(off_s) // 3600):02d}:{int(abs(off_s) % 3600 // 60):02d}",
                "nadir_offset_km": round(d_ca_km, 1), "roi_side_of_track": side,
                "edge_margin_km": round(half_km - d_ca_km, 1),
                "view_angle_deg": round(view, 2), "max_elevation_deg": round(max_el, 1),
                "sun_elevation_deg": round(sun_el, 1), "sat_altitude_km": round(float(alt[i_ca]), 1),
                "tle_epoch_utc": epoch,
                "tle_age_days": round((t_ca - epoch).total_seconds() / 86400.0, 1),
                "note": cat["note"], "geometry": disp,
            })
            kept += 1
        if verbose:
            print(f"  {name:<12} {kept} overpass(es)")

    cols = ["satellite", "mission", "sensor", "norad_id", "direction", "imaging_expected",
            "roi_coverage_pct", "roi_entry_utc", "overpass_utc", "roi_exit_utc", "duration_s",
            "roi_entry_local", "overpass_local", "roi_exit_local", "timezone", "utc_offset",
            "nadir_offset_km", "roi_side_of_track", "edge_margin_km", "view_angle_deg", "max_elevation_deg",
            "sun_elevation_deg", "sat_altitude_km", "tle_epoch_utc", "tle_age_days", "note", "geometry"]
    gdf = gpd.GeoDataFrame(rows, columns=cols, geometry="geometry", crs="EPSG:4326")
    gdf = gdf.sort_values("overpass_utc").reset_index(drop=True)
    gdf.attrs.update({"roi_name": roi_gdf["name"].iloc[0] if "name" in roi_gdf else "ROI",
                      "timezone": tz_name, "window_start_utc": t0,
                      "window_end_utc": t0 + pd.Timedelta(seconds=total_s)})
    if verbose:
        print(f"\n🛰️  {len(gdf)} overpass(es) between {t0:%Y-%m-%d %H:%M} and "
              f"{gdf.attrs['window_end_utc']:%Y-%m-%d %H:%M} UTC")
    return gdf

# ====================================================================================================
# REPORTING
# ====================================================================================================

def summarise_overpasses(passes: gpd.GeoDataFrame) -> pd.DataFrame:

    """
    Tabulate predicted overpasses in a compact, human-readable form.

    Args:
        passes (gpd.GeoDataFrame): Output of ``predict_overpasses``.

    Returns:
        pd.DataFrame: Satellite, local day/date, local time window, UTC time,
        direction, ROI coverage, off-nadir angle, sun elevation, expected flag and
        (if present) the ESA plan status.
    """

    if passes.empty:
        return pd.DataFrame()
    tz = passes["timezone"].iloc[0]
    df = pd.DataFrame({
        "Satellite": passes["satellite"],
        "Local date": passes["overpass_local"].dt.strftime("%a %d %b %Y"),
        f"Local time ({tz})": passes["roi_entry_local"].dt.strftime("%H:%M:%S") + " – "
                               + passes["roi_exit_local"].dt.strftime("%H:%M:%S"),
        "UTC offset": passes["utc_offset"],
        "Overpass (UTC)": passes["overpass_utc"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Direction": passes["direction"],
        "ROI cover %": passes["roi_coverage_pct"],
        "View angle °": passes["view_angle_deg"],
        "Sun elev °": passes["sun_elevation_deg"],
        "Expected": passes["imaging_expected"].map({True: "✔", False: "✘"}),
    })
    if "esa_plan_status" in passes:
        df["ESA plan"] = passes["esa_plan_status"]
    return df


def next_overpasses(passes: gpd.GeoDataFrame, now_utc=None) -> pd.DataFrame:
    """Next upcoming overpass per satellite with a live countdown (n2yo-style)."""
    now = _to_utc_timestamp(now_utc) if now_utc is not None else pd.Timestamp.now(tz="UTC")
    up = passes[passes["roi_exit_utc"] >= now]
    if up.empty:
        return pd.DataFrame()
    nxt = up.sort_values("overpass_utc").groupby("satellite", sort=False).head(1)

    def _cd(t):
        s = int((t - now).total_seconds())
        if s <= 0:
            return "imaging now"
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        return f"{d}d {h:02d}h {r // 60:02d}m"

    return pd.DataFrame({
        "Satellite": nxt["satellite"].values,
        "Countdown": [_cd(t) for t in nxt["roi_entry_utc"]],
        "Local": nxt["overpass_local"].dt.strftime("%a %d %b %H:%M:%S").values,
        "UTC": nxt["overpass_utc"].dt.strftime("%Y-%m-%d %H:%M:%S").values,
        "ROI cover %": nxt["roi_coverage_pct"].values,
    })


def export_overpass_csv(
    passes: gpd.GeoDataFrame,
    out_dir: Union[str, Path] = "OUTPUT",
    file_name: Optional[str] = None,
    expected_only: bool = False,
) -> Path:

    """
    Write the overpass report to CSV with both UTC and ROI-local times.

    Times are written as ISO-8601 strings: UTC columns end in ``Z``; local columns
    carry their explicit offset (``+11:00``) so the file is unambiguous across DST
    changes, plus split ``local_date`` / ``local_time`` columns for spreadsheets.
    The swath geometry is not written (see ``export_overpass_footprints``).

    Args:
        passes (gpd.GeoDataFrame): Output of ``predict_overpasses`` (optionally cross-checked).
        out_dir (str | Path, optional): Output folder. Defaults to "OUTPUT".
        file_name (str, optional): Override the file name. Defaults to
            ``overpasses_<roi>_<start>_<end>.csv``.
        expected_only (bool, optional): Write only rows with ``imaging_expected``.

    Returns:
        Path: Path of the written CSV.
    """

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(passes.drop(columns="geometry")).copy()
    if expected_only:
        df = df[df["imaging_expected"]]

    for c in ["roi_entry_utc", "overpass_utc", "roi_exit_utc", "tle_epoch_utc"]:
        if c in df:
            df[c] = df[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in ["roi_entry_local", "overpass_local", "roi_exit_local"]:
        if c in df:
            df[c] = df[c].apply(lambda t: t.isoformat(timespec="seconds"))
    if len(df):
        loc = passes.loc[df.index, "overpass_local"]
        df.insert(df.columns.get_loc("timezone"), "local_date", loc.dt.strftime("%Y-%m-%d").values)
        df.insert(df.columns.get_loc("timezone"), "local_time", loc.dt.strftime("%H:%M:%S").values)
        df.insert(df.columns.get_loc("timezone"), "local_weekday", loc.dt.strftime("%A").values)

    if file_name is None:
        roi = re.sub(r"[^A-Za-z0-9_-]+", "_", str(passes.attrs.get("roi_name", "ROI")))
        s = passes.attrs.get("window_start_utc")
        e = passes.attrs.get("window_end_utc")
        span = f"_{s:%Y%m%d}_{e:%Y%m%d}" if s is not None and e is not None else ""
        file_name = f"overpasses_{roi}{span}.csv"
    path = out_dir / file_name
    df.to_csv(path, index=False)
    print(f"💾 {len(df)} overpass(es) written to {path}")
    return path


def export_overpass_footprints(passes: gpd.GeoDataFrame, path: Union[str, Path]) -> Path:
    """Write the per-pass swath strips to a GeoPackage / GeoJSON / shapefile for QGIS or ArcGIS."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    g = passes.copy()
    for c in g.columns:
        if pd.api.types.is_datetime64_any_dtype(g[c]):
            g[c] = g[c].astype(str)
    g.to_file(path)
    print(f"💾 {len(g)} footprint(s) written to {path}")
    return path

# ====================================================================================================
# VISUALISATION
# ====================================================================================================

def visualise_overpass_footprints(
    passes: gpd.GeoDataFrame,
    roi_gdf: gpd.GeoDataFrame,
    tiles: str = "Esri.WorldImagery",
    expected_only: bool = True,
    max_passes: Optional[int] = None,
    save_path: Optional[str] = None,
) -> folium.Map:

    """
    Map the predicted swath strips over the ROI, one toggleable layer per satellite.

    Hovering a strip shows satellite, local and UTC times, direction, ROI coverage
    and view angle.

    Args:
        passes (gpd.GeoDataFrame): Output of ``predict_overpasses``.
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        tiles (str, optional): Basemap. Defaults to "Esri.WorldImagery".
        expected_only (bool, optional): Draw only passes where imaging is expected.
        max_passes (int, optional): Draw only the next N passes.
        save_path (str, optional): Export to standalone HTML.

    Returns:
        folium.Map: Interactive footprint map.
    """

    m = visualise_roi_on_map(roi_gdf, tiles=tiles)
    p = passes[passes["imaging_expected"]] if expected_only else passes
    if max_passes:
        p = p.head(max_passes)
    tz = passes["timezone"].iloc[0] if len(passes) else ""

    for sat, grp in p.groupby("satellite", sort=False):
        colour = SATELLITE_CATALOGUE.get(sat, {}).get("colour", "#00E5FF")
        fg = folium.FeatureGroup(name=f"{sat} ({len(grp)})")
        for _, r in grp.iterrows():
            tip = (f"<b>{sat}</b><br>{r['overpass_local']:%a %d %b %Y %H:%M:%S} ({tz})"
                   f"<br>{r['overpass_utc']:%Y-%m-%d %H:%M:%S} UTC<br>{r['direction']} · "
                   f"ROI cover {r['roi_coverage_pct']:.0f}% · view {r['view_angle_deg']:.1f}°")
            if "esa_plan_status" in r:
                tip += f"<br>ESA plan: {r['esa_plan_status']}"
            folium.GeoJson(mapping(r.geometry), tooltip=folium.Tooltip(tip),
                           style_function=lambda _, c=colour: {"color": c, "weight": 1.5,
                                                               "fillColor": c, "fillOpacity": 0.12}
                           ).add_to(fg)
        fg.add_to(m)
    if len(p):
        minx, miny, maxx, maxy = p.total_bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))
    folium.LayerControl(collapsed=False).add_to(m)
    if save_path:
        m.save(save_path)
    return m


def plot_overpass_timeline(passes: gpd.GeoDataFrame, save_path: Optional[str] = None, ax=None):

    """
    Plot a pass timeline: one row per satellite, local date on x, time-of-day annotated.

    Filled markers are expected acquisitions; hollow markers are geometric passes
    flagged as not expected (only present with ``daytime_descending_only=False``)
    or not in the ESA plan (after ``crosscheck_s2_acquisition_plan``). Marker
    size scales with ROI coverage.

    Args:
        passes (gpd.GeoDataFrame): Output of ``predict_overpasses``.
        save_path (str, optional): Save the figure (PNG/SVG/PDF).
        ax (matplotlib Axes, optional): Draw into an existing axes.

    Returns:
        matplotlib.figure.Figure
    """

    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    sats = [s for s in SATELLITE_CATALOGUE if s in set(passes["satellite"])]
    tz = passes["timezone"].iloc[0]
    if ax is None:
        fig, ax = plt.subplots(figsize=(11, 0.55 * len(sats) + 1.6))
    else:
        fig = ax.figure

    for y, sat in enumerate(sats):
        g = passes[passes["satellite"] == sat]
        filled = g["imaging_expected"].copy()
        if "esa_plan_status" in g:
            filled &= ~g["esa_plan_status"].str.startswith("not planned")
        x = g["overpass_local"].dt.tz_localize(None)
        c = SATELLITE_CATALOGUE[sat]["colour"]
        size = 30 + 1.4 * g["roi_coverage_pct"]
        ax.scatter(x[filled], [y] * filled.sum(), s=size[filled], color=c, zorder=3)
        ax.scatter(x[~filled], [y] * (~filled).sum(), s=size[~filled], facecolor="none",
                   edgecolor=c, linewidth=1.2, zorder=3)
        for xi, t in zip(x, g["overpass_local"]):
            ax.annotate(t.strftime("%H:%M"), (xi, y), xytext=(0, 9), textcoords="offset points",
                        ha="center", fontsize=7, color="#555")
    ax.set_yticks(range(len(sats)), sats)
    ax.set_ylim(-0.7, len(sats) - 0.3)
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d\n%b"))
    ax.grid(axis="x", color="#ddd", linewidth=0.6)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0)
    roi = passes.attrs.get("roi_name", "ROI")
    ax.set_title(f"Predicted overpasses — {roi} (local time, {tz})", loc="left", fontsize=11)
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
    return fig

# ====================================================================================================
# ESA SENTINEL-2 ACQUISITION PLANS
# ====================================================================================================

_PLAN_RE = re.compile(r"(s2[abc])_mp_acq__kml_(\d{8}t\d{6})_(\d{8}t\d{6})", re.I)


def fetch_s2_acquisition_plans(
    plan_dir: Union[str, Path] = Path("DATA") / "KML",
    start_utc=None,
    end_utc=None,
    satellites: Iterable = ("S2A", "S2B", "S2C"),
    timeout: int = 90,
    verbose: bool = True,
) -> list:

    """
    Download the ESA Sentinel-2 acquisition-plan KMLs that overlap a time window.

    ESA publishes the mission planning for each Sentinel-2 unit as KML files of
    planned data-takes (usually 10–18 days each, refreshed weekly). This function
    scrapes the acquisition-plans page for KML links, keeps those whose planning
    window (encoded in the file name) overlaps ``[start_utc, end_utc]``, and caches
    them in ``plan_dir``. If the page layout changes, download the files manually
    from the page and drop them into ``plan_dir`` — ``load_s2_acquisition_plans``
    reads any KML there.

    Args:
        plan_dir (str | Path, optional): Cache folder. Defaults to ``DATA/KML``.
        start_utc, end_utc (optional): Window of interest. Defaults to now → now + 16 d.
        satellites (Iterable, optional): Units to fetch. Defaults to S2A, S2B, S2C.
        timeout (int, optional): HTTP timeout (s). Defaults to 90.
        verbose (bool, optional): Print progress. Defaults to True.

    Returns:
        list[Path]: Local KML paths covering the window.
    """

    plan_dir = Path(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    start = _to_utc_timestamp(start_utc) if start_utc is not None else pd.Timestamp.now(tz="UTC")
    end = _to_utc_timestamp(end_utc) if end_utc is not None else start + pd.Timedelta(days=16)
    wanted = {s.lower() for s in satellites}

    html = requests.get(ESA_S2_PLAN_PAGE, timeout=timeout).text
    links = {}
    for m in re.finditer(r'href="([^"]*?(s2[abc])_mp_acq__kml_(\d{8}t\d{6})_(\d{8}t\d{6})[^"]*)"', html, re.I):
        href, sat, a, b = m.groups()
        if sat.lower() not in wanted:
            continue
        p0 = pd.Timestamp(datetime.strptime(a.lower(), "%Y%m%dt%H%M%S"), tz="UTC")
        p1 = pd.Timestamp(datetime.strptime(b.lower(), "%Y%m%dt%H%M%S"), tz="UTC")
        if p1 < start or p0 > end:
            continue
        url = href if href.startswith("http") else ESA_BASE_URL + href
        links[f"{sat.lower()}_mp_acq__kml_{a.lower()}_{b.lower()}"] = url

    paths, failed = [], []
    for stem, url in sorted(links.items()):
        path = plan_dir / f"{stem}.kml"
        if path.exists():
            if verbose:
                print(f"✔ {path.name} (cached)")
            paths.append(path)
            continue
        # ESA links occasionally carry a broken suffix or need ?download=true — try variants,
        # and never let one bad link stop the remaining units from downloading.
        base = url.split("?")[0]
        plain = f"{ESA_BASE_URL}/documents/d/sentinel/{stem}"
        candidates = list(dict.fromkeys([url, base + "?download=true", plain, plain + "?download=true"]))
        last_err = None
        for cand in candidates:
            try:
                r = requests.get(cand, timeout=timeout)
                r.raise_for_status()
                if b"<kml" not in r.content[:2000] and r.content[:2] != b"PK":
                    raise ValueError("response is not KML/KMZ")
                path.write_bytes(r.content)
                if verbose:
                    print(f"⬇ {path.name} ({len(r.content) / 1e6:.1f} MB)")
                paths.append(path)
                break
            except Exception as exc:
                last_err = exc
        else:
            failed.append(stem)
            if verbose:
                print(f"\x1b[93m⚠ {stem}: download failed on ESA's link ({type(last_err).__name__}). "
                      f"Skipping; save it manually into {plan_dir} if the page link is fixed.\x1b[0m")
    if failed and verbose:
        print(f"\x1b[93m⚠ {len(failed)} plan file(s) unavailable — passes they cover will show "
              f"'no plan loaded' (or rely on an older overlapping plan).\x1b[0m")
    if not paths and verbose:
        print("\x1b[93m⚠ No acquisition-plan KMLs found for this window "
              "(plans are typically published ~2 weeks ahead).\x1b[0m")
    return paths


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_s2_acquisition_plans(source: Union[str, Path, Iterable] = Path("DATA") / "KML") -> gpd.GeoDataFrame:

    """
    Parse ESA Sentinel-2 acquisition-plan KML/KMZ files into a GeoDataFrame of data-takes.

    Each Placemark becomes one row with its unit, plan category (the KML folder,
    e.g. ``NOMINAL`` — the only category distributed to users), data-take id,
    observation start/stop (UTC), absolute/relative orbit and footprint polygon.
    The planning window of each file is taken from the KML document name
    ("... from YYYYMMDDTHHMMSS to YYYYMMDDTHHMMSS") or, failing that, the file name.

    Args:
        source (str | Path | Iterable): A folder of KML/KMZ files, a single file, or a list.

    Returns:
        gpd.GeoDataFrame: EPSG:4326 data-take footprints.
    """

    if isinstance(source, (str, Path)) and Path(source).is_dir():
        files = sorted(list(Path(source).glob("*.kml")) + list(Path(source).glob("*.kmz")))
    elif isinstance(source, (str, Path)):
        files = [Path(source)]
    else:
        files = [Path(f) for f in source]

    rows = []
    for f in files:
        raw = f.read_bytes()
        if raw[:2] == b"PK":
            with zipfile.ZipFile(f) as z:
                raw = z.read(next(n for n in z.namelist() if n.lower().endswith(".kml")))
        root = ET.fromstring(raw)

        doc_name = next((el.text or "" for el in root.iter() if _strip_ns(el.tag) == "name"), "")
        m = re.search(r"from\s+(\d{8}T\d{6})\s+to\s+(\d{8}T\d{6})", doc_name, re.I)
        m = m or _PLAN_RE.search(f.name)
        if m:
            a, b = m.groups()[-2:]
            plan_start = pd.Timestamp(datetime.strptime(a.upper(), "%Y%m%dT%H%M%S"), tz="UTC")
            plan_end = pd.Timestamp(datetime.strptime(b.upper(), "%Y%m%dT%H%M%S"), tz="UTC")
        else:
            plan_start = plan_end = pd.NaT

        def walk(node, folders):
            for child in node:
                tag = _strip_ns(child.tag)
                if tag in ("Document", "Folder"):
                    nm = next((c.text for c in child if _strip_ns(c.tag) == "name"), None)
                    walk(child, folders + ([nm] if (tag == "Folder" and nm) else []))
                elif tag == "Placemark":
                    yield_placemark(child, folders)

        def yield_placemark(pm, folders):
            data = {d.get("name"): next((v.text for v in d if _strip_ns(v.tag) == "value"), None)
                    for d in pm.iter() if _strip_ns(d.tag) == "Data"}
            polys = []
            for pg in (el for el in pm.iter() if _strip_ns(el.tag) == "Polygon"):
                outer = next((el for el in pg.iter() if _strip_ns(el.tag) == "outerBoundaryIs"), pg)
                coords = next((el.text for el in outer.iter() if _strip_ns(el.tag) == "coordinates"), "")
                pts = [tuple(map(float, c.split(",")[:2])) for c in coords.split()]
                if len(pts) >= 3:
                    lons = np.array([p[0] for p in pts])
                    if lons.max() - lons.min() > 180:          # antimeridian: shift to 0–360
                        pts = [((x + 360) if x < 0 else x, y) for x, y in pts]
                    polys.append(shapely.make_valid(Polygon(pts)))
            sat_folder = next((fo for fo in folders if re.fullmatch(r"S2[ABC]", fo or "", re.I)), None)
            sat_code = (sat_folder or (_PLAN_RE.search(f.name).group(1) if _PLAN_RE.search(f.name) else "")).upper()
            rows.append({
                "satellite": {"S2A": "SENTINEL-2A", "S2B": "SENTINEL-2B", "S2C": "SENTINEL-2C"}.get(sat_code, sat_code),
                "plan_category": folders[-1] if folders else None,
                "datatake_id": data.get("ID"),
                "mode": data.get("Mode"),
                "obs_start_utc": pd.to_datetime(data.get("ObservationTimeStart"), utc=True, errors="coerce"),
                "obs_stop_utc": pd.to_datetime(data.get("ObservationTimeStop"), utc=True, errors="coerce"),
                "orbit_absolute": pd.to_numeric(data.get("OrbitAbsolute"), errors="coerce"),
                "orbit_relative": pd.to_numeric(data.get("OrbitRelative"), errors="coerce"),
                "plan_file": f.name, "plan_start_utc": plan_start, "plan_end_utc": plan_end,
                "geometry": unary_union(polys) if polys else None,
            })

        walk(root, [])

    cols = ["satellite", "plan_category", "datatake_id", "mode", "obs_start_utc", "obs_stop_utc",
            "orbit_absolute", "orbit_relative", "plan_file", "plan_start_utc", "plan_end_utc", "geometry"]
    gdf = gpd.GeoDataFrame(rows, columns=cols, geometry="geometry", crs="EPSG:4326")
    if not files:
        print("📄 No acquisition-plan KML files found.")
    elif len(gdf):
        print(f"📄 {len(gdf)} planned data-take(s) from {len(files)} KML file(s): "
              + ", ".join(f"{k} {v}" for k, v in gdf['satellite'].value_counts().sort_index().items()))
    return gdf


def crosscheck_s2_acquisition_plan(
    passes: gpd.GeoDataFrame,
    plans: gpd.GeoDataFrame,
    roi_gdf: gpd.GeoDataFrame,
    time_tolerance_s: float = 120.0,
) -> gpd.GeoDataFrame:

    """
    Flag each predicted Sentinel-2 overpass as planned / not planned by ESA.

    A pass is ``planned (NOMINAL)`` when a data-take of the same unit overlaps the
    pass's ROI entry–exit window (± ``time_tolerance_s``) and its footprint
    intersects the ROI. Data-takes in other plan categories (e.g. vicarious
    calibration) are reported as such because they are not distributed. Passes
    outside the time span of every loaded plan for that unit are reported as
    ``no plan loaded``; Landsat rows get ``n/a``.

    Args:
        passes (gpd.GeoDataFrame): Output of ``predict_overpasses``.
        plans (gpd.GeoDataFrame): Output of ``load_s2_acquisition_plans``.
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        time_tolerance_s (float, optional): Timing tolerance. Defaults to 120 s.

    Returns:
        gpd.GeoDataFrame: ``passes`` with ``esa_plan_status``, ``esa_datatake_id``
        and ``esa_relative_orbit`` columns.
    """

    out = passes.copy()
    roi = roi_gdf.geometry.iloc[0]
    roi_alt = transform(lambda x, y, z=None: (np.where(np.asarray(x) < 0, np.asarray(x) + 360, x), y), roi)
    tol = pd.Timedelta(seconds=time_tolerance_s)
    status, dt_id, rel = [], [], []

    for _, r in out.iterrows():
        if r["mission"] != "Sentinel-2":
            status.append("n/a"); dt_id.append(None); rel.append(None)
            continue
        p = plans[plans["satellite"] == r["satellite"]]
        spans = p[["plan_start_utc", "plan_end_utc"]].drop_duplicates()
        covered = ((spans["plan_start_utc"] <= r["overpass_utc"]) & (spans["plan_end_utc"] >= r["overpass_utc"])).any()
        if not covered:
            status.append("no plan loaded"); dt_id.append(None); rel.append(None)
            continue
        cand = p[(p["obs_start_utc"] <= r["roi_exit_utc"] + tol) & (p["obs_stop_utc"] >= r["roi_entry_utc"] - tol)]
        cand = cand[cand.geometry.notna()]
        cand = cand[cand.geometry.intersects(roi) | cand.geometry.intersects(roi_alt)]
        if cand.empty:
            status.append("not planned"); dt_id.append(None); rel.append(None)
            continue
        nominal = cand[cand["plan_category"].fillna("").str.upper() == "NOMINAL"]
        best = (nominal if len(nominal) else cand).iloc[0]
        status.append(f"planned ({best['plan_category']})")
        dt_id.append(best["datatake_id"])
        rel.append(int(best["orbit_relative"]) if pd.notna(best["orbit_relative"]) else None)

    out["esa_plan_status"] = status
    out["esa_datatake_id"] = dt_id
    out["esa_relative_orbit"] = pd.array(rel, dtype="Int64")
    counts = out.loc[out["mission"] == "Sentinel-2", "esa_plan_status"].value_counts()
    print("🗓️  ESA plan cross-check: " + (", ".join(f"{k}: {v}" for k, v in counts.items()) or "no Sentinel-2 passes"))
    return out

# ====================================================================================================
# LIVE TRACKING
# ====================================================================================================

def satellite_positions(tles: dict, when_utc=None, satellites: Optional[Iterable] = None) -> pd.DataFrame:
    """Instantaneous sub-satellite position, altitude and ground speed for each satellite."""
    t = _to_utc_timestamp(when_utc) if when_utc is not None else pd.Timestamp.now(tz="UTC")
    names = _resolve_satellites(satellites) if satellites else list(tles)
    rows = []
    for name in names:
        sat = _build_satellite(name, tles[name])
        lat, lon, alt = _propagate(sat, t, np.array([0.0, 1.0]))
        _, _, d = GEOD.inv(lon[0], lat[0], lon[1], lat[1])
        rows.append({"satellite": name, "lat": float(lat[0]), "lon": float(lon[0]),
                     "alt_km": float(alt[0]), "ground_speed_kms": d / 1000.0,
                     "direction": "Ascending" if lat[1] > lat[0] else "Descending"})
    return pd.DataFrame(rows)


def _split_antimeridian(lats, lons) -> list:
    """Split a lat/lon sequence into [[lat, lon], ...] segments wherever it crosses ±180°."""
    segs, cur = [], []
    for i, (la, lo) in enumerate(zip(lats, lons)):
        if cur and abs(lo - lons[i - 1]) > 180:
            segs.append(cur)
            cur = []
        cur.append([float(la), float(lo)])
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s) > 1]


def _night_polygon(when_utc, step_deg: float = 2.0) -> list:
    """[[lat, lon], ...] ring of the night hemisphere for a Leaflet overlay."""
    dec, slon = subsolar_point(when_utc)
    dec = dec if abs(dec) > 1e-3 else 1e-3
    lons = np.arange(-180.0, 180.0 + step_deg, step_deg)
    lat_t = np.rad2deg(np.arctan(-np.cos(np.deg2rad(lons - slon)) / np.tan(np.deg2rad(dec))))
    pole = -90.0 if dec > 0 else 90.0
    return [[pole, -180.0]] + [[float(a), float(b)] for a, b in zip(lat_t, lons)] + [[pole, 180.0]]


class LiveTracker:

    """
    Jupyter live tracker (ipyleaflet) — an n2yo-style view focused on an ROI.

    Shows each satellite's current position, past (dashed) and future (solid)
    ground track, its current instrument swath, the day/night terminator and the
    ROI, plus a live panel with UTC and ROI-local clocks, positions and a countdown
    to each satellite's next predicted ROI overpass. Updates run as an asyncio task
    on the kernel's event loop, so the notebook stays usable while it runs.

    Use ``tracker = live_tracker(...)`` then ``tracker.stop()`` / ``tracker.start()``.
    """

    def __init__(self, tles, roi_gdf, passes=None, satellites=None, refresh_s=2.0,
                 track_minutes=(45, 90), timezone_name=None, zoom=3, height="620px"):
        import ipyleaflet as L
        import ipywidgets as W

        self.L, self.W = L, W
        self.tles = tles
        self.names = _resolve_satellites(satellites) if satellites else list(tles)
        self.sats = {n: _build_satellite(n, tles[n]) for n in self.names}
        self.roi = roi_gdf.geometry.iloc[0]
        self.passes = passes
        self.refresh_s = refresh_s
        self.past_min, self.future_min = track_minutes
        self.tz_name = timezone_name or resolve_roi_timezone(roi_gdf, verbose=False)
        self.tz = ZoneInfo(self.tz_name)
        self._task, self._running, self._last_track = None, False, None

        a = _roi_anchor(self.roi)
        self.map = L.Map(center=(a.y, a.x), zoom=zoom, basemap=L.basemaps.Esri.WorldImagery,
                         scroll_wheel_zoom=True, layout=W.Layout(height=height))
        self.night = L.Polygon(locations=_night_polygon(pd.Timestamp.now(tz="UTC")),
                               color="#000", weight=0, fill_color="#000", fill_opacity=0.35)
        self.map.add(self.night)
        self.map.add(L.GeoJSON(data=json.loads(gpd.GeoSeries([self.roi]).to_json()),
                               style={"color": "#FF3B30", "weight": 2, "fillOpacity": 0.15}, name="ROI"))

        self.layers = {}
        for n in self.names:
            c = SATELLITE_CATALOGUE[n]["colour"]
            past = L.Polyline(locations=[], color=c, weight=1, opacity=0.6, dash_array="4,6", fill=False)
            fut = L.Polyline(locations=[], color=c, weight=2, opacity=0.9, fill=False)
            swath = L.Polygon(locations=[], color=c, weight=1, fill_color=c, fill_opacity=0.25)
            dot = L.CircleMarker(location=(0, 0), radius=7, color="#fff", weight=2, fill_color=c, fill_opacity=1)
            dot.popup = W.HTML(f"<b>{n}</b>")
            for lyr in (past, fut, swath, dot):
                self.map.add(lyr)
            self.layers[n] = {"past": past, "future": fut, "swath": swath, "dot": dot}

        self.panel = W.HTML()
        self.btn = W.ToggleButton(value=True, description="Live", icon="satellite",
                                  tooltip="Pause / resume live updates")
        self.btn.observe(lambda ch: self.start() if ch["new"] else self.stop(), "value")
        self.widget = W.VBox([W.HBox([self.btn]), self.map, self.panel])
        self.update(force_tracks=True)

    # ---------------------------------------------------------------------------------------
    def _ground_track(self, name, now):
        off = np.arange(-self.past_min * 60, self.future_min * 60 + 1, 30.0)
        lat, lon, _ = _propagate(self.sats[name], now, off)
        k = int(np.searchsorted(off, 0))
        return (_split_antimeridian(lat[:k + 1], lon[:k + 1]),
                _split_antimeridian(lat[k:], lon[k:]))

    def _current_swath(self, name, now, minutes=2.5):
        off = np.arange(-minutes * 60, minutes * 60 + 1, 10.0)
        lat, lon, _ = _propagate(self.sats[name], now, off)
        az, _, _ = GEOD.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
        az = np.append(az, az[-1])
        half = SATELLITE_CATALOGUE[name]["swath_km"] * 500.0
        llon, llat, _ = GEOD.fwd(lon, lat, az - 90, np.full_like(lon, half))
        rlon, rlat, _ = GEOD.fwd(lon, lat, az + 90, np.full_like(lon, half))
        ring_lon = np.concatenate([llon, rlon[::-1]])
        if ring_lon.max() - ring_lon.min() > 180:        # skip drawing across the antimeridian
            return []
        return [[float(a), float(b)] for a, b in zip(np.concatenate([llat, rlat[::-1]]), ring_lon)]

    def update(self, force_tracks=False):
        now = pd.Timestamp.now(tz="UTC")
        pos = satellite_positions(self.tles, now, self.names).set_index("satellite")
        redo = force_tracks or self._last_track is None or (now - self._last_track).total_seconds() > 60
        for n, lyr in self.layers.items():
            p = pos.loc[n]
            lyr["dot"].location = (p["lat"], p["lon"])
            lyr["swath"].locations = self._current_swath(n, now)
            if redo:
                past, fut = self._ground_track(n, now)
                lyr["past"].locations, lyr["future"].locations = past, fut
        if redo:
            self.night.locations = _night_polygon(now)
            self._last_track = now
        self.panel.value = self._panel_html(now, pos)

    def _panel_html(self, now, pos):
        loc = now.tz_convert(self.tz)
        nxt = (next_overpasses(self.passes, now).set_index("Satellite")
               if self.passes is not None and len(self.passes) else pd.DataFrame())
        rows = []
        for n, p in pos.iterrows():
            c = SATELLITE_CATALOGUE[n]["colour"]
            cd = (f"{nxt.loc[n, 'Countdown']} · {nxt.loc[n, 'Local']}" if n in nxt.index else "—")
            rows.append(
                f"<tr><td><span style='color:{c}'>●</span> {n}</td>"
                f"<td>{p['lat']:+7.2f}</td><td>{p['lon']:+8.2f}</td><td>{p['alt_km']:.0f}</td>"
                f"<td>{'↑' if p['direction'] == 'Ascending' else '↓'}</td><td>{cd}</td></tr>")
        return (
            "<div style='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px'>"
            f"<b>UTC</b> {now:%Y-%m-%d %H:%M:%S} &nbsp;|&nbsp; <b>ROI local</b> "
            f"{loc:%Y-%m-%d %H:%M:%S} {loc.tzname()} ({self.tz_name})"
            "<table style='border-collapse:collapse;margin-top:6px' cellpadding='3'>"
            "<tr style='text-align:left;border-bottom:1px solid #999'><th>Satellite</th><th>Lat</th>"
            "<th>Lon</th><th>Alt km</th><th></th><th>Next ROI overpass (countdown · local)</th></tr>"
            + "".join(rows) + "</table></div>")

    # ---------------------------------------------------------------------------------------
    async def _loop(self):
        while self._running:
            try:
                self.update()
            except Exception as exc:          # keep the loop alive; surface the error in the panel
                self.panel.value += f"<pre style='color:#c00'>{exc}</pre>"
            await asyncio.sleep(self.refresh_s)

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.ensure_future(self._loop())
        if not self.btn.value:
            self.btn.value = True

    def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.btn.value:
            self.btn.value = False

    def _ipython_display_(self):
        from IPython.display import display
        display(self.widget)


def live_tracker(tles, roi_gdf, passes=None, satellites=None, refresh_s: float = 2.0,
                 track_minutes=(45, 90), timezone_name=None, zoom: int = 3,
                 height: str = "620px", autostart: bool = True) -> LiveTracker:

    """
    Create (and by default start) the ipyleaflet live tracker in a notebook cell.

    Args:
        tles (dict): Output of ``fetch_tles``.
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        passes (gpd.GeoDataFrame, optional): Output of ``predict_overpasses`` — enables
            the next-overpass countdown column.
        satellites (Iterable, optional): Subset to display.
        refresh_s (float, optional): Position refresh interval. Defaults to 2 s.
        track_minutes (tuple, optional): (past, future) ground-track span. Defaults to (45, 90).
        timezone_name (str, optional): IANA zone for the local clock. Defaults to ROI zone.
        zoom (int, optional): Initial zoom. Defaults to 3.
        height (str, optional): Map height. Defaults to "620px".
        autostart (bool, optional): Start updating immediately. Defaults to True.

    Returns:
        LiveTracker: Display it (last line of a cell) and call ``.stop()`` when done.
    """

    tr = LiveTracker(tles, roi_gdf, passes=passes, satellites=satellites, refresh_s=refresh_s,
                     track_minutes=track_minutes, timezone_name=timezone_name, zoom=zoom, height=height)
    if autostart:
        tr.start()
    return tr


def export_live_tracker_html(
    tles: dict,
    roi_gdf: gpd.GeoDataFrame,
    out_path: Union[str, Path] = Path("OUTPUT") / "live_tracker.html",
    passes: Optional[gpd.GeoDataFrame] = None,
    timezone_name: Optional[str] = None,
    satellites: Optional[Iterable] = None,
    title: str = "Overpass Tracker",
    standalone: bool = True,
) -> Path:

    """
    Write a standalone, self-updating live-tracker web page (no Python needed to view it).

    The page embeds the TLEs, ROI and predicted overpasses, and propagates the
    orbits in the browser with satellite.js (SGP4), so it keeps tracking in real
    time on any device — useful outside Jupyter, on GitHub Pages, or on a wall
    screen. It shows positions, ground tracks, live swaths, the terminator,
    UTC/ROI-local clocks and an overpass countdown list.

    The embedded TLEs age: re-export after refreshing TLEs (the page shows TLE age).

    Args:
        tles (dict): Output of ``fetch_tles``.
        roi_gdf (gpd.GeoDataFrame): ROI from ``load_roi``.
        out_path (str | Path, optional): Output HTML path. Defaults to ``OUTPUT/live_tracker.html``.
        passes (gpd.GeoDataFrame, optional): Output of ``predict_overpasses`` for the countdown list.
        timezone_name (str, optional): IANA zone. Defaults to the ROI zone.
        satellites (Iterable, optional): Subset to include.
        title (str, optional): Page title. Defaults to "Overpass Tracker".
        standalone (bool, optional): Wrap in a full ``<html>`` document. Defaults to True.

    Returns:
        Path: Path of the written HTML file.
    """

    names = _resolve_satellites(satellites) if satellites else list(tles)
    tz_name = timezone_name or resolve_roi_timezone(roi_gdf, verbose=False)
    sats = [{"name": n, "line1": tles[n]["line1"], "line2": tles[n]["line2"],
             "colour": SATELLITE_CATALOGUE[n]["colour"], "swath_km": SATELLITE_CATALOGUE[n]["swath_km"],
             "mission": SATELLITE_CATALOGUE[n]["mission"],
             "epoch": tles[n]["epoch_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")} for n in names]
    plist = []
    if passes is not None and len(passes):
        for _, r in passes[passes["satellite"].isin(names)].iterrows():
            plist.append({"sat": r["satellite"], "in": r["roi_entry_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "ca": r["overpass_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "out": r["roi_exit_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "cov": float(r["roi_coverage_pct"]), "dir": r["direction"],
                          "exp": bool(r["imaging_expected"]),
                          "plan": r.get("esa_plan_status") if "esa_plan_status" in r else None})
    payload = {
        "title": title, "tz": tz_name,
        "roi_name": str(roi_gdf["name"].iloc[0]) if "name" in roi_gdf else "ROI",
        "roi": json.loads(gpd.GeoSeries([roi_gdf.geometry.iloc[0]]).to_json()),
        "sats": sats, "passes": plist,
        "generated": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    here = Path(__file__).parent
    template = (here / "live_tracker_template.html").read_text(encoding="utf-8")
    land = (here / "world_land_50m.json").read_text(encoding="utf-8")
    safe = lambda s: s.replace("</", "<\\/")              # keep embedded JSON from closing the <script>
    html = (template.replace("__TITLE__", title)
            .replace("/*__PAYLOAD__*/null", safe(json.dumps(payload)))
            .replace("/*__LAND__*/null", land))
    if standalone:
        html = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                + html + "\n</html>\n")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"🌐 Live tracker written to {out_path} — open it in any browser.")
    return out_path
