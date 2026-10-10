"""
database.py

Creates movies.db (if needed) and loads the movie list from Movielist.json.
Safe to run repeatedly: tables are only created if missing, and movies that
are already in the database are skipped rather than duplicated.

Run order for a full refresh:
  1. (when your Letterboxd list changes) regenerate Movielist.json
  2. database.py        <- this file: loads the movie list
  3. scanner.py         searches your drives (Everything must be running)
  4. remote_scanner.py  checks the friends' Plex servers
  5. app.py             (or generate_markdown.py) to view the results
"""

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"
JSON_PATH = HERE / "Movielist.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    year        INTEGER,
    source      TEXT,
    date_logged TEXT,
    rating      REAL
);

-- One row per distinct (title, year). This is a separate index instead of
-- UNIQUE(title, year) on the table because SQLite treats every NULL as
-- different from every other NULL, so movies WITHOUT a year slipped past
-- that rule and were re-added on every run. IFNULL(year, 0) makes a missing
-- year count as 0, so "same title, no year" is recognized as a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_title_year
    ON movies (title, IFNULL(year, 0));

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


def main():
    with open(JSON_PATH, encoding="utf-8-sig") as f:
        movies = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)

    conn.executemany(
        "INSERT OR IGNORE INTO movies (title, year) VALUES (?, ?)",
        [(m["Title"], int(m["Year"]) if m.get("Year") else None) for m in movies],
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    print(f"{count} movies in database")
    conn.close()


main()