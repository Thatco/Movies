"""
run_all.py

Runs the whole refresh pipeline in the right order. Stops at the first
step that fails, so a broken step can't feed bad data to the next one.

    python run_all.py            # everything
    python run_all.py --no-remote   # skip the Plex servers (faster, offline-friendly)

Not included: regenerating Movielist.json from your Letterboxd export.
Do that first when your Letterboxd list has changed. (This script is a
stopgap until the app gets built-in refresh buttons.)
"""

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

STEPS = [
    ("Loading the movie list", "database.py"),
    ("Scanning drives with Everything", "scanner.py"),
    ("Checking friends' Plex servers", "remote_scanner.py"),
]


def main():
    steps = STEPS[:-1] if "--no-remote" in sys.argv else STEPS

    for number, (description, script) in enumerate(steps, start=1):
        print(f"\n=== Step {number} of {len(steps)}: {description} ({script}) ===")
        started = time.time()
        # sys.executable = the exact Python running this script, so every
        # step uses the same interpreter (handy with several Pythons installed).
        result = subprocess.run([sys.executable, str(HERE / script)], cwd=HERE)
        if result.returncode != 0:
            sys.exit(f"\n{script} failed (exit code {result.returncode}). Stopping here.")
        print(f"--- done in {time.time() - started:.0f}s ---")

    print("\nAll steps finished. Start (or restart) app.py to see the new data.")


main()
