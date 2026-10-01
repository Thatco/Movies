"""
Export movie entries (title + year) from a Letterboxd `ratings.csv`
export, merged with `OrphanedDiary.csv` and `watchlist.csv` in the same
folder, into a JSON array for consumption by Search-MoviesOnComputer.ps1.

Using JSON here (instead of joining/splitting a single string) means we
never have to worry about apostrophes, commas, or quotes inside a movie
title breaking the parser on the PowerShell side. Year travels alongside
each title now too, for the main script's confidence-matching check (a
result containing both the title and the release year, plus a resolution
tag, is a much stronger signal than title text alone).
"""

import csv
import json
import sys
from pathlib import Path

import easygui

DEFAULT_CSV_PATH = Path(
    r"C:\Users\Amphy\Programming Projects\MovieSearch\Exported from Letterboxd\letterboxd-thatco-2026-10-01-21-47-utc\ratings.csv"
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


def load_movie_entries(csv_path: Path) -> list[dict]:
    """Read the 'Name' and 'Year' columns out of a standard
    Letterboxd-format CSV, as {"Title": ..., "Year": ...} entries."""
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "Name" not in reader.fieldnames:
            sys.exit(
                "Couldn't find a 'Name' column in the CSV. "
                f"Columns found: {reader.fieldnames}"
            )
        return [
            {"Title": row["Name"].strip(), "Year": (row.get("Year") or "").strip()}
            for row in reader
            if row.get("Name")
        ]


def load_diary_entries(ratings_csv_path: Path) -> list[dict]:
    """
    Read entries out of OrphanedDiary.csv, if it exists alongside
    ratings.csv in the same export folder - renamed from Letterboxd's own
    diary.csv to avoid colliding with the one already in a full export.
    Same column layout as ratings.csv, so load_movie_entries handles it
    directly; this just locates the file and tolerates it being absent.
    """
    diary_path = ratings_csv_path.with_name("OrphanedDiary.csv")
    if not diary_path.exists():
        print(f"No OrphanedDiary.csv found next to {ratings_csv_path.name} - skipping it.")
        return []
    return load_movie_entries(diary_path)


def load_watchlist_entries(watchlist_csv_path: Path) -> list[dict]:
    """
    Read entries out of watchlist.csv, if it exists alongside ratings.csv
    in the same export folder. Same column layout as ratings.csv, so
    load_movie_entries handles it directly; this just locates the file
    and tolerates it being absent.
    """
    watchlist_path = watchlist_csv_path.with_name("watchlist.csv")
    if not watchlist_path.exists():
        print(f"No watchlist.csv found next to {watchlist_csv_path.name} - skipping it.")
        return []
    return load_movie_entries(watchlist_path)


def dedupe_preserve_order(entries: list[dict]) -> list[dict]:
    """Drop duplicate titles (e.g. logged in more than one file) while
    keeping first-seen order and that first entry's Year - dicts preserve
    insertion order in Python, so a dict keyed by title is a simple, fast
    way to dedupe without sorting."""
    seen: dict[str, dict] = {}
    for entry in entries:
        if entry["Title"] not in seen:
            seen[entry["Title"]] = entry
    return list(seen.values())


def main() -> None:
    csv_path = find_csv_path()
    entries = load_movie_entries(csv_path)
    entries += load_diary_entries(csv_path)
    entries += load_watchlist_entries(csv_path)
    entries = dedupe_preserve_order(entries)

    OUTPUT_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(entries)} movie entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()