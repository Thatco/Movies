"""
generate_markdown.py

Builds the Markdown report from movies.db instead of from a live search.
Every line comes from database queries.

Usage (from the MovieSearch folder):
    python generate_markdown.py            # all movies (default)
    python generate_markdown.py results    # only movies with files
    python generate_markdown.py noresults  # only movies with no local files
    python generate_markdown.py nowhere    # not on your drives or any friend's server

Each movie's heading uses its BEST confidence level. With
HIDE_LOWER_CONFIDENCE on, only the files at that best level are listed, so
a movie with a strong match no longer drags along hundreds of low-confidence
false positives. Movies whose best match is low still list their low files,
flagged for manual checking.

Remote (Plex) availability comes from the remote_availability table, filled
by remote_scanner.py. If that has never been run, the remote lines are
simply left out.

Not shown yet: the "matched via cleaned title" note (not stored).
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"

# Most files listed under one movie (same idea as MaxResultsShown = 50).
MAX_RESULTS_SHOWN = 50

# True: list only the files at each movie's best confidence level.
# False: list every stored file for the movie.
HIDE_LOWER_CONFIDENCE = True

# Higher number = more trustworthy match. Used to find each movie's best level.
RANK = {"strong": 3, "basic": 2, "low": 1}
EMOJI = {"strong": "✅", "basic": "🟡", "low": "⚠️"}
DESCRIPTION = {
    "strong": "_Confident match — title, year & resolution all present_",
    "basic": "_Confident match — title and year are present_",
    "low": "_Low confidence — verify manually_",
}

# mode name -> (output file name, label used in the report title)
MODES = {
    "both": ("AllMovies_fromdb.md", "All Movies"),
    "results": ("MoviesWithResults_fromdb.md", "Movies With Results"),
    "noresults": ("MoviesWithNoResults_fromdb.md", "Movies With No Results"),
    "nowhere": ("NotFoundAnywhere_fromdb.md", "Movies Not Found Anywhere"),
}


def format_size(size_bytes):
    """Turn a byte count into a short human-friendly string."""
    if size_bytes is None:
        return "size unknown"
    gb = size_bytes / 1024**3
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size_bytes / 1024**2:.0f} MB"


def format_server_names(names, conjunction="and"):
    """Join server names into a natural phrase with possessives.

    ["A"]            -> "A's"
    ["A", "B"]       -> "A's and B's"
    ["A", "B", "C"]  -> "A's, B's, and C's"
    Pass conjunction="or" for phrasing like "not on A's, B's, or C's server".
    """
    possessives = [f"{name}'s" for name in names]
    if not possessives:
        return ""
    if len(possessives) == 1:
        return possessives[0]
    if len(possessives) == 2:
        return f"{possessives[0]} {conjunction} {possessives[1]}"
    return f"{', '.join(possessives[:-1])}, {conjunction} {possessives[-1]}"


def build_report(conn, mode):
    """Return the whole report as a list of lines."""
    movies = conn.execute("SELECT id, title, year FROM movies ORDER BY id").fetchall()

    # Load every file once and group by movie, instead of running one
    # query per movie. defaultdict(list) creates an empty list on first use.
    files_by_movie = defaultdict(list)
    for movie_id, path, size, confidence in conn.execute(
        "SELECT movie_id, path, size_bytes, confidence FROM locations ORDER BY path"
    ):
        files_by_movie[movie_id].append((path, size, confidence))

    # Same idea for remote availability: movie id -> [server names].
    servers_by_movie = defaultdict(list)
    for movie_id, server_name in conn.execute(
        """
        SELECT r.movie_id, p.name FROM remote_availability r
        JOIN places p ON p.id = r.place_id ORDER BY p.name
        """
    ):
        servers_by_movie[movie_id].append(server_name)

    # Every remote server the database knows about (for "not on A, B, or C").
    remote_places = [
        name for (name,) in conn.execute(
            "SELECT name FROM places WHERE kind = 'remote' ORDER BY name"
        )
    ]

    body = []
    found_count = 0
    only_remote_count = 0

    for movie_id, title, _year in movies:
        files = files_by_movie.get(movie_id, [])
        servers = servers_by_movie.get(movie_id, [])

        if files:
            found_count += 1
            if mode in ("noresults", "nowhere"):
                continue

            # Best confidence = the highest-ranked level among its files.
            best = max((f[2] for f in files), key=lambda level: RANK[level])
            shown = [f for f in files if f[2] == best] if HIDE_LOWER_CONFIDENCE else files
            hidden_count = len(files) - len(shown)

            word = "result" if len(shown) == 1 else "results"
            body.append(f"### {EMOJI[best]} '{title}' — {len(shown)} {word}")
            body.append(DESCRIPTION[best])
            if servers:
                body.append(f"_Also available on {format_server_names(servers)} Plex server_")
            if hidden_count:
                noun = "match" if hidden_count == 1 else "matches"
                body.append(f"_({hidden_count} lower-confidence {noun} hidden)_")
            body.append("")

            for path, size, confidence in shown[:MAX_RESULTS_SHOWN]:
                # Bold paths are confident matches (same as the old report).
                text = f"`{path}` — {format_size(size)}"
                body.append(f"- **{text}**" if confidence != "low" else f"- {text}")
            if len(shown) > MAX_RESULTS_SHOWN:
                body.append(f"- _...and {len(shown) - MAX_RESULTS_SHOWN} more_")
        else:
            if servers:
                only_remote_count += 1
            if mode == "results":
                continue
            if mode == "nowhere" and servers:
                continue  # it IS available somewhere, so not for this report

            body.append(f"### ❌ '{title}'")
            if mode == "nowhere":
                body.append(
                    "_Not found on your computer, and not available on "
                    f"{format_server_names(remote_places, 'or')} Plex server._"
                )
            else:
                body.append("_Did not return any results._")
                if servers:
                    body.append(f"_Available on {format_server_names(servers)} Plex server_")

        body.append("")
        body.append("---")
        body.append("")

    # The header needs the final counts, so it's built last and put first.
    header = [
        f"# Movie Search Results — {MODES[mode][1]}",
        "",
        f"_{found_count} of {len(movies)} movies found locally._",
    ]
    if remote_places:
        not_local = len(movies) - found_count
        header.append(
            f"_Of the {not_local} not found locally, {only_remote_count} are available "
            f"on {format_server_names(remote_places)} Plex server._"
        )
    header += ["", "---", ""]
    return header + body


def main():
    parser = argparse.ArgumentParser(description="Write a Markdown report from movies.db.")
    parser.add_argument("mode", nargs="?", default="both", choices=MODES.keys())
    args = parser.parse_args()

    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py and scanner.py first.")

    conn = sqlite3.connect(DB_PATH)
    try:
        lines = build_report(conn, args.mode)
    except sqlite3.OperationalError as error:
        sys.exit(f"Database problem: {error}. Did you add remote_availability to database.py and re-run it?")
    finally:
        conn.close()

    output_path = HERE / MODES[args.mode][0]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")


main()