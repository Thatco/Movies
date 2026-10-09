"""
generate_markdown.py

Builds the Markdown report from movies.db instead of from a live search.
This is the "does the foundation work?" step: the report looks like the
PowerShell script's output, but every line comes from database queries.

Usage (from the MovieSearch folder):
    python generate_markdown.py            # all movies (default)
    python generate_markdown.py results    # only movies with files
    python generate_markdown.py noresults  # only movies with no files

Each movie's heading uses its BEST confidence level. With
HIDE_LOWER_CONFIDENCE on, only the files at that best level are listed, so
a movie with a strong match no longer drags along hundreds of low-confidence
false positives. Movies whose best match is low still list their low files,
flagged for manual checking.

Not shown yet (this information isn't in the database): the "Also available
on Plex" line and the "matched via cleaned title" note.
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
}


def format_size(size_bytes):
    """Turn a byte count into a short human-friendly string."""
    if size_bytes is None:
        return "size unknown"
    gb = size_bytes / 1024**3
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size_bytes / 1024**2:.0f} MB"


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

    body = []
    found_count = 0

    for movie_id, title, _year in movies:
        files = files_by_movie.get(movie_id, [])

        if files:
            found_count += 1
            if mode == "noresults":
                continue

            # Best confidence = the highest-ranked level among its files.
            best = max((f[2] for f in files), key=lambda level: RANK[level])
            shown = [f for f in files if f[2] == best] if HIDE_LOWER_CONFIDENCE else files
            hidden_count = len(files) - len(shown)

            word = "result" if len(shown) == 1 else "results"
            body.append(f"### {EMOJI[best]} '{title}' — {len(shown)} {word}")
            body.append(DESCRIPTION[best])
            if hidden_count:
                body.append(f"_({hidden_count} lower-confidence matches hidden)_")
            body.append("")

            for path, size, confidence in shown[:MAX_RESULTS_SHOWN]:
                # Bold paths are confident matches (same as the old report).
                text = f"`{path}` — {format_size(size)}"
                body.append(f"- **{text}**" if confidence != "low" else f"- {text}")
            if len(shown) > MAX_RESULTS_SHOWN:
                body.append(f"- _...and {len(shown) - MAX_RESULTS_SHOWN} more_")
        else:
            if mode == "results":
                continue
            body.append(f"### ❌ '{title}'")
            body.append("_Did not return any results._")

        body.append("")
        body.append("---")
        body.append("")

    # The header needs the final counts, so it's built last and put first.
    label = MODES[mode][1]
    header = [
        f"# Movie Search Results — {label}",
        "",
        f"_{found_count} of {len(movies)} movies found locally._",
        "",
        "---",
        "",
    ]
    return header + body


def main():
    parser = argparse.ArgumentParser(description="Write a Markdown report from movies.db.")
    parser.add_argument("mode", nargs="?", default="both", choices=MODES.keys())
    args = parser.parse_args()

    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py and scanner.py first.")

    conn = sqlite3.connect(DB_PATH)
    lines = build_report(conn, args.mode)
    conn.close()

    output_path = HERE / MODES[args.mode][0]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")


main()