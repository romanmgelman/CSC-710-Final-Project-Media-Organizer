# geo.py
# GPS extraction and reverse-geocoding for mo.
#
# This module is independent of mo.py — it only knows about file paths
# and returns plain Python values. mo.py imports from here, never the
# other way around. You can also run this file directly to test against
# a single media file:
#
#     python3 geo.py path/to/photo.jpg

import multiprocessing

# ─── Optional imports ───────────────────────────────────────────────
# Same graceful-degradation pattern mo.py uses for exif/hachoir:
# if a library is missing, the relevant code path just returns None
# instead of crashing.

try:
    from exif import Image as ExifImage
    HAS_EXIF = True
except ImportError:
    HAS_EXIF = False

try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    import hachoir.core.config as hachoir_config
    hachoir_config.quiet = True
    HAS_HACHOIR = True
except ImportError:
    HAS_HACHOIR = False

try:
    import reverse_geocoder as rg
    HAS_GEOCODER = True
except ImportError:
    HAS_GEOCODER = False


# ─── Warmup (main process only) ─────────────────────────────────────
# reverse_geocoder lazy-loads its ~150k-row CSV on first call, which
# costs 1-2 seconds and prints "Loading formatted geocoded file..." to
# stdout. We pre-trigger it here so the first real lookup is fast and
# silent.
#
# The guard `multiprocessing.current_process().name == "MainProcess"`
# is critical on Python 3.13 + macOS, where multiprocessing uses spawn
# semantics. spawn re-imports this module inside each worker process,
# and we don't want every worker to redo the warmup (which would itself
# spawn workers, causing infinite recursion / the "before bootstrapping
# phase" RuntimeError).

if HAS_GEOCODER and multiprocessing.current_process().name == "MainProcess":
    import io
    import sys
    _stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        # mode=2 forces single-process mode — no worker pool, no spawn,
        # no recursion risk. Single lookup is plenty fast.
        rg.search([(0.0, 0.0)], mode=2)
    except Exception:
        pass
    finally:
        sys.stdout = _stdout


# ─── Country code → name mapping ────────────────────────────────────
# reverse_geocoder returns ISO-3166 alpha-2 codes ("US", "JP", etc).
# We map a handful to friendly names so folder names look readable.
# If a code isn't here, we fall back to the raw code so unknown
# countries still get sorted into *some* folder rather than disappearing.

COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "JP": "Japan",
    "FR": "France",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "CA": "Canada",
    "AU": "Australia",
    "BR": "Brazil",
    "IN": "India",
    "TH": "Thailand",
    "ID": "Indonesia",
    "MX": "Mexico",
    "CN": "China",
    "KR": "South Korea",
    "NL": "Netherlands",
    "SE": "Sweden",
    "NO": "Norway",
    "CH": "Switzerland",
    "GR": "Greece",
    "PT": "Portugal",
    "IE": "Ireland",
    "NZ": "New Zealand",
    "ZA": "South Africa",
    "AR": "Argentina",
    "CL": "Chile",
    "PE": "Peru",
    "EG": "Egypt",
    "TR": "Turkey",
    "AE": "United Arab Emirates",
    "SG": "Singapore",
    "MY": "Malaysia",
    "PH": "Philippines",
    "VN": "Vietnam",
}


# ─── Helpers ────────────────────────────────────────────────────────

def _dms_to_decimal(dms, ref):
    """
    Convert (degrees, minutes, seconds) + reference char to signed decimal.

    EXIF stores GPS as three numbers + a hemisphere reference. The exif
    library hands us the three numbers as floats already, so we just need
    the standard formula and a sign flip for southern/western hemisphere.

    Example:
      ((40.0, 42.0, 46.08), 'N') -> 40.7128
      ((74.0, 0.0,  21.6),  'W') -> -74.006
    """
    degrees, minutes, seconds = dms
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


# ─── Extractors ─────────────────────────────────────────────────────

def get_gps_from_exif(path):
    """
    Pull (lat, lng) from a photo's EXIF GPS tags. Returns None if absent.

    Most photos with EXIF won't actually have GPS — phone photos usually
    do, dedicated cameras usually don't unless they have built-in GPS or
    were paired with a phone.
    """
    if not HAS_EXIF:
        return None
    try:
        with open(path, "rb") as f:
            img = ExifImage(f)
        if not img.has_exif:
            return None
        # all four tags must be present for a usable coordinate
        attrs = dir(img)
        needed = ("gps_latitude", "gps_latitude_ref",
                  "gps_longitude", "gps_longitude_ref")
        if not all(a in attrs for a in needed):
            return None
        lat = _dms_to_decimal(img.gps_latitude, img.gps_latitude_ref)
        lng = _dms_to_decimal(img.gps_longitude, img.gps_longitude_ref)
        return (lat, lng)
    except Exception:
        # corrupt EXIF, weird format, locked file — anything goes wrong,
        # we just say "no GPS" and let the caller try the next source.
        return None


def get_gps_from_hachoir(path):
    """
    Pull (lat, lng) from a video's metadata. Most video formats don't
    store GPS at all, but iPhone .mov files sometimes do. Returns None
    when there's nothing usable.
    """
    if not HAS_HACHOIR:
        return None
    try:
        parser = createParser(str(path))
        if not parser:
            return None
        with parser:
            metadata = extractMetadata(parser)
            if not metadata:
                return None
            if metadata.has("latitude") and metadata.has("longitude"):
                lat = float(metadata.get("latitude"))
                lng = float(metadata.get("longitude"))
                return (lat, lng)
    except Exception:
        pass
    return None


def get_file_gps(path):
    """
    Try each GPS source in order. Returns ((lat, lng), source_tag) on
    success, or (None, "none") if no source has coordinates.

    Mirrors mo.py's get_file_date pattern. There's deliberately no
    mtime-style fallback — file systems don't store GPS, so either the
    metadata has it or it doesn't.
    """
    coords = get_gps_from_exif(path)
    if coords:
        return coords, "exif"
    coords = get_gps_from_hachoir(path)
    if coords:
        return coords, "hachoir"
    return None, "none"


# ─── Reverse geocoding ──────────────────────────────────────────────

def get_location_name(lat, lng):
    """
    Turn coordinates into (country_name, city_name).

    Uses reverse_geocoder, an offline dataset of ~150k cities. It picks
    the nearest one, which might be 30km away if you're in the middle
    of nowhere — fine for sorting purposes.

    Returns (None, None) if reverse_geocoder isn't installed or the
    lookup fails for any reason.
    """
    if not HAS_GEOCODER:
        return None, None
    try:
        # mode=2 disables multiprocessing, which is essential because
        # we're often called from a Tkinter background thread, and
        # macOS + Python 3.13 spawn semantics make worker pools fragile
        # in that context.
        results = rg.search([(lat, lng)], mode=2)
        if not results:
            return None, None
        result = results[0]
        cc = result.get("cc", "")
        city = result.get("name", "") or None
        country = COUNTRY_NAMES.get(cc, cc) if cc else None
        return country, city
    except Exception:
        return None, None


# ─── Standalone test ────────────────────────────────────────────────
# Run "python3 geo.py path/to/photo.jpg" to test against a single file
# without launching the GUI. Useful for sanity-checking after you
# generate fake data with genpics.py.

if __name__ == "__main__":
    import sys
    from pathlib import Path

    print(f"status: exif={HAS_EXIF}, hachoir={HAS_HACHOIR}, "
          f"geocoder={HAS_GEOCODER}")

    if len(sys.argv) < 2:
        print("\nusage: python3 geo.py <path-to-media-file>")
        sys.exit(0)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"file not found: {path}")
        sys.exit(1)

    coords, tag = get_file_gps(path)
    if coords is None:
        print(f"{path.name}: no GPS data found")
    else:
        lat, lng = coords
        print(f"{path.name}: {lat:.6f}, {lng:.6f}  (source: {tag})")
        country, city = get_location_name(lat, lng)
        if country:
            print(f"  → {city}, {country}")
        else:
            print("  → reverse geocoding unavailable or failed")