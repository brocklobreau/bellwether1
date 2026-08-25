"""
Bellwether as a single always-on web service.

Serves the dashboard AND refreshes its own data on an internal timer, in
one process -- no separate GitHub Actions cron + static-site redeploy
needed. Simpler moving parts, but it does need to stay resident: Render's
FREE web services sleep after 15 minutes with no inbound traffic, which
would silently kill the background refresh thread -- exactly the kind of
"looks fine, quietly does nothing" failure this whole project has been
trying to get away from. This needs Render's cheapest ALWAYS-ON (paid)
instance type to actually work as intended.

Run locally with: python3 app.py
Deployed on Render with: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
(exactly one worker -- see the note above start_scheduler_once() for why)
"""
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from flask import Flask, send_from_directory

from scripts.refresh import run as refresh_run, log

app = Flask(__name__, static_folder=None)

SITE_DIR = os.path.join(BASE, "site")
REFRESH_INTERVAL_SECONDS = 30 * 60  # same cadence the GitHub Actions version used

_scheduler_started = False
_scheduler_lock = threading.Lock()

WARMUP_HTML = """<!doctype html><html><head><title>Bellwether</title></head>
<body style="font-family:sans-serif;max-width:40em;margin:4em auto;line-height:1.5">
<h1>Bellwether is warming up</h1>
<p>The first data refresh is running now (real HTTP calls to fetch and score
every ticker take a minute or two). Refresh this page shortly.</p>
</body></html>"""


@app.route("/")
def index():
    index_path = os.path.join(SITE_DIR, "index.html")
    if not os.path.exists(index_path):
        return WARMUP_HTML, 200
    return send_from_directory(SITE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(SITE_DIR, path)


@app.route("/healthz")
def healthz():
    """Cheap liveness check -- also what you'd point an external uptime
    monitor at if you want a heads-up beyond the results/heartbeat.json
    file whenever a refresh cycle fails outright."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


def _scheduler_loop():
    log("Background scheduler thread started -- refreshing immediately, then every "
        f"{REFRESH_INTERVAL_SECONDS // 60} minutes (refresh_run() itself skips the "
        "real work outside actual market hours).")
    while True:
        try:
            refresh_run()
        except Exception as e:
            # A single bad run must never take the whole scheduler thread down --
            # that would recreate the exact "looks alive, silently stopped doing
            # anything" failure mode this project exists to avoid.
            log(f"Scheduler loop error (continuing, will retry next cycle): {e}")
            traceback.print_exc()
        time.sleep(REFRESH_INTERVAL_SECONDS)


def start_scheduler_once():
    """Guards against starting the thread twice within ONE process. This does
    NOT protect against multiple gunicorn *worker processes* each starting
    their own thread (each worker is a separate Python interpreter with its
    own globals) -- that's why the deploy command must use --workers 1. With
    a single worker, this guard is what stops Flask's reloader (if ever
    enabled locally) from double-starting it."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        threading.Thread(target=_scheduler_loop, daemon=True).start()


start_scheduler_once()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
