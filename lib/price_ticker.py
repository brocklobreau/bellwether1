"""
Fast price ticker: refreshes just the PRICES of everything on the dashboard
every ~30 seconds, independent of the 15-minute full refresh cycle.

Why this is separate from scripts/refresh.py: a full cycle costs ~1,000 API
calls and five-plus minutes because it re-fetches fundamentals, news, insider
filings and three screeners. None of that changes minute to minute. Prices
do. Splitting them means quotes can update 30x more often at roughly 1/500th
of the cost -- one `batch-quote-short` call covers every tracked symbol.

DELIBERATELY DISPLAY-ONLY. The ticker never feeds the bot, the scoring
pipeline, or the track record. Those run on the 15-minute cycle with a daily
thesis review, and that cadence is not an accident -- it is the fix for the
churn problem found in simulation, where re-deciding too often turned a
months-horizon strategy into a noise-driven trading machine. Letting 30-second
ticks drive exits would reintroduce exactly that, 30x worse. Prices here move
numbers on a screen; they do not move positions.
"""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICES_PATH = os.path.join(BASE, "results", "prices.json")
RESULTS_PATH = os.path.join(BASE, "results", "latest.json")

MAX_SYMBOLS = 120  # generous ceiling; one batch call regardless


def tracked_symbols(payload=None):
    """Every symbol currently visible on the dashboard, deduped."""
    if payload is None:
        try:
            with open(RESULTS_PATH) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
    syms = []
    for key in ("watchlist_results", "discovered_candidates", "value_picks",
                "screener_picks"):
        for r in payload.get(key) or []:
            if r.get("ticker"):
                syms.append(r["ticker"])
    for p in ((payload.get("bot") or {}).get("positions") or []):
        if p.get("ticker"):
            syms.append(p["ticker"])
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:MAX_SYMBOLS]


def fetch_prices(fmp, symbols):
    rows = fmp.batch_quote_short(symbols)
    prices = {}
    for r in rows or []:
        sym, px = r.get("symbol"), r.get("price")
        if sym and px is not None:
            prices[sym] = {"price": round(float(px), 4),
                           "change": r.get("change"),
                           "volume": r.get("volume")}
    return prices


def write_prices(prices, note=None):
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(),
               "count": len(prices), "prices": prices}
    if note:
        payload["note"] = note
    os.makedirs(os.path.dirname(PRICES_PATH), exist_ok=True)
    tmp = PRICES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, PRICES_PATH)   # atomic: readers never see a half file
    return payload


def load_prices():
    try:
        with open(PRICES_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "count": 0, "prices": {}}


def tick(fmp):
    """One pass. Returns the written payload, or None if nothing to fetch."""
    syms = tracked_symbols()
    if not syms:
        return None
    prices = fetch_prices(fmp, syms)
    if not prices:
        return None
    return write_prices(prices)
