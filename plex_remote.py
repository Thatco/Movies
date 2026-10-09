"""
plex_remote.py

Python port of API_call.ps1: asks each friend's Plex server which movies
it has. For every server in config.json's "friend_server_names" it:

  1. Asks plex.tv for your account's servers and finds the named one.
  2. Picks a direct (non-relay, non-local) connection, falling back to any.
  3. Finds the server's movie library (asking you once if there are
     several, then remembering the answer in library_defaults.json).
  4. Downloads the movie list and saves it to a per-server cache file.
  5. If the live check fails (server offline, expired token...), uses
     the cached list from the last successful run instead.

Returns {server_name: [{"title": ..., "year": ...}, ...]}.

Setup: `python -m pip install requests`, then fill in config.json.
Run this file directly for a test that prints counts and a sample.
"""

import json
from pathlib import Path

import requests

from config import HERE, load_config

REQUEST_TIMEOUT = 30  # seconds to wait before giving up on a server


def _get_json(url, token, client_id, params=None):
    """GET a URL and return the parsed JSON.

    The token goes in a header rather than in the URL. That matters
    because error messages from `requests` include the URL, and we print
    those messages as warnings: a token in the URL would be printed too.
    """
    response = requests.get(
        url,
        params=params,
        headers={
            "Accept": "application/json",
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": client_id,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()  # turns HTTP errors (401, 500...) into exceptions
    return response.json()


def _load_library_defaults(path):
    """Read the saved {server name: library key} map (empty if none yet)."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _choose_library(server_name, libraries, defaults_path):
    """Return the key of the movie library to use on this server."""
    defaults = _load_library_defaults(defaults_path)

    # A saved choice wins, as long as that library still exists.
    saved_key = defaults.get(server_name)
    if any(lib["key"] == saved_key for lib in libraries):
        return saved_key

    if len(libraries) == 1:
        return libraries[0]["key"]

    # Several movie libraries: ask once, then remember the answer.
    while True:
        print(f"Multiple movie libraries were found on '{server_name}':")
        for number, lib in enumerate(libraries, start=1):
            print(f"  {number}: {lib['title']}")
        answer = input("Enter the number of the library to use (remembered next time): ")
        if answer.isdigit() and 1 <= int(answer) <= len(libraries):
            break
    key = libraries[int(answer) - 1]["key"]

    defaults[server_name] = key
    defaults_path.parent.mkdir(parents=True, exist_ok=True)
    defaults_path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
    return key


def _fetch_live(server_name, config, cache_folder, peek=False):
    """Ask the server directly for its movie list. Raises on any failure."""
    token = config["plex_account_token"]
    client_id = config["plex_client_identifier"]

    # Step 1: find the server among the account's resources.
    resources = _get_json(
        "https://plex.tv/api/v2/resources", token, client_id,
        params={"includeHttps": 1},
    )
    server = next(
        (r for r in resources
         if r.get("name") == server_name and r.get("product") == "Plex Media Server"),
        None,
    )
    if server is None:
        raise RuntimeError(f"No server named '{server_name}' was found.")

    # Step 2: prefer a direct connection (we're not on their network, so
    # "local" addresses are useless, and relays are slow).
    connections = server.get("connections", [])
    connection = next(
        (c for c in connections if not c.get("relay") and not c.get("local")),
        connections[0] if connections else None,
    )
    if connection is None:
        raise RuntimeError(f"'{server_name}' has no connections listed.")
    base_url = connection["uri"]
    server_token = server["accessToken"]  # per-server token, not your account token

    # Step 3: find the movie libraries on that server.
    sections = _get_json(f"{base_url}/library/sections", server_token, client_id)
    libraries = [d for d in sections["MediaContainer"].get("Directory", [])
                 if d.get("type") == "movie"]
    if not libraries:
        raise RuntimeError(f"No movie library was found on '{server_name}'.")
    key = _choose_library(server_name, libraries, cache_folder / "library_defaults.json")

    # Step 4: download every movie in that library.
    listing = _get_json(f"{base_url}/library/sections/{key}/all", server_token, client_id)
    raw_movies = listing["MediaContainer"].get("Metadata", [])

    if peek and raw_movies:
        # Discovery aid: shows which fields Plex gives us for each movie
        # (year, file sizes, ...) so we can decide what else to store.
        print(f"  Fields available per movie: {sorted(raw_movies[0].keys())}")

    return [{"title": m["title"], "year": m.get("year")} for m in raw_movies]


def get_remote_movies(config=None, peek=False):
    """Return {server_name: [{"title", "year"}, ...]} for every friend server."""
    config = config or load_config()
    cache_folder = HERE / config["remote_cache_folder"]  # absolute paths also work
    cache_folder.mkdir(parents=True, exist_ok=True)

    results = {}
    for server_name in config["friend_server_names"]:
        cache_file = cache_folder / f"{server_name}.json"
        try:
            movies = _fetch_live(server_name, config, cache_folder, peek=peek)
            # Refresh the cache so a future outage has something recent.
            cache_file.write_text(json.dumps(movies, ensure_ascii=False), encoding="utf-8")
            print(f"Live-checked '{server_name}': {len(movies)} movies.")
        except Exception as error:
            print(f"Live check of '{server_name}' failed ({error}) - trying the cached list.")
            if not cache_file.exists():
                print(f"No cached list for '{server_name}' either - skipping it this run.")
                continue
            movies = json.loads(cache_file.read_text(encoding="utf-8"))
        results[server_name] = movies
    return results


if __name__ == "__main__":
    remote = get_remote_movies(peek=True)
    for name, movies in remote.items():
        print(f"\n{name}: {len(movies)} movies. First three:")
        for movie in movies[:3]:
            print(f"  {movie['title']} ({movie['year']})")
