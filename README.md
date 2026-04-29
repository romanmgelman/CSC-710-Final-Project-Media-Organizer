# CSC-710-Final-Project-Media-Organizer

Organizes photos and videos into `YYYY/MMM` (eg. `2026/Apr`) folders based on whatever date we can pry out of their metadata. Optionally media can be renamed to `MM_DD_YYYY_HHMMSS`. Can also organize by location (country) if files have GPS data, or combine date and location together. Always previews before touching anything.

To run, download the files in this repo.

In a terminal, run `pip install -r requirements.txt` to install the libraries we use.

Then `cd` to where the files are and run `python mo.py`.

## What you can do in the GUI

- Pick a Source folder (messy media) and Destination folder (sorted output)
- Choose Copy (keeps originals) or Move
- Toggle Include subfolders for recursive scans
- Toggle Rename to rewrite filenames as `MM_DD_YYYY.ext`
- Pick a sort mode: Date, Location, or Date + Location
- Click Preview to see what would happen, then Apply to do it
- Click Undo Last Operation to reverse the most recent Apply

## Generating fake test data

Don't want to point it at your real photos? Use `genpics.py`:

```
python genpics.py ~/Desktop/fake_input -n 60 --subdirs 3 --seed 42
```

Makes 60 fake photos and videos with realistic dates and GPS coordinates. Add `--gps-coverage 1.0` to give every photo GPS, useful for testing the location modes.

## Files

- `mo.py` — the app
- `geo.py` — GPS extraction and reverse geocoding
- `genpics.py` — test data generator
- `requirements.txt` — dependencies