"""
database.py

Creates movies.db (if needed), upgrades it to the latest layout, and loads
the film list from MovieList.json (made by movie_list_export.py).
Safe to run repeatedly.

Run order for a full refresh:
  1. movie_list_export.py   when your Letterboxd list changes
  2. database.py            <- this file: loads films and their dated logs
  3. scanner.py             searches your drives (Everything must be running)
  4. remote_scanner.py      checks the friends' Plex servers
  5. app.py                 (or generate_markdown.py) to view the results
(run_all.py runs steps 2-4 for you.)

Tables:
  movies               one row per film (title, year, rating, Letterboxd link,
                       date added to the watchlist)
  watch_logs           one row per dated log, so a rewatched film has several.
                       Rebuilt from the JSON on every run (it is derived data).
  places / locations   where files were found (filled by scanner.py)
  remote_availability  which friend servers have a film (remote_scanner.py)
"""

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"
JSON_PATH = HERE / "MovieList.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    year            INTEGER,
    source          TEXT,
    rating          REAL,
    letterboxd_url  TEXT,
    watchlist_added TEXT
);

-- One row per distinct (title, year). This is a separate index instead of
-- UNIQUE(title, year) on the table because SQLite treats every NULL as
-- different from every other NULL, so films WITHOUT a year slipped past
-- that rule and were re-added on every run. IFNULL(year, 0) makes a missing
-- year count as 0, so "same title, no year" is recognized as a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_title_year
    ON movies (title, IFNULL(year, 0));

CREATE TABLE IF NOT EXISTS watch_logs (
    id           INTEGER PRIMARY KEY,
    movie_id     INTEGER NOT NULL REFERENCES movies(id),
    logged_date  TEXT,                       -- when it was logged on Letterboxd
    watched_date TEXT,                       -- when you say you watched it
    rating       REAL,
    rewatch      INTEGER NOT NULL DEFAULT 0, -- 1 = marked as a rewatch
    source       TEXT                        -- 'diary' or 'rating'
);
CREATE INDEX IF NOT EXISTS idx_watch_logs_movie ON watch_logs (movie_id);

CREATE TABLE IF NOT EXISTS places (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('local', 'network', 'remote'))
);

CREATE TABLE IF NOT EXISTS locations (
    id         INTEGER PRIMARY KEY,
    movie_id   INTEGER NOT NULL REFERENCES movies(id),
    place_id   INTEGER NOT NULL REFERENCES places(id),
    path       TEXT NOT NULL,
    size_bytes INTEGER,
    confidence TEXT CHECK (confidence IN ('low', 'basic', 'strong'))
);

CREATE TABLE IF NOT EXISTS remote_availability (
    movie_id     INTEGER NOT NULL REFERENCES movies(id),
    place_id     INTEGER NOT NULL REFERENCES places(id),
    remote_title TEXT NOT NULL,
    remote_year  INTEGER,
    PRIMARY KEY (movie_id, place_id)
);
"""

# Columns added to `movies` after the first version of the database. CREATE
# TABLE IF NOT EXISTS never changes a table that already exists, so an older
# movies.db gets these through ALTER TABLE (a "migration") instead of being
# deleted and rebuilt. Fresh databases already have them from SCHEMA above.
MOVIE_COLUMNS_ADDED_LATER = {
    "rating": "REAL",
    "letterboxd_url": "TEXT",
    "watchlist_added": "TEXT",
}


def ensure_columns(conn, table, columns):
    """Add any of `columns` ({name: type}) that `table` doesn't have yet."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
            print(f"Upgraded database: added {table}.{name}")


def load_films(conn, films):
    """Insert/update every film and rebuild all of the dated logs."""
    # Logs are derived entirely from the JSON, so wipe and refill them;
    # that also removes logs you deleted on Letterboxd.
    conn.execute("DELETE FROM watch_logs")

    for film in films:
        title = film["Title"]
        year = int(film["Year"]) if film.get("Year") else None

        # Add the film if it's new (the unique index skips existing ones)...
        conn.execute("INSERT OR IGNORE INTO movies (title, year) VALUES (?, ?)", (title, year))
        # ...then look up its id, matching a missing year as 0 like the index does.
        movie_id = conn.execute(
            "SELECT id FROM movies WHERE title = ? AND IFNULL(year, 0) = IFNULL(?, 0)",
            (title, year),
        ).fetchone()[0]

        conn.execute(
            "UPDATE movies SET source = ?, rating = ?, letterboxd_url = ?, watchlist_added = ? "
            "WHERE id = ?",
            (
                ", ".join(film.get("Sources", [])) or None,
                film.get("Rating"),
                film.get("LetterboxdURI") or None,
                film.get("WatchlistDate"),
                movie_id,
            ),
        )

        for log in film.get("Logs", []):
            conn.execute(
                "INSERT INTO watch_logs "
                "(movie_id, logged_date, watched_date, rating, rewatch, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    movie_id,
                    log.get("LoggedDate"),
                    log.get("WatchedDate"),
                    log.get("Rating"),
                    1 if log.get("Rewatch") else 0,
                    log.get("Source"),
                ),
            )


def main():
    with open(JSON_PATH, encoding="utf-8-sig") as f:
        films = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    ensure_columns(conn, "movies", MOVIE_COLUMNS_ADDED_LATER)

    try:
        load_films(conn, films)
        conn.commit()  # all-or-nothing: a failure leaves the old data intact
    except Exception:
        conn.rollback()
        conn.close()
        raise

    movie_count = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    log_count = conn.execute("SELECT COUNT(*) FROM watch_logs").fetchone()[0]
    print(f"{movie_count} movies and {log_count} dated logs in database")
    conn.close()


main()