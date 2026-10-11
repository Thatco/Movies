"""
settings.py

Saves and loads the window's remembered state (zoom, window size, which
filters are ticked, excluded folders, ...) in ui_settings.json next to the
program. It's a plain text file, so you can open it, read it, or delete it
to go back to the defaults.

This is separate from config.json on purpose: config.json holds secrets
and tool locations (set once), while ui_settings.json holds things the app
rewrites constantly. Add ui_settings.json to .gitignore.

Design notes:
  * A missing, unreadable, or outdated file never crashes the app; it just
    means "use the defaults".
  * Saving writes to a temporary file first and then swaps it in, so a
    crash mid-save can't leave a half-written (corrupt) settings file.
"""

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent / "ui_settings.json"

# Bump this if the layout of the saved data changes in a way that old files
# can't be understood; old files are then ignored instead of misread.
SETTINGS_VERSION = 1


def load_settings():
    """Return the saved settings dict, or {} if there are none to use."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read {SETTINGS_PATH.name} ({error}); using defaults.")
        return {}

    if not isinstance(data, dict) or data.get("version") != SETTINGS_VERSION:
        return {}
    return data


def save_settings(data):
    """Write the settings dict to disk (atomically)."""
    data = {"version": SETTINGS_VERSION, **data}
    temp_path = SETTINGS_PATH.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_path.replace(SETTINGS_PATH)  # swap the finished file into place
    except OSError as error:
        print(f"Could not save {SETTINGS_PATH.name}: {error}")
