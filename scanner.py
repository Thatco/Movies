"""
scanner.py

Searches Everything for every movie in movies.db and records what it finds:

  * Each file found becomes a row in the `locations` table.
  * Each distinct drive letter or network share becomes a row in `places`,
    worked out automatically from the file's path:
        D:\\Movies\\Heat.mkv           -> place "D:"            (local)
        \\\\NAS\\Share\\Heat.mkv         -> place "\\\\NAS\\Share"   (network)

Run order: database.py (creates and fills the movie list) first, then this.
Requirements: Everything must be running, and ES_PATH in
everything_search.py must point at es.exe.

Every run is a FULL RESCAN: old locations are cleared and rebuilt, so files
you have deleted or moved stop showing up. All changes are saved in one
step at the very end, so if the scan fails midway the previous results
stay untouched.
"""

import sqlite3
import sys
from pathlib import Path, PureWindowsPath

from everything_search import find_movie_files, match_confidence

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"

# Print a progress line every this-many movies.
PROGRESS_EVERY = 25


def place_for_path(path):
    """Work out which drive or network share a file path lives on.

    Returns (name, kind), or None if the path has no drive part.
    PureWindowsPath understands Windows path rules without touching the
    disk. Its .drive is "D:" for normal paths and "\\\\server\\share" for
    network (UNC) paths.

    Limitation: a *mapped* network drive (say Z: pointing at the NAS) looks
    like a normal local drive letter here. We can add a manual override in
    the config later if that matters for you.
    """
    drive = PureWindowsPath(path).drive
    if not drive:
        return None
    if drive.startswith("\\\\"):
        return drive, "network"
    return drive.upper(), "local"  # normalize "d:" to "D:"


def get_place_id(conn, cache, name, kind):
    """Return the id of a place, creating the row the first time it's seen.

    `cache` is a plain dict remembering ids we've already looked up, so we
    don't query the database for every single file.
    """
    if name not in cache:
        # INSERT OR IGNORE does nothing if the place already exists
        # (the UNIQUE rule on places.name), so this is safe to repeat.
        conn.execute("INSERT OR IGNORE INTO places (name, kind) VALUES (?, ?)", (name, kind))
        cache[name] = conn.execute("SELECT id FROM places WHERE name = ?", (name,)).fetchone()[0]
    return cache[name]


def scan(conn):
    """Search for every movie and store the results (not yet committed)."""
    movies = conn.execute("SELECT id, title, year FROM movies ORDER BY id").fetchall()
    total = len(movies)
    place_cache = {}

    # Start from a clean slate. Places are kept; they're just drives.
    conn.execute("DELETE FROM locations")

    for number, (movie_id, title, year) in enumerate(movies, start=1):
        # find_movie_files returns (list of (path, size), matched_title).
        files, _matched_title = find_movie_files(title)

        for path, size in files:
            place = place_for_path(path)
            if place is None:
                print(f"  Skipping path with no drive: {path}")
                continue
            place_id = get_place_id(conn, place_cache, *place)
            conn.execute(
                "INSERT INTO locations (movie_id, place_id, path, size_bytes, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (movie_id, place_id, path, size, match_confidence(path, year)),
            )

        if number % PROGRESS_EVERY == 0 or number == total:
            print(f"Scanned {number} of {total} movies...")


def print_summary(conn):
    """Show what the scan found, using the same kind of queries the UI will."""
    total = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    found = conn.execute("SELECT COUNT(DISTINCT movie_id) FROM locations").fetchone()[0]
    print(f"\n{found} of {total} movies have at least one file; {total - found} have none.")

    # Each movie's BEST confidence across all of its files. We turn the
    # text levels into numbers (strong=3, basic=2, low=1), take the MAX
    # per movie, then count how many movies landed on each level.
    rows = conn.execute(
        """
        SELECT best, COUNT(*) FROM (
            SELECT movie_id,
                   MAX(CASE confidence WHEN 'strong' THEN 3
                                       WHEN 'basic'  THEN 2
                                       ELSE 1 END) AS best
            FROM locations GROUP BY movie_id
        ) GROUP BY best ORDER BY best DESC
        """
    ).fetchall()
    names = {3: "strong", 2: "basic", 1: "low"}
    print("Best confidence per found movie:")
    for best, count in rows:
        print(f"  {names[best]:>6}: {count}")

    # Files and total size per drive/share (the WizTree-style numbers).
    print("\nFiles and total size per place:")
    rows = conn.execute(
        """
        SELECT p.name, p.kind, COUNT(*) AS files,
               COALESCE(SUM(l.size_bytes), 0) AS total_bytes
        FROM locations l JOIN places p ON p.id = l.place_id
        GROUP BY p.id ORDER BY total_bytes DESC
        """
    ).fetchall()
    for name, kind, files, total_bytes in rows:
        print(f"  {name:<24} {kind:<8} {files:>5} files  {total_bytes / 1024**3:>9.1f} GB")


def main():
    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py first.")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        scan(conn)
        conn.commit()  # save everything in one step
    except Exception:
        conn.rollback()  # undo the partial scan; old results stay intact
        conn.close()
        raise

    print_summary(conn)
    conn.close()


main()
