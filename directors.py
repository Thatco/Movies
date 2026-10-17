"""
directors.py

Looks up each movie's director(s) from its Letterboxd page and saves them
in the `directors` column of movies.db, so the app can show a Director
column and let you search by director.

  python directors.py                 fetch directors for every movie that
                                      doesn't have them yet (prints progress)
  python directors.py <letterboxd url>
                                      just try ONE page and print what was
                                      found: a quick way to check that the
                                      lookup works on your machine

Also used by the app's "Get Directors" button (fetch_directors).

How it behaves:
  * One page at a time with a pause in between (REQUEST_DELAY), so it is
    gentle on Letterboxd. 1,100 movies takes roughly 20 minutes.
  * Each movie is saved the moment it's read, so you can cancel (or close
    the app) at any time and the next run continues where this one stopped.
  * Movies that already have directors are never fetched again.
  * It stops with a clear message if Letterboxd starts refusing requests
    (HTTP 403 / 429), or if page after page yields no director at all, which
    would mean the page layout changed and the parser needs an update.

Letterboxd has no public API for this, so the director is read from the
film page itself. parse_directors() tries three places in turn (the page's
structured data, the /director/ links, and the social-card tags), so a
small layout change is unlikely to break all of them at once.
"""

import html as html_lib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import links

HERE = Path(__file__).parent
DB_PATH = HERE / "movies.db"

# Seconds to wait between pages.
REQUEST_DELAY = 5

# Give up (and say why) after this many pages in a row with no director found.
MAX_CONSECUTIVE_EMPTY = 10
# ...or this many network failures in a row.
MAX_CONSECUTIVE_ERRORS = 5

# A normal browser's identity; plain "Python-urllib" is often refused.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Stop reading a page after this many bytes (film pages are well under it).
MAX_PAGE_BYTES = 3_000_000


class DirectorFetchError(Exception):
    """The lookup had to stop (blocked, layout changed, no network...)."""


# ----------------------------------------------------------------------
# Reading directors out of a page
# ----------------------------------------------------------------------
_JSON_LD = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)
_DIRECTOR_LINK = re.compile(
    r'<a\b[^>]*href="/director/[^"/]+/?"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_META = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_TAG = re.compile(r"<[^>]+>")


def _clean(name):
    """Tidy one name: no tags, HTML entities decoded, whitespace collapsed."""
    return " ".join(html_lib.unescape(_TAG.sub("", name)).split())


def _unique(names):
    """Remove blanks and repeats, keeping the original order."""
    seen = set()
    result = []
    for name in names:
        name = _clean(name)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def _from_json_ld(page):
    """Directors from the page's structured data ("director": [...])."""
    names = []
    for block in _JSON_LD.findall(page):
        # Letterboxd wraps its JSON in /* <![CDATA[ */ ... /* ]]> */ comments.
        block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL).strip()
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            directors = item.get("director")
            if isinstance(directors, dict):
                directors = [directors]
            for director in directors or []:
                if isinstance(director, dict):
                    names.append(director.get("name", ""))
                elif isinstance(director, str):
                    names.append(director)
    return _unique(names)


def _from_links(page):
    """Directors from links like <a href="/director/michael-mann/">."""
    return _unique(_DIRECTOR_LINK.findall(page))


def _from_meta(page):
    """Directors from the social-card tags: label "Directed by", data "Name"."""
    tags = {}
    for attributes in _META.findall(page):
        found = dict(_ATTRIBUTE.findall(attributes))
        key = found.get("name") or found.get("property")
        if key and "content" in found:
            tags[key.lower()] = found["content"]
    for number in ("1", "2"):
        label = tags.get(f"twitter:label{number}", "")
        if label.lower().startswith("directed"):
            return _unique(tags.get(f"twitter:data{number}", "").split(","))
    return []


def parse_directors(page):
    """The directors named on a Letterboxd film page, as a list of names."""
    return _from_json_ld(page) or _from_links(page) or _from_meta(page)


# ----------------------------------------------------------------------
# Fetching pages
# ----------------------------------------------------------------------
def fetch_page(url):
    """Download one page and return its text.

    Raises DirectorFetchError when Letterboxd is refusing requests (the
    lookup should stop), and OSError for ordinary failures (this one movie
    is skipped; see fetch_directors).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(MAX_PAGE_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        if error.code in (403, 429, 503):
            raise DirectorFetchError(
                f"Letterboxd refused the request (HTTP {error.code}). It may be limiting "
                "requests, so wait a while and try again; what was fetched so far is saved."
            ) from error
        raise  # e.g. 404 for one movie: HTTPError is an OSError, so it just skips this one
    return raw.decode(charset, errors="replace")


# ----------------------------------------------------------------------
# The lookup itself
# ----------------------------------------------------------------------
def count_pending(db_path=DB_PATH):
    """How many movies still need their directors looked up."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        links.ensure_tables(conn)
        return conn.execute(
            "SELECT COUNT(*) FROM movies "
            "WHERE letterboxd_url IS NOT NULL AND directors_checked IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def fetch_directors(db_path=DB_PATH, progress=None, cancel_event=None,
                    delay=REQUEST_DELAY, fetch=fetch_page):
    """Look up directors for every movie that doesn't have them yet.

    progress      optional function(done, total, message)
    cancel_event  optional threading.Event; set it to stop (what is already
                  saved stays saved)
    fetch         the function that downloads a page (replaceable for tests)

    Returns a dict: fetched (movies saved), missing (pages with no director
    found), remaining (movies still to do), cancelled (bool) and error (a
    message if the lookup had to stop early, else None).
    """
    conn = sqlite3.connect(db_path, timeout=30)
    summary = {"fetched": 0, "missing": 0, "remaining": 0, "cancelled": False, "error": None}
    try:
        links.ensure_tables(conn)
        conn.commit()
        todo = conn.execute(
            "SELECT id, title, letterboxd_url FROM movies "
            "WHERE letterboxd_url IS NOT NULL AND directors_checked IS NULL ORDER BY id"
        ).fetchall()
        total = len(todo)
        summary["remaining"] = total

        empty_in_a_row = 0
        errors_in_a_row = 0
        for done, (movie_id, _title, url) in enumerate(todo):
            if cancel_event is not None and cancel_event.is_set():
                summary["cancelled"] = True
                break
            if progress:
                progress(done, total, "Fetching directors")

            try:
                names = parse_directors(fetch(url))
                errors_in_a_row = 0
            except DirectorFetchError as error:
                summary["error"] = str(error)
                break
            except OSError:
                errors_in_a_row += 1
                if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                    summary["error"] = (
                        "Several requests in a row failed. Check your internet connection "
                        "and try again; what was fetched so far is saved."
                    )
                    break
                names = None  # this movie is skipped and retried next time

            if names:
                conn.execute(
                    "UPDATE movies SET directors = ?, directors_checked = datetime('now') "
                    "WHERE id = ?",
                    (", ".join(names), movie_id),
                )
                conn.commit()
                summary["fetched"] += 1
                summary["remaining"] -= 1
                empty_in_a_row = 0
            elif names is not None:
                # A page that loaded but named no director. Leave it unchecked
                # (so it is retried), and watch for a run of these: that means
                # the page layout changed, not that every film is director-less.
                summary["missing"] += 1
                empty_in_a_row += 1
                if empty_in_a_row >= MAX_CONSECUTIVE_EMPTY:
                    summary["error"] = (
                        f"{MAX_CONSECUTIVE_EMPTY} pages in a row had no director. Letterboxd's page "
                        "layout may have changed (or the pages are being blocked), so the "
                        "lookup was stopped. Try 'python directors.py <a film's Letterboxd link>' "
                        "to see what is coming back."
                    )
                    break

            # Pause politely, but wake straight away if cancelled.
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
        else:
            if progress:
                progress(total, total, "Fetching directors")
    finally:
        conn.close()
    return summary


def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
        try:
            page = fetch_page(url)
        except (DirectorFetchError, OSError) as error:
            sys.exit(f"Couldn't fetch the page: {error}")
        names = parse_directors(page)
        print(f"Page size: {len(page)} characters")
        print("Directors found:", ", ".join(names) if names else "(none)")
        for label, method in (("structured data", _from_json_ld),
                              ("/director/ links", _from_links),
                              ("social-card tags", _from_meta)):
            print(f"  via {label}: {method(page)}")
        return

    if not DB_PATH.exists():
        sys.exit("movies.db not found. Run database.py first.")

    def print_progress(done, total, _message):
        if done % 25 == 0 or done == total:
            print(f"Looked up {done} of {total} movies...")

    summary = fetch_directors(progress=print_progress)
    print(f"\nSaved directors for {summary['fetched']} movies; "
          f"{summary['remaining']} still to do.")
    if summary["missing"]:
        print(f"{summary['missing']} pages loaded but named no director (they'll be retried next run).")
    if summary["error"]:
        print(f"\nStopped early: {summary['error']}")


if __name__ == "__main__":
    main()
