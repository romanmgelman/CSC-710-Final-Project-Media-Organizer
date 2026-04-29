# genpics: test data generator for mo
#
# Spits out a bunch of fake photo/video files with real, readable
# date metadata so you can exercise the organizer.
#
# Photos get proper EXIF DateTimeOriginal written in, plus optional
# GPS coordinates from a list of real-world cities.
# Videos get their file mtime set (hachoir usually falls back to that
# anyway on simple synthetic files, and mo's mtime fallback catches them).
#
# needs: pip install Pillow piexif

import argparse
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import piexif


# file types we'll randomly pick from.
# heic/raw formats are left out because Pillow can't write them without
# extra wheels that are a pain to install. mo still handles them fine
# in the wild, this is just the generator being realistic.
PHOTO_EXTS = [".jpg", ".jpeg", ".png", ".tiff", ".webp", ".bmp"]
VIDEO_EXTS = [".mov", ".mp4", ".avi", ".mkv"]

# some photos get no metadata at all so we can test the skipped bucket
NO_META_CHANCE = 0.08

# a handful of plausible camera strings for flavor
FAKE_CAMERAS = [
    ("Canon", "EOS R5"),
    ("Sony", "ILCE-7M4"),
    ("Nikon", "Z 6II"),
    ("FUJIFILM", "X-T5"),
    ("Apple", "iPhone 15 Pro"),
    ("Google", "Pixel 8"),
    ("Panasonic", "DC-GH6"),
]

# real-world coordinates for a spread of countries — gives the
# reverse-geocoder something interesting to chew on. Mix of major
# cities + a few less-obvious places so the output has variety.
FAKE_LOCATIONS = [
    ("New York, USA",        40.7128,  -74.0060),
    ("San Francisco, USA",   37.7749, -122.4194),
    ("Los Angeles, USA",     34.0522, -118.2437),
    ("London, UK",           51.5074,   -0.1278),
    ("Paris, France",        48.8566,    2.3522),
    ("Berlin, Germany",      52.5200,   13.4050),
    ("Rome, Italy",          41.9028,   12.4964),
    ("Barcelona, Spain",     41.3851,    2.1734),
    ("Tokyo, Japan",         35.6762,  139.6503),
    ("Kyoto, Japan",         35.0116,  135.7681),
    ("Bangkok, Thailand",    13.7563,  100.5018),
    ("Bali, Indonesia",      -8.3405,  115.0920),
    ("Sydney, Australia",   -33.8688,  151.2093),
    ("Toronto, Canada",      43.6532,  -79.3832),
    ("Mexico City, Mexico",  19.4326,  -99.1332),
    ("Rio de Janeiro, BR",  -22.9068,  -43.1729),
    ("Cape Town, ZA",       -33.9249,   18.4241),
    ("Mumbai, India",        19.0760,   72.8777),
    ("Singapore",             1.3521,  103.8198),
    ("Seoul, South Korea",   37.5665,  126.9780),
]


def random_date_between(start, end):
    """Uniform random datetime between two bounds."""
    delta = end - start
    seconds = random.randint(0, int(delta.total_seconds()))
    return start + timedelta(seconds=seconds)


def decimal_to_dms_rational(decimal):
    """
    Convert a signed decimal coordinate into the EXIF GPS format:
    a tuple of three rationals ((deg_num, deg_den), (min_num, min_den),
    (sec_num, sec_den)).

    EXIF stores GPS as unsigned magnitudes — the hemisphere ('N'/'S' or
    'E'/'W') goes in a separate ref tag. So we strip the sign here and
    let the caller stamp the ref.

    Example: 40.7128 -> ((40,1), (42,1), (4608,100))
    """
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes_full = (decimal - degrees) * 60
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60
    # store seconds with 2 decimal places of precision (×100 numerator)
    return (
        (degrees, 1),
        (minutes, 1),
        (int(round(seconds * 100)), 100),
    )


def make_photo(path, size, dt, write_metadata, gps_coords=None):
    """
    Draw a solid-color image with a date label, optionally stamp EXIF.

    gps_coords: optional (label, lat, lng) tuple. If provided and the
    format supports EXIF, GPS tags get written too.
    """
    w, h = size
    # pick a random-ish pastel so they're visually distinguishable
    color = (random.randint(80, 230),
             random.randint(80, 230),
             random.randint(80, 230))
    img = Image.new("RGB", (w, h), color)
    draw = ImageDraw.Draw(img)
    label = dt.strftime("%Y-%m-%d %H:%M:%S")
    # fall back to the default bitmap font so this works without a font file
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except (OSError, IOError):
        font = ImageFont.load_default()

    draw.text((10, 10), label, fill=(20, 20, 20), font=font)
    draw.text((10, h - 30), path.name, fill=(20, 20, 20), font=font)
    # also draw the location name if we have one — makes it easy to
    # eyeball whether sorting worked correctly without opening EXIF
    if gps_coords is not None:
        loc_label, _, _ = gps_coords
        draw.text((10, 40), loc_label, fill=(20, 20, 20), font=font)

    ext = path.suffix.lower()

    # EXIF only really makes sense on jpeg/tiff/webp. for png/bmp we skip it
    # and mo will just fall back to mtime, which we set below anyway.
    exif_bytes = None
    if write_metadata and ext in (".jpg", ".jpeg", ".tiff", ".webp"):
        make, model = random.choice(FAKE_CAMERAS)
        exif_str = dt.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict = {
            "0th": {
                piexif.ImageIFD.Make: make.encode(),
                piexif.ImageIFD.Model: model.encode(),
                piexif.ImageIFD.DateTime: exif_str.encode(),
            },
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: exif_str.encode(),
                piexif.ExifIFD.DateTimeDigitized: exif_str.encode(),
            },
        }
        # add GPS section if coordinates were provided
        if gps_coords is not None:
            _, lat, lng = gps_coords
            lat_ref = b'N' if lat >= 0 else b'S'
            lng_ref = b'E' if lng >= 0 else b'W'
            exif_dict["GPS"] = {
                piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
                piexif.GPSIFD.GPSLatitudeRef: lat_ref,
                piexif.GPSIFD.GPSLatitude: decimal_to_dms_rational(lat),
                piexif.GPSIFD.GPSLongitudeRef: lng_ref,
                piexif.GPSIFD.GPSLongitude: decimal_to_dms_rational(lng),
            }
        try:
            exif_bytes = piexif.dump(exif_dict)
        except Exception:
            exif_bytes = None

    # Pillow wants specific format strings for some extensions
    save_format = {
        ".jpg": "JPEG", ".jpeg": "JPEG",
        ".png": "PNG", ".tiff": "TIFF", ".tif": "TIFF",
        ".webp": "WEBP", ".bmp": "BMP",
    }[ext]
    save_kwargs = {}
    if exif_bytes and save_format in ("JPEG", "TIFF", "WEBP"):
        save_kwargs["exif"] = exif_bytes
    img.save(path, format=save_format, **save_kwargs)

    # always set mtime so mo's last-resort fallback still sees something sensible
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def make_video(path, dt):
    """
    Write a tiny placeholder file with the right extension and set its mtime.
    Real video muxing is out of scope; mo's hachoir pass will usually fail
    on these and fall through to mtime, which is exactly what we want to test.
    """
    # a few bytes of junk so it's not zero-length
    path.write_bytes(os.urandom(random.randint(1024, 8192)))
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def main():
    parser = argparse.ArgumentParser(
        description="Generate fake photos/videos with date metadata for mo."
    )
    parser.add_argument("output", type=Path,
                        help="Folder to dump generated files into.")
    parser.add_argument("-n", "--count", type=int, default=50,
                        help="How many files to generate (default 50).")
    parser.add_argument("--start-year", type=int, default=2020,
                        help="Earliest year for random dates.")
    parser.add_argument("--end-year", type=int, default=2026,
                        help="Latest year for random dates.")
    parser.add_argument("--videos", type=float, default=0.15,
                        help="Fraction of files that should be videos (0-1).")
    parser.add_argument("--gps-coverage", type=float, default=0.7,
                        help="Fraction of EXIF-bearing photos that get GPS "
                             "(0-1, default 0.7). Reflects the real-world "
                             "mix where most phone photos have GPS but some "
                             "don't.")
    parser.add_argument("--subdirs", type=int, default=0,
                        help="If >0, scatter files into this many subfolders.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible runs.")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    args.output.mkdir(parents=True, exist_ok=True)

    start = datetime(args.start_year, 1, 1)
    end = datetime(args.end_year, 12, 31, 23, 59, 59)

    # precompute which folder each file goes into (if any)
    if args.subdirs > 0:
        subfolders = [args.output / f"batch_{i+1:02d}"
                      for i in range(args.subdirs)]
        for sf in subfolders:
            sf.mkdir(exist_ok=True)
    else:
        subfolders = [args.output]

    # track counts so duplicate-filename collisions are rare
    counter = 0

    for _ in range(args.count):
        counter += 1
        is_video = random.random() < args.videos
        ext = random.choice(VIDEO_EXTS if is_video else PHOTO_EXTS)
        folder = random.choice(subfolders)
        dt = random_date_between(start, end)

        # occasionally collide dates on purpose to test the rename suffix logic
        if random.random() < 0.1:
            dt = dt.replace(hour=12, minute=0, second=0)

        name = f"IMG_{counter:04d}{ext}"
        path = folder / name

        if is_video:
            make_video(path, dt)
            kind = "video"
        else:
            # randomize size so the output has visual variety and varied file sizes
            w = random.choice([640, 800, 1024, 1280, 1920])
            h = random.choice([480, 600, 768, 720, 1080])
            write_meta = random.random() >= NO_META_CHANCE
            # only write GPS if we're writing metadata at all, AND we
            # roll under the coverage threshold. Photos without metadata
            # never get GPS (you can't write GPS without EXIF).
            gps = None
            if write_meta and random.random() < args.gps_coverage:
                gps = random.choice(FAKE_LOCATIONS)
            make_photo(path, (w, h), dt, write_meta, gps_coords=gps)
            kind = "photo"
            if not write_meta:
                kind += " (no meta)"
            elif gps is None:
                kind += " (no gps)"
            else:
                kind += f" @ {gps[0]}"

        print(f"  {path.relative_to(args.output)}  {dt:%Y-%m-%d %H:%M}  [{kind}]")

    print(f"\nDone. {args.count} files generated in {args.output}")


if __name__ == "__main__":
    main()