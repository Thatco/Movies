"""
Export movie titles from a Letterboxd `ratings.csv` export into a JSON
array, for consumption by Search-MoviesOnComputer.ps1.

Using JSON here (instead of joining/splitting a single string) means we
never have to worry about apostrophes, commas, or quotes inside a movie
title breaking the parser on the PowerShell side.
"""

import csv
import json
import sys
from pathlib import Path

import easygui

# Update this each time you download a fresh export from Letterboxd,
# or just let the file picker below handle it.
DEFAULT_CSV_PATH = Path(
    r"C:\Users\Amphy\Downloads\letterboxd-thatco-2026-09-22-02-28-utc\ratings.csv"
)

OUTPUT_PATH = Path(__file__).with_name("MovieList.json")


def find_csv_path() -> Path:
    """Use the default export location if it exists, otherwise prompt."""
    if DEFAULT_CSV_PATH.exists():
        return DEFAULT_CSV_PATH

    chosen = easygui.fileopenbox(title="Select Letterboxd ratings.csv")
    if not chosen:
        sys.exit("No file selected. Exiting.")
    return Path(chosen)


def load_movie_titles(csv_path: Path) -> list[str]:
    """Read the 'Name' column out of a standard Letterboxd ratings.csv."""
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "Name" not in reader.fieldnames:
            sys.exit(
                "Couldn't find a 'Name' column in the CSV. "
                f"Columns found: {reader.fieldnames}"
            )
        return [row["Name"].strip() for row in reader if row.get("Name")]


def main() -> None:
    csv_path = find_csv_path()
    titles = load_movie_titles(csv_path)

    OUTPUT_PATH.write_text(
        json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(titles)} movie titles to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
