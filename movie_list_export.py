"""
movie_list_export.py

Turns a Letterboxd export into MovieList.json: ONE entry per film, carrying
everything the database needs, including EVERY dated log of that film
(rewatches included).

Files read from the export folder (the folder holding ratings.csv):
  ratings.csv        one row per rated film
  diary.csv          one row per diary entry, so a rewatched film has several
  OrphanedDiary.csv  your hand-made diary-format file for entries Letterboxd
                     has no film page for (no Letterboxd URI)
  watchlist.csv      films on your watchlist
Any file except ratings.csv may be missing; it is skipped with a message.

A "film" is identified by (title, year), the same rule the database uses, so
two different films that share a title but not a year (Halloween 1978 and
2018) stay separate.

Each JSON entry looks like:
  {
    "Title": "The Cabin in the Woods", "Year": "2011",
    "LetterboxdURI": "https://boxd.it/bhQR5",   (empty for orphaned entries)
    "Rating": 4.0,                              (null if never rated)
    "WatchlistDate": null,                      (date added to watchlist)
    "Sources": ["ratings", "diary"],
    "Logs": [ {"LoggedDate": "2016-09-16", "WatchedDate": "2016-09-16",
               "Rating": 4.0, "Rewatch": true, "Source": "diary"} ]
  }
"Title" and "Year" keep their old names, so Search-MoviesOnComputer.ps1
still reads this file unchanged.

Dates: "LoggedDate" is when you logged the film on Letterboxd; "WatchedDate"
is when you say you watched it. A rated film with no diary entry gets a
single log made from its rating date (Source "rating") so it still has a date.
"""

import csv
import json
import sys
from pathlib import Path

# Update this each time you download a fresh export from Letterboxd,
# or just let the file picker below handle it.
DEFAULT_CSV_PATH = Path(
    r"C:\Users\Amphy\Programming Projects\MovieSearch\Exported from Letterboxd\letterboxd-thatco-2026-10-11-06-42-utc\ratings.csv"
)

OUTPUT_PATH = Path(__file__).with_name("MovieList.json")


def find_csv_path() -> Path:
    """Use the default export location if it exists, otherwise prompt."""
    if DEFAULT_CSV_PATH.exists():
        return DEFAULT_CSV_PATH

    # Imported here so the script still runs where easygui isn't installed,
    # as long as the default path above is valid.
    import easygui

    chosen = easygui.fileopenbox(title="Select Letterboxd ratings.csv")
    if not chosen:
        sys.exit("No file selected. Exiting.")
    return Path(chosen)


def read_rows(csv_path: Path) -> list[dict]:
    """Read a Letterboxd-format CSV into a list of row dicts.

    Rows with no Name are dropped. utf-8-sig quietly handles the invisible
    marker (BOM) some programs put at the start of a CSV file.
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "Name" not in reader.fieldnames:
            sys.exit(
                f"Couldn't find a 'Name' column in {csv_path.name}. "
                f"Columns found: {reader.fieldnames}"
            )
        return [row for row in reader if clean(row.get("Name"))]


def clean(value) -> str:
    """Strip whitespace; treat a missing value as an empty string."""
    return (value or "").strip()


def parse_rating(text: str):
    """'3.5' -> 3.5, '' (unrated) -> None."""
    try:
        return float(text)
    except ValueError:
        return None


def get_film(films: dict, row: dict) -> dict:
    """Find this row's film in `films`, creating it the first time it's seen.

    Also remembers the Letterboxd URI if this row has one and we don't yet.
    """
    title, year = clean(row["Name"]), clean(row.get("Year"))
    key = (title, year)
    if key not in films:
        films[key] = {
            "Title": title,
            "Year": year,
            "LetterboxdURI": "",
            "Rating": None,
            "WatchlistDate": None,
            "Sources": [],
            "Logs": [],
            "_RatingDate": None,  # internal; removed before saving
        }
    film = films[key]
    uri = clean(row.get("Letterboxd URI"))
    if uri and not film["LetterboxdURI"]:
        film["LetterboxdURI"] = uri
    return film


def add_source(film: dict, label: str) -> None:
    if label not in film["Sources"]:
        film["Sources"].append(label)


def add_ratings(films: dict, rows: list[dict]) -> None:
    for row in rows:
        film = get_film(films, row)
        film["Rating"] = parse_rating(clean(row.get("Rating")))
        film["_RatingDate"] = clean(row.get("Date")) or None
        add_source(film, "ratings")


def add_diary(films: dict, rows: list[dict], label: str) -> None:
    """Every diary row becomes one log, so a rewatched film collects several."""
    for row in rows:
        film = get_film(films, row)
        log = {
            "LoggedDate": clean(row.get("Date")) or None,
            "WatchedDate": clean(row.get("Watched Date")) or None,
            "Rating": parse_rating(clean(row.get("Rating"))),
            "Rewatch": clean(row.get("Rewatch")).lower() == "yes",
            "Source": "diary",
        }
        # The same entry can appear in both diary.csv and OrphanedDiary.csv;
        # an identical log is only kept once.
        if log not in film["Logs"]:
            film["Logs"].append(log)
        add_source(film, label)


def add_watchlist(films: dict, rows: list[dict]) -> None:
    for row in rows:
        film = get_film(films, row)
        film["WatchlistDate"] = clean(row.get("Date")) or None
        add_source(film, "watchlist")


def finish_film(film: dict) -> dict:
    """Tidy one film once every file has been read."""
    rating_date = film.pop("_RatingDate")

    # A rated film with no diary entries still gets one dated log.
    if not film["Logs"] and rating_date:
        film["Logs"].append({
            "LoggedDate": rating_date,
            "WatchedDate": None,
            "Rating": film["Rating"],
            "Rewatch": False,
            "Source": "rating",
        })

    # Oldest log first. ISO dates (YYYY-MM-DD) sort correctly as plain text.
    film["Logs"].sort(key=lambda log: (log["LoggedDate"] or "", log["WatchedDate"] or ""))

    # Never rated in ratings.csv? Fall back to the newest rating in the diary.
    if film["Rating"] is None:
        for log in reversed(film["Logs"]):
            if log["Rating"] is not None:
                film["Rating"] = log["Rating"]
                break
    return film


def read_optional(folder: Path, name: str):
    """Rows of an optional CSV in the export folder, or None if absent."""
    path = folder / name
    if not path.exists():
        print(f"No {name} found next to ratings.csv - skipping it.")
        return None
    return read_rows(path)


def main() -> None:
    csv_path = find_csv_path()
    folder = csv_path.parent

    films: dict = {}
    add_ratings(films, read_rows(csv_path))
    rated_only_count = len(films)

    for name, label in (("diary.csv", "diary"), ("OrphanedDiary.csv", "orphaned diary")):
        rows = read_optional(folder, name)
        if rows is not None:
            add_diary(films, rows, label)
    diary_only_count = len(films) - rated_only_count  # films absent from ratings.csv

    rows = read_optional(folder, "watchlist.csv")
    if rows is not None:
        add_watchlist(films, rows)

    entries = [finish_film(film) for film in films.values()]

    OUTPUT_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_logs = sum(len(f["Logs"]) for f in entries)
    rewatched = sum(1 for f in entries if len(f["Logs"]) > 1)
    no_uri = sum(1 for f in entries if not f["LetterboxdURI"])
    print(f"Wrote {len(entries)} films to {OUTPUT_PATH}")
    print(f"  {total_logs} dated logs; {rewatched} films have more than one log (rewatches)")
    print(f"  {diary_only_count} films appear only in a diary file (not in ratings.csv)")
    print(f"  {no_uri} films have no Letterboxd link")


if __name__ == "__main__":
    main()