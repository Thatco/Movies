"""
scanner.py

Searches Everything for every movie in movies.db and records what it finds:

  * Each file found becomes a row in the `locations` table.
  * Each distinct drive letter or network share becomes a row in `places`,
    worked out automatically from the file's path:
        D:\\Movies\\Heat.mkv           -> place "D:"            (local)
        \\\\NAS\\Share\\Heat.mkv         -> place "\\\\NAS\\Share"   (network)
  * Every video file on the drives (matched to a movie or not) is saved in
    the `disk_files` table; the app's "Unmatched files" tab uses it.
  * Files you confirmed by hand (links.py) are kept across scans: they are
    found again by name and size and put back in `locations`.

Used two ways:
  * python scanner.py          from the command line (prints progress)
  * run_scan(...)              called by the app's "Rescan Drives" button

Requirements: Everything must be running, and ES_PATH in
everything_search.py must point at es.exe.

How it works, in two phases:
  1. SEARCH  - es.exe is run for every movie, several at once (see
     SEARCH_WORKERS), then once more to list every video file. Nothing is
     written to the database yet.
  2. SAVE    - old locations are cleared and the new ones written in one
     quick all-or-nothing step.
So a failed or cancelled search leaves the previous results untouched, and
the database is only locked for a moment, not for the whole scan.
"""

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath

import links
from everything_search import find_movie_files, list_all_video_files, match_confidence

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"

# How many es.exe searches run at the same time. Each search is a separate
# little process, so running several at once is much faster than one by one.
# If you ever see odd es.exe errors, set this to 1 to go back to one-by-one.
SEARCH_WORKERS = 8

# Print a progress line every this-many movies (command-line use only).
PROGRESS_EVERY = 25


class ScanCancelled(Exception):
    """Raised when the user cancels a scan; nothing has been changed."""


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


def search_all(titles, progress, cancel_event):
    """Phase 1: search Everything for every title (several at once).

    Returns a list of file lists, in the same order as `titles`.
    """
    total = len(titles)
    pool = ThreadPoolExecutor(max_workers=SEARCH_WORKERS)
    try:
        futures = [pool.submit(find_movie_files, title) for title in titles]
        found = []
        # Collect results in order. .result() re-raises any error a search
        # hit (for example Everything not running), which stops the scan.
        for number, future in enumerate(futures, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            files, _matched_title = future.result()
            found.append(files)
            if progress:
                progress(number, total, "Searching drives")
        return found
    finally:
        # Drop any searches still queued if we're leaving early.
        pool.shutdown(wait=True, cancel_futures=True)


def save_results(conn, movies, found, all_files):
    """Phase 2: replace the stored locations and file list with the new results."""
    place_cache = {}
    conn.execute("DELETE FROM locations")  # places are kept; they're just drives
    conn.execute("DELETE FROM disk_files")
    for (movie_id, _title, year), files in zip(movies, found):
        for path, size in files:
            place = place_for_path(path)
            if place is None:
                continue
            place_id = get_place_id(conn, place_cache, *place)
            conn.execute(
                "INSERT INTO locations (movie_id, place_id, path, size_bytes, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (movie_id, place_id, path, size, match_confidence(path, year)),
            )

    # Every video file Everything knows about, claimed by a movie or not.
    for path, size in all_files:
        place = place_for_path(path)
        if place is None:
            continue
        place_id = get_place_id(conn, place_cache, *place)
        conn.execute(
            "INSERT OR IGNORE INTO disk_files (path, place_id, size_bytes) VALUES (?, ?, ?)",
            (path, place_id, size),
        )

    # Put back the files you confirmed by hand (found again by name + size).
    links.restore_confirmed_locations(conn)


def run_scan(db_path=DB_PATH, progress=None, cancel_event=None):
    """Do a full rescan. Returns how many seconds it took.

    progress     optional function(done, total, message) for progress updates
    cancel_event optional threading.Event; set it to cancel (raises
                 ScanCancelled, and the database is left untouched)
    """
    started = time.time()

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        links.ensure_tables(conn)  # no-op once they exist
        movies = conn.execute("SELECT id, title, year FROM movies ORDER BY id").fetchall()
        found = search_all([title for _, title, _ in movies], progress, cancel_event)

        # One more search, with no title: every video file on the drives.
        if progress:
            progress(0, 1, "Listing all video files")
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        all_files = list_all_video_files()

        try:
            save_results(conn, movies, found, all_files)
            conn.commit()  # save everything in one step
        except Exception:
            conn.rollback()  # undo a partial save; old results stay intact
            raise
    finally:
        conn.close()

    return time.time() - started


def print_summary(conn):
    """Show what the scan found, using the same kind of queries the UI will."""
    total = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    found = conn.execute("SELECT COUNT(DISTINCT movie_id) FROM locations").fetchone()[0]
    print(f"\n{found} of {total} movies have at least one file; {total - found} have none.")
    confirmed = conn.execute("SELECT COUNT(DISTINCT movie_id) FROM confirmed_links").fetchone()[0]
    listed = conn.execute("SELECT COUNT(*) FROM disk_files").fetchone()[0]
    print(f"{confirmed} movies have a confirmed file; {listed} video files on disk in total.")

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

    def print_progress(done, total, _message):
        if done % PROGRESS_EVERY == 0 or done == total:
            print(f"Scanned {done} of {total} movies...")

    seconds = run_scan(progress=print_progress)

    conn = sqlite3.connect(DB_PATH)
    print_summary(conn)
    conn.close()
    print(f"\nScan took {seconds:.0f} seconds.")


if __name__ == "__main__":
    main()