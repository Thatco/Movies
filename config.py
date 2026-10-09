"""
config.py

Loads settings from config.json (the Python-side replacement for
Config.local.psd1). config.json holds secrets such as your Plex token, so
it must stay OUT of git: add `config.json` to .gitignore. The checked-in
template is config_example.json.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"


def load_config():
    """Read config.json and return its contents as a dict."""
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "config.json not found. Copy config_example.json to config.json "
            "and fill in your values."
        )
    # utf-8-sig tolerates a leading BOM (invisible marker) that some
    # Windows editors add to the start of the file.
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)
