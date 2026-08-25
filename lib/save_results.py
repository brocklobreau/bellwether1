"""
Persistence for run results. No network. Keeps the latest snapshot plus a
short rolling history (used to detect noteworthy changes between runs).
"""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE, "results")
LATEST_PATH = os.path.join(RESULTS_DIR, "latest.json")
HISTORY_DIR = os.path.join(RESULTS_DIR, "history")
# Kept generously long on purpose: this is now also the raw data behind the
# dashboard's Signal track record (lib/track_record.py), not just recent-run
# comparison. ~720 hourly snapshots is several months of market-hours runs;
# each snapshot is small (tens of KB), so the disk cost is trivial.
MAX_HISTORY = 720


def load_previous():
    if not os.path.exists(LATEST_PATH):
        return None
    with open(LATEST_PATH) as f:
        return json.load(f)


def save_run(payload: dict):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()

    # Write atomically (temp file + rename) so a concurrent run (e.g. a
    # manual fire overlapping the scheduled one) can never leave latest.json
    # half-written.
    tmp_path = LATEST_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, LATEST_PATH)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open(os.path.join(HISTORY_DIR, f"{ts}.json"), "w") as f:
        json.dump(payload, f, indent=2)

    # prune old history
    snaps = sorted(os.listdir(HISTORY_DIR))
    for old in snaps[:-MAX_HISTORY]:
        try:
            os.remove(os.path.join(HISTORY_DIR, old))
        except OSError:
            pass

    return payload
