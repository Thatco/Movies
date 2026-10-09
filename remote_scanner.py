"""
remote_scanner.py

Stores which movies are available on each friend's Plex server.

Flow:
  1. plex_remote.get_remote_movies() fetches every server's movie list
     (live, or from cache when a server is unreachable).
  2. Each server becomes a row in `places` with kind = 'remote'.
  3. Each of YOUR movies is matched against those lists, and every match
     becomes a row in `remote_availability` (movie_id, place_id).

Matching rule: the titles must be identical after punctuation is removed
and capitals are ignored ("Spider-Man: Homecoming" == "spider man
homecoming"), AND the release years must be within YEAR_TOLERANCE of each
other (sites often disagree by a year). If either side has no year, the
title alone decides, which is how the PowerShell version always worked.

Run this separately from scanner.py: refreshing remote availability
shouldn't require rescanning your drives, and vice versa.

Prerequisite: the remote_availability table must exist. Add it to SCHEMA
in database.py and re-run database.py (safe to repeat).
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from everything_search import get_search_friendly_title
from plex_remote import get_remote_movies

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"

# How many years apart two releases may be and still count as the same movie.
YEAR_TOLERANCE = 1


def normalize(title):
    """Comparison form of a title: no punctuation, lower case."""
    return get_search_friendly_title(title).casefold()


def years_compatible(year_a, year_b):
    """True if the years are close enough, or if either one is unknown."""
    if year_a is None or year_b is None:
        return True
    return abs(year_a - year_b) <= YEAR_TOLERANCE


def build_index(remote_movies):
    """Make a lookup: normalized title -> [(server, title, year), ...].

    Looking titles up in a dict is far faster than comparing every one of
    your movies against every remote movie.
    """
    index = defaultdict(list)
    for server_name, movies in remote_movies.items():
        for movie in movies:
            index[normalize(movie["title"])].append(
                (server_name, movie["title"], movie["year"])
            )
    return index


def print_summary(conn):
    """Show what the match found, using the same kinds of queries the UI will."""
    print("\nMovies available per remote server:")
    for name, count in conn.execute(
        """
        SELECT p.name, COUNT(*) FROM remote_availability r
        JOIN places p ON p.id = r.place_id
        GROUP BY p.id ORDER BY p.name
        """
    ):
        print(f"  {name}: {count}")

    # EXISTS asks "is there at least one matching row?" without joining.
    only_remote = conn.execute(
        """
        SELECT COUNT(*) FROM movies m
        WHERE NOT EXISTS (SELECT 1 FROM locations l WHERE l.movie_id = m.id)
          AND EXISTS (SELECT 1 FROM remote_availability r WHERE r.movie_id = m.id)
        """
    ).fetchone()[0]
    nowhere = conn.execute(
        """
        SELECT COUNT(*) FROM movies m
        WHERE NOT EXISTS (SELECT 1 FROM locations l WHERE l.movie_id = m.id)
          AND NOT EXISTS (SELECT 1 FROM remote_availability r WHERE r.movie_id = m.id)
        """
    ).fetchone()[0]
    print(f"\nNot on your drives but on a friend's server: {only_remote}")
    print(f"Not found anywhere: {nowhere}")


def main():
    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py and scanner.py first.")

    # Do the network part BEFORE touching the database, so a failure here
    # can't leave anything half-updated.
    remote_movies = get_remote_movies()
    if not remote_movies:
        sys.exit("No remote server data available (live or cached). Nothing updated.")
    index = build_index(remote_movies)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # One 'remote' place per server (INSERT OR IGNORE = safe to repeat).
        place_ids = {}
        for server_name in remote_movies:
            conn.execute(
                "INSERT OR IGNORE INTO places (name, kind) VALUES (?, 'remote')",
                (server_name,),
            )
            place_ids[server_name] = conn.execute(
                "SELECT id FROM places WHERE name = ?", (server_name,)
            ).fetchone()[0]

        # Clear old matches only for servers we have fresh data for, so a
        # server that was skipped this run keeps its previous results.
        for place_id in place_ids.values():
            conn.execute("DELETE FROM remote_availability WHERE place_id = ?", (place_id,))

        movies = conn.execute("SELECT id, title, year FROM movies").fetchall()
        for movie_id, title, year in movies:
            for server_name, remote_title, remote_year in index.get(normalize(title), []):
                if years_compatible(year, remote_year):
                    # OR IGNORE: if a server lists the same movie twice,
                    # keep just the first (movie_id + place_id is unique).
                    conn.execute(
                        "INSERT OR IGNORE INTO remote_availability "
                        "(movie_id, place_id, remote_title, remote_year) VALUES (?, ?, ?, ?)",
                        (movie_id, place_ids[server_name], remote_title, remote_year),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    print_summary(conn)
    conn.close()


main()
