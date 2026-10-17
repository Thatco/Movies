"""
links.py

Your manual decisions: "this file IS this movie" (a *confirmed* link).

Why a separate file: scanner.py wipes and rebuilds the `locations` table on
every scan, so anything you decide by hand has to live somewhere a scan
never touches. That is the `confirmed_links` table here. (It lives in
movies.db, so don't delete movies.db without a backup: it now holds work
you did by hand.)

Tables created here (safe to run repeatedly):
  confirmed_links  one row per confirmed file: which movie, the file's NAME
                   and SIZE (so it is still recognised if you move it to
                   another folder or drive), and the last path it was seen at.
  disk_files       every video file Everything found on the last scan, whether
                   or not any movie claimed it. The app's "Unmatched files"
                   tab is built from this.

  deconfirmed_links  the opposite decision: "this file is NOT this movie" (a
                   featurette, a TV episode, something unrelated that the
                   search happened to match). Same shape as confirmed_links.
                   A file is either confirmed or deconfirmed for a movie, never
                   both: setting one clears the other.

A file is identified by (filename, size), not by its full path. That means
the same file copied to two drives counts as confirmed in both places.

This file also adds two columns to `movies` for the director lookup
(directors.py): `directors` and `directors_checked`.

Everything that WRITES for the UI goes through this file, so app.py can keep
its main database connection read-only.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from everything_search import match_confidence

SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmed_links (
    id           INTEGER PRIMARY KEY,
    movie_id     INTEGER NOT NULL REFERENCES movies(id),
    filename     TEXT NOT NULL,
    size_bytes   INTEGER,
    path         TEXT,                                  -- last path it was seen at
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One confirmation per (movie, filename, size). NOCASE because Windows
-- filenames ignore case; IFNULL because SQLite treats every NULL as
-- different from every other NULL (the same trick database.py uses).
CREATE UNIQUE INDEX IF NOT EXISTS idx_confirmed_identity
    ON confirmed_links (movie_id, filename COLLATE NOCASE, IFNULL(size_bytes, -1));

-- Same shape and same identity rule as confirmed_links.
CREATE TABLE IF NOT EXISTS deconfirmed_links (
    id              INTEGER PRIMARY KEY,
    movie_id        INTEGER NOT NULL REFERENCES movies(id),
    filename        TEXT NOT NULL,
    size_bytes      INTEGER,
    path            TEXT,
    deconfirmed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deconfirmed_identity
    ON deconfirmed_links (movie_id, filename COLLATE NOCASE, IFNULL(size_bytes, -1));

CREATE TABLE IF NOT EXISTS disk_files (
    path       TEXT PRIMARY KEY,
    place_id   INTEGER NOT NULL REFERENCES places(id),
    size_bytes INTEGER
);
"""

# Confidence levels that auto-confirm is allowed to work on.
AUTO_CONFIRM_LEVELS = ("strong", "basic")

# Columns added to `movies` after the first version of the database (the
# same migration idea as database.py's ensure_columns). directors_checked
# records that a movie's Letterboxd page was read successfully, so the
# lookup can be stopped and resumed without repeating itself.
MOVIE_COLUMNS = {"directors": "TEXT", "directors_checked": "TEXT"}


def ensure_tables(conn):
    """Create the tables above, and the extra movie columns, if missing."""
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(movies)")}
    for name, column_type in MOVIE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {name} {column_type}")


def ensure_database(db_path):
    """Open movies.db just long enough to make sure the tables exist."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        ensure_tables(conn)
        conn.commit()
    finally:
        conn.close()


def run_write(db_path, action):
    """Run `action(conn)` on a short-lived write connection and save it.

    Everything the action does is saved together, or not at all. The
    connection waits up to 30 seconds if a scan is saving at that moment.
    Returns whatever `action` returns.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_tables(conn)
        result = action(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def filename_of(path):
    """The file's own name, e.g. 'Heat (1995) 1080p.mkv'."""
    return PureWindowsPath(path).name


def _delete_decision(conn, table, movie_id, path, size):
    """Delete one file's row from confirmed_links or deconfirmed_links."""
    cursor = conn.execute(
        f"DELETE FROM {table} "
        "WHERE movie_id = ? AND filename = ? COLLATE NOCASE "
        "AND IFNULL(size_bytes, -1) = IFNULL(?, -1)",
        (movie_id, filename_of(path), size),
    )
    return cursor.rowcount


def confirm_files(conn, items):
    """Confirm files. `items` is a list of (movie_id, path, size_bytes).

    Returns how many were newly confirmed (already-confirmed ones are skipped).
    Confirming a file you had deconfirmed simply replaces that decision.
    """
    added = 0
    for movie_id, path, size in items:
        _delete_decision(conn, "deconfirmed_links", movie_id, path, size)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO confirmed_links (movie_id, filename, size_bytes, path) "
            "VALUES (?, ?, ?, ?)",
            (movie_id, filename_of(path), size, path),
        )
        added += cursor.rowcount
    # A file linked from the Unmatched tab has no `locations` row for its
    # movie yet (no search ever found it for that movie), so add one.
    restore_confirmed_locations(conn)
    return added


def unconfirm_files(conn, items):
    """Remove confirmations. `items` is a list of (movie_id, path, size_bytes).

    The file simply goes back to whatever confidence the scan gave it.
    Returns how many confirmations were removed.
    """
    removed = 0
    for movie_id, path, size in items:
        removed += _delete_decision(conn, "confirmed_links", movie_id, path, size)
    return removed


def deconfirm_files(conn, items):
    """Mark files as "not this movie". `items` is (movie_id, path, size_bytes).

    Any confirmation of the same file is removed. The file stays in the
    database (a rescan will find it again) but is hidden unless the
    Deconfirmed box is ticked, and it no longer counts as a match for that
    movie. Returns how many were newly deconfirmed.
    """
    added = 0
    for movie_id, path, size in items:
        _delete_decision(conn, "confirmed_links", movie_id, path, size)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO deconfirmed_links (movie_id, filename, size_bytes, path) "
            "VALUES (?, ?, ?, ?)",
            (movie_id, filename_of(path), size, path),
        )
        added += cursor.rowcount
    return added


def undeconfirm_files(conn, items):
    """Take back a deconfirmation: the file returns to its scanned confidence.

    Returns how many deconfirmations were removed.
    """
    removed = 0
    for movie_id, path, size in items:
        removed += _delete_decision(conn, "deconfirmed_links", movie_id, path, size)
    return removed


def restore_confirmed_locations(conn):
    """Make sure every confirmed file has a `locations` row for its movie.

    Finds each confirmed file again among the files on disk (by name and
    size, wherever it now lives) and adds a `locations` row if that movie
    doesn't already have one for it. This is what makes a confirmation
    survive a rescan, a moved file, or a title the scanner could never
    search for. Returns how many rows were added.
    """
    confirmed = conn.execute(
        "SELECT c.movie_id, c.filename, c.size_bytes, m.year "
        "FROM confirmed_links c JOIN movies m ON m.id = c.movie_id"
    ).fetchall()
    if not confirmed:
        return 0

    on_disk = defaultdict(list)  # (lowercase filename, size) -> [(path, place_id)]
    for path, place_id, size in conn.execute("SELECT path, place_id, size_bytes FROM disk_files"):
        on_disk[(filename_of(path).casefold(), size)].append((path, place_id))

    existing = {
        (movie_id, path.casefold())
        for movie_id, path in conn.execute("SELECT movie_id, path FROM locations")
    }

    added = 0
    for movie_id, filename, size, year in confirmed:
        for path, place_id in on_disk.get((filename.casefold(), size), []):
            if (movie_id, path.casefold()) in existing:
                continue
            conn.execute(
                "INSERT INTO locations (movie_id, place_id, path, size_bytes, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (movie_id, place_id, path, size, match_confidence(path, year)),
            )
            existing.add((movie_id, path.casefold()))
            added += 1
    return added


@dataclass
class AutoConfirmPlan:
    """What an auto-confirm would do, worked out before anything is saved."""
    items: list = field(default_factory=list)  # (movie_id, path, size_bytes) to confirm
    skipped_several: int = 0  # movie has more than one candidate at this level
    skipped_shared: int = 0   # the file is also a strong/basic candidate for another movie
    skipped_done: int = 0     # movie already has a confirmed file


def plan_auto_confirm(conn, results, level):
    """Work out which files "Confirm all Strong/Basic" would confirm.

    `results` is the list of MovieEntry objects currently on screen. A movie
    is confirmed automatically only when it is unambiguous:
      * it has exactly one (neither confirmed nor deconfirmed) file at
        `level` in the current list,
      * it has no confirmed file yet, and
      * that file isn't also a strong/basic candidate for a different movie
        (the classic case is a sequel whose title shares a first half with
        the original).
    Anything else is counted and left for you to review by hand.
    """
    already = {row[0] for row in conn.execute("SELECT DISTINCT movie_id FROM confirmed_links")}

    # Which movies each strong/basic file is a candidate for, not counting
    # pairs you have deconfirmed (a file you rejected for one movie no longer
    # makes it ambiguous for another).
    rejected = {
        (movie_id, name.casefold(), size)
        for movie_id, name, size in conn.execute(
            "SELECT movie_id, filename, size_bytes FROM deconfirmed_links"
        )
    }
    claimants = defaultdict(set)
    for movie_id, path, size in conn.execute(
        "SELECT movie_id, path, size_bytes FROM locations WHERE confidence IN ('strong', 'basic')"
    ):
        if (movie_id, filename_of(path).casefold(), size) not in rejected:
            claimants[path.casefold()].add(movie_id)
    shared = {path for path, movie_ids in claimants.items() if len(movie_ids) > 1}

    plan = AutoConfirmPlan()
    for movie in results:
        candidates = [
            f for f in movie.files
            if f.confidence == level and not f.confirmed and not f.deconfirmed
        ]
        if not candidates:
            continue
        if movie.movie_id in already:
            plan.skipped_done += 1
        elif len(candidates) > 1:
            plan.skipped_several += 1
        elif candidates[0].path.casefold() in shared:
            plan.skipped_shared += 1
        else:
            plan.items.append((movie.movie_id, candidates[0].path, candidates[0].size_bytes))
    return plan