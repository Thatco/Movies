"""
queries.py

The data layer for the UI. It knows about the database and the filtering
rules, but nothing about windows or widgets. Keeping it separate means the
rules can be tested on their own, and a different UI could reuse them.

How a movie ends up in the results (this is the heart of the filters):

  * A local/network FILE is visible if its confidence is ticked, its place
    (drive or share) is ticked, and it isn't inside an excluded folder.
  * A movie is shown if it has at least one visible file, OR it is on a
    ticked Plex server, OR "Not found" is ticked and the movie has no
    files at all and isn't on any Plex server.
  * The search box narrows everything by title.
  * The "scope" (All / Watched / Watchlist) decides which films are even
    considered before any of the above applies.
"""

import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

# Display order of place kinds in the sidebar.
KIND_ORDER = {"local": 0, "network": 1, "remote": 2}

# The list scopes shown as tabs: key -> tab label.
#   all       every film in the database
#   watched   films with at least one dated log (diary entry or rating)
#   watchlist films on your Letterboxd watchlist
SCOPES = {"all": "All", "watched": "Watched", "watchlist": "Watchlist"}


@dataclass
class Place:
    id: int
    name: str
    kind: str  # 'local', 'network' or 'remote'


@dataclass
class FileEntry:
    path: str
    size_bytes: int | None
    confidence: str
    place_name: str


@dataclass
class LogEntry:
    """One dated log of a film (a diary entry, or a rating date)."""
    logged_date: str | None
    watched_date: str | None
    rating: float | None
    rewatch: bool
    source: str | None  # 'diary' or 'rating'


@dataclass
class MovieEntry:
    movie_id: int
    title: str
    year: int | None
    files: list = field(default_factory=list)    # visible FileEntry objects
    servers: list = field(default_factory=list)  # visible Plex server names
    letterboxd_url: str | None = None
    logs: list = field(default_factory=list)     # LogEntry objects, oldest first
    first_logged: str | None = None              # earliest log date
    last_logged: str | None = None               # latest log date
    watchlist_added: str | None = None           # date added to the watchlist


@dataclass
class Summary:
    movies: int
    files: int
    total_bytes: int
    per_place: dict  # place name -> (file count, bytes)


# Sort choices shown in the dropdown. Each entry is a function that turns a
# movie into a sortable key, plus whether to reverse the order. Movies with
# no year always sort last. ("Date Logged" gets added once the exporter
# writes that field.)
SORT_OPTIONS = {
    "Title (A-Z)": (lambda m: m.title.casefold(), False),
    "Title (Z-A)": (lambda m: m.title.casefold(), True),
    "Year (newest first)": (lambda m: (m.year is None, -(m.year or 0), m.title.casefold()), False),
    "Year (oldest first)": (lambda m: (m.year is None, m.year or 0, m.title.casefold()), False),
    # Dates are ISO text (YYYY-MM-DD), which sorts correctly as plain text.
    # Newest first uses each film's LATEST log; oldest first uses its EARLIEST.
    # Films with no date at all always sort last in both directions.
    "Date logged (newest first)": (lambda m: m.last_logged or "", True),
    "Date logged (oldest first)": (lambda m: (m.first_logged is None, m.first_logged or ""), False),
}


def displayed_date(movie, sort_name):
    """The date shown in the 'Logged' column: it matches the sort, so an
    oldest-first list shows each film's first log, otherwise its latest."""
    return movie.first_logged if sort_name == "Date logged (oldest first)" else movie.last_logged


def describe_logs(movie):
    """Plain-text summary of a film's logs, used as the row tooltip."""
    lines = []
    if movie.logs:
        noun = "time" if len(movie.logs) == 1 else "times"
        lines.append(f"Logged {len(movie.logs)} {noun}:")
        for log in movie.logs:
            date = log.logged_date or "no date"
            parts = [f"rated {date}" if log.source == "rating" else date]
            if log.watched_date and log.watched_date != log.logged_date:
                parts.append(f"watched {log.watched_date}")
            if log.rating is not None:
                parts.append(f"\u2605{log.rating:g}")
            if log.rewatch:
                parts.append("rewatch")
            lines.append("  " + " \u00b7 ".join(parts))
    if movie.watchlist_added:
        lines.append(f"On watchlist since {movie.watchlist_added}")
    return "\n".join(lines)


def format_size(size_bytes):
    """Turn a byte count into a short human-friendly string."""
    if size_bytes is None:
        return ""
    if size_bytes >= 1024**4:
        return f"{size_bytes / 1024**4:.2f} TB"
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    return f"{size_bytes / 1024**2:.0f} MB"


def _fold(text):
    """Simplify text for searching: no accents, no punctuation, any case.

    "Amélie" -> "amelie", "Spider-Man: Homecoming" -> "spider man homecoming".
    Searching in Python instead of SQL LIKE is what lets "amelie" find
    "Amélie" (SQLite's LIKE ignores case only for plain English letters).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", without_accents).casefold().strip()


def _norm_path(path):
    """Comparison form of a path: backslashes, no trailing slash, any case."""
    return path.replace("/", "\\").rstrip("\\").casefold()


def _is_excluded(path, excluded):
    """True if the path is inside (or equal to) any excluded folder."""
    norm = _norm_path(path)
    return any(norm == folder or norm.startswith(folder + "\\") for folder in excluded)


def _placeholders(values):
    """'?, ?, ?' for use in an SQL IN (...) clause."""
    return ", ".join("?" for _ in values)


def get_places(conn):
    """All drives, shares and servers, ordered local -> network -> remote."""
    rows = conn.execute("SELECT id, name, kind FROM places").fetchall()
    places = [Place(*row) for row in rows]
    places.sort(key=lambda p: (KIND_ORDER.get(p.kind, 9), p.name.casefold()))
    return places


def count_movies(conn, scope="all"):
    """Number of films in a scope (see SCOPES)."""
    try:
        if scope == "watched":
            sql = "SELECT COUNT(DISTINCT movie_id) FROM watch_logs"
        elif scope == "watchlist":
            sql = "SELECT COUNT(*) FROM movies WHERE watchlist_added IS NOT NULL"
        else:
            sql = "SELECT COUNT(*) FROM movies"
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return 0  # database not upgraded yet (re-run database.py)


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _load_movie_rows(conn):
    """(id, title, year, letterboxd_url, watchlist_added) for every film.

    Falls back to the older, smaller layout if database.py hasn't been
    re-run yet, so the app still opens (just without the newer fields).
    """
    try:
        return conn.execute(
            "SELECT id, title, year, letterboxd_url, watchlist_added FROM movies"
        ).fetchall()
    except sqlite3.OperationalError:
        return [(i, t, y, None, None) for i, t, y in conn.execute("SELECT id, title, year FROM movies")]


def _load_logs(conn):
    """All dated logs grouped by film id, each film's logs oldest first."""
    logs_by_movie = defaultdict(list)
    if not _table_exists(conn, "watch_logs"):
        return logs_by_movie
    rows = conn.execute(
        "SELECT movie_id, logged_date, watched_date, rating, rewatch, source "
        "FROM watch_logs ORDER BY logged_date, watched_date"
    )
    for movie_id, logged, watched, rating, rewatch, source in rows:
        logs_by_movie[movie_id].append(LogEntry(logged, watched, rating, bool(rewatch), source))
    return logs_by_movie


def fetch_results(
    conn,
    search,
    confidences,
    local_place_ids,
    remote_place_ids,
    include_not_found,
    excluded_folders,
    sort_name,
    scope="all",
):
    """Return the list of MovieEntry objects matching the current filters."""
    # 1. Every movie that matches the search text.
    needle = _fold(search)
    logs_by_movie = _load_logs(conn)
    movies = {}
    for movie_id, title, year, url, watchlist_added in _load_movie_rows(conn):
        if not needle or needle in _fold(title):
            logs = logs_by_movie.get(movie_id, [])
            dated = [log.logged_date for log in logs if log.logged_date]
            # A film with no logs falls back to the date it joined the watchlist.
            first = dated[0] if dated else watchlist_added
            last = dated[-1] if dated else watchlist_added

            # Scope: which films are considered at all.
            if scope == "watched" and not logs:
                continue
            if scope == "watchlist":
                if not watchlist_added:
                    continue
                first = last = watchlist_added  # dates now mean "added to watchlist"

            movies[movie_id] = MovieEntry(
                movie_id, title, year,
                letterboxd_url=url, logs=logs, first_logged=first, last_logged=last,
                watchlist_added=watchlist_added,
            )

    excluded = [_norm_path(folder) for folder in excluded_folders]

    # 2. Visible files: right confidence, right place, not in an excluded folder.
    if confidences and local_place_ids:
        sql = (
            "SELECT l.movie_id, l.path, l.size_bytes, l.confidence, p.name "
            "FROM locations l JOIN places p ON p.id = l.place_id "
            f"WHERE l.confidence IN ({_placeholders(confidences)}) "
            f"AND l.place_id IN ({_placeholders(local_place_ids)}) "
            "ORDER BY l.path"
        )
        for movie_id, path, size, confidence, place_name in conn.execute(
            sql, [*confidences, *local_place_ids]
        ):
            if movie_id in movies and not _is_excluded(path, excluded):
                movies[movie_id].files.append(FileEntry(path, size, confidence, place_name))

    # 3. Plex availability on the ticked servers (if that table exists yet).
    has_remote_table = _table_exists(conn, "remote_availability")
    if has_remote_table and remote_place_ids:
        sql = (
            "SELECT r.movie_id, p.name FROM remote_availability r "
            "JOIN places p ON p.id = r.place_id "
            f"WHERE r.place_id IN ({_placeholders(remote_place_ids)}) ORDER BY p.name"
        )
        for movie_id, server_name in conn.execute(sql, list(remote_place_ids)):
            if movie_id in movies:
                movies[movie_id].servers.append(server_name)

    # 4. "Not found" = no files at all (ignoring filters) and no Plex match.
    not_found_ids = set()
    if include_not_found:
        has_files = {row[0] for row in conn.execute("SELECT DISTINCT movie_id FROM locations")}
        on_servers = set()
        if has_remote_table:
            on_servers = {row[0] for row in conn.execute("SELECT DISTINCT movie_id FROM remote_availability")}
        not_found_ids = set(movies) - has_files - on_servers

    # 5. Keep only the movies that qualify, then sort.
    results = [
        m for m in movies.values()
        if m.files or m.servers or m.movie_id in not_found_ids
    ]
    # Sort by title first, then by the chosen key. Python's sort is "stable"
    # (ties keep their existing order), so films with the same date, or
    # many logged on the same day, stay in alphabetical order.
    results.sort(key=lambda m: m.title.casefold())
    key, reverse = SORT_OPTIONS[sort_name]
    results.sort(key=key, reverse=reverse)
    return results


def summarize(results):
    """Totals for the info panel, computed from exactly what's on screen."""
    per_place = {}
    total_files = 0
    total_bytes = 0
    for movie in results:
        for f in movie.files:
            total_files += 1
            total_bytes += f.size_bytes or 0
            count, size = per_place.get(f.place_name, (0, 0))
            per_place[f.place_name] = (count + 1, size + (f.size_bytes or 0))
    return Summary(len(results), total_files, total_bytes, per_place)