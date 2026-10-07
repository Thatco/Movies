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
    rating      REAL,
    UNIQUE (title, year)
);
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