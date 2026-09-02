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
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from flask import Flask, send_from_directory

from scripts.refresh import run as refresh_run, log, within_market_hours
from lib import price_ticker
from scripts import fmp_client as fmp_module

app = Flask(__name__, static_folder=None)

SITE_DIR = os.path.join(BASE, "site")
PRICE_TICK_SECONDS = 30   # display-only quote refresh; see lib/price_ticker.py
REFRESH_INTERVAL_SECONDS = 15 * 60  # bumped from 30 min -- plan's rate limit is
                                     # 300 calls/min, not a daily cap, and cycles
                                     # never overlap (see the sleep below), so this
                                     # doesn't add any risk of hitting that ceiling

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


@app.route("/api/prices")
def api_prices():
    """Small JSON the dashboard polls every 30s so quotes stay live without
    regenerating the whole page. Cache headers off -- a cached price tick is
    a stale price tick, which is worse than none."""
    data = price_ticker.load_prices()
    resp = app.response_class(json.dumps(data), mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/healthz")
def healthz():
    """Cheap liveness check -- also what you'd point an external uptime
    monitor at if you want a heads-up beyond the results/heartbeat.json
    file whenever a refresh cycle fails outright. Also reports how many
    results/history/ snapshots are actually on disk right now (added
    2026-08-25 to verify the persistent disk survives redeploys instead of
    guessing from the rendered dashboard)."""
    from lib.track_record import build_track_record
    tr = build_track_record()
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "history_snapshot_count": tr.get("snapshot_count"),
        "history_first_snapshot_at": tr.get("first_snapshot_at"),
        "history_latest_snapshot_at": tr.get("latest_snapshot_at"),
    }


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


def _price_loop():
    """Quotes only, every PRICE_TICK_SECONDS during market hours. Kept in its
    own thread so a slow full refresh never delays a price tick and a failing
    tick never touches the refresh cycle. Errors are swallowed per-iteration
    on purpose: prices going stale for 30 seconds is a cosmetic problem, and
    it must never be able to take the process down."""
    log(f"Price ticker started -- quotes every {PRICE_TICK_SECONDS}s during market hours "
        f"(display only; never drives the bot or scoring).")
    while True:
        try:
            if within_market_hours():
                res = price_ticker.tick(fmp_module)
                if res is None:
                    price_ticker.write_prices({}, note="no tracked symbols yet")
        except Exception as e:
            log(f"price tick failed (non-fatal): {e}")
        time.sleep(PRICE_TICK_SECONDS)


def rebuild_page_on_boot():
    """Regenerate the dashboard from the last SAVED results the moment the
    process starts, before any refresh cycle runs.

    Why this is needed: site/index.html is tracked in git, so every deploy
    checks out whatever stale copy is in the repo and overwrites whatever the
    server had generated. Until the next full cycle finished -- five or six
    minutes into market hours, or not at all outside them -- the live site
    served an old page with tabs and data that no longer matched the code.
    results/ lives on the persistent disk and survives deploys, so the fresh
    page can be rebuilt from it instantly with no network calls. Non-fatal:
    if it fails, the normal cycle still regenerates the page later."""
    try:
        from lib.dashboard import generate_html
        out = generate_html()
        with open(out) as f:
            html = f.read()
        with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
            f.write(html)
        log("Rebuilt dashboard from saved results on boot (deploy reset the checked-in copy).")
    except Exception as e:
        log(f"boot page rebuild skipped ({e}) -- the next refresh cycle will regenerate it.")


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
        threading.Thread(target=_price_loop, daemon=True).start()


rebuild_page_on_boot()
start_scheduler_once()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
