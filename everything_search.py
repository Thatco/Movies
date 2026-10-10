"""
everything_search.py

Python port of the search logic from Search-MoviesOnComputer.ps1:

  * get_search_friendly_title()  <- Get-SearchFriendlyTitle
  * search_everything()          <- Search-MovieEverything (strict, then loose)
  * find_movie_files()           <- the fallback tiers in the main loop
  * match_confidence()           <- Get-MatchConfidence

It talks to Everything through es.exe, which asks the running Everything
app for results. Nothing here touches the database yet; the scanner will
call these functions later.
"""

import csv
import io
import re
import subprocess

# Full path to es.exe (raw string so backslashes stay literal). Later this
# will come from the program's config file instead of living in the code.
ES_PATH = r"C:\Users\Amphy\Downloads\ES-1.1.0.38.x64\es.exe"

# Video file types to look for (same list as the PowerShell config).
EXTENSIONS = ["mkv", "mp4", "avi", "wmv"]

# Resolution tags that upgrade a match from 'basic' to 'strong'.
# \b means "word boundary", so "1080p" matches but "x1080px" would not.
RESOLUTION_PATTERN = re.compile(r"\b(480p|720p|1080p|2160p|4k)\b", re.IGNORECASE)


def get_search_friendly_title(title):
    """Collapse every run of punctuation/symbols into a single space.

    "Spider-Man: Into the Spider-Verse" -> "Spider Man Into the Spider Verse"

    Windows filenames can't contain colons, and many titles lose their
    punctuation on disk, so we compare on punctuation-free text. In Python's
    regex, \\W means "not a letter/digit/underscore" (and it understands
    accented letters), so adding "_" to the set also removes underscores.
    """
    return re.sub(r"[\W_]+", " ", title).strip()


def _run_es(words, whole_word):
    """Run one es.exe search and return a list of (path, size_bytes).

    words      : the title split into separate words (each is its own
                 argument, which avoids Windows quote-mangling problems).
    whole_word : if True, pass -w so "M" can't match "Madness.mkv".
    """
    # -csv gives machine-readable output; -size adds each file's size.
    command = [ES_PATH, "-csv", "-size"]
    if whole_word:
        command.append("-w")
    command += words
    # Everything's "ext:" filter limits results to our video types.
    command.append("ext:" + ";".join(EXTENSIONS))

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # never crash on an unexpected character
    )

    # When Everything is reachable, es.exe always prints at least the CSV
    # header row. Completely empty output means something is wrong, most
    # likely that the Everything app isn't running.
    if not completed.stdout.strip():
        raise RuntimeError(
            "es.exe returned nothing. Is Everything running, and is "
            f"ES_PATH correct? {completed.stderr.strip()}"
        )

    results = []
    for row in csv.DictReader(io.StringIO(completed.stdout)):
        path = row.get("Filename")
        size = row.get("Size")
        if path:
            results.append((path, int(size) if size else None))
    return results


def search_everything(title):
    """Strict search first (whole words), then a looser retry if empty.

    The loose retry covers filenames like "Vermilion_Souls.avi", where
    Everything's whole-word check sees one single token and so never
    matches "Vermilion" or "Souls" on their own.
    """
    words = title.split()
    if not words:
        # An empty search would match EVERY file on the PC, so refuse it.
        return []

    results = _run_es(words, whole_word=True)
    if not results:
        results = _run_es(words, whole_word=False)
    return results


def find_movie_files(title):
    """Run the full fallback ladder for one movie title.

    Returns (results, matched_title), where results is a list of
    (path, size_bytes) and matched_title is whichever version of the
    title actually found files.

    Tier 1: the title exactly as written.
    Tier 2: the punctuation-free version ("Animal Crossing: The Movie"
            fails at tier 1 because the colon breaks the search, but
            "Animal Crossing The Movie" works here).
    Tier 3: for "Title / Alternate Title" entries, each half in turn.
    """
    results = search_everything(title)
    matched_title = title

    if not results:
        clean_title = get_search_friendly_title(title)
        if clean_title and clean_title != title:
            results = search_everything(clean_title)
            if results:
                matched_title = clean_title

    if not results and " / " in title:
        for part in title.split(" / "):
            part = part.strip()
            clean_part = get_search_friendly_title(part)
            if not clean_part:
                continue
            results = search_everything(clean_part)
            if results:
                matched_title = part
                break

    return results, matched_title

# Years either side of the Letterboxd year that still count as a year match.
# Set to 0 for exact-year matching only.
YEAR_TOLERANCE = 1

def match_confidence(path, year):
    if not year:
        return "low"
    acceptable_years = range(year - YEAR_TOLERANCE, year + YEAR_TOLERANCE + 1)
    if not any(str(y) in path for y in acceptable_years):
        return "low"
    if RESOLUTION_PATTERN.search(path):
        return "strong"
    return "basic"


if __name__ == "__main__":
    # Quick manual test. Edit the list to try other movies.
    tests = [
        ("Animal Crossing: The Movie", 2006),
        ("Paprika", 2006),
        ("Groundhog Day", 2001),
    ]
    for title, year in tests:
        files, matched = find_movie_files(title)
        print(f"\n{title} ({year}): {len(files)} file(s), matched via '{matched}'")
        for path, size in files:
            print(f"  [{match_confidence(path, year)}] {path} ({size} bytes)")