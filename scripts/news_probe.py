"""
Data reconnaissance for a news-driven trading bot.

This answers questions, it does not trade and it does not score anything.
Before designing a bot around "watch the news and react", three things have
to be true, and none of them are knowable from inside this repo:

  1. LATENCY -- how old is a headline by the time the API will show it to
     us? If the feed lags the market by twenty minutes there is nothing to
     react to; the move already happened.
  2. HISTORY -- can we pull news as it looked on a past morning, with real
     timestamps? Without that, a news strategy cannot be backtested at all,
     and "cannot be backtested" is how the old TRADE side ended up running
     on hope for three weeks.
  3. GRANULARITY -- do timestamps carry a TIME, or only a date? Date-only
     stamps cannot be aligned to an intraday move, which silently turns any
     backtest into lookahead: "there was news that day" is knowledge you
     would not have had at 9:35am.

It also checks whether intraday bars are available on this plan, because
that is the single thing that would make a genuine day-trade strategy
testable rather than inferred from daily closes.

Writes results/news_probe.json and logs a readable summary. Safe to run
repeatedly; it is read-only against the API and costs ~15 calls.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import fmp_client as fmp

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_PATH = os.path.join(BASE, "results", "news_probe.json")

LATEST_ENDPOINTS = (
    "news/general-latest",
    "news/stock-latest",
    "news/press-releases-latest",
)
INTRADAY_ENDPOINTS = (
    "historical-chart/1min",
    "historical-chart/5min",
    "historical-chart/15min",
    "historical-chart/1hour",
)
SAMPLE_SYMBOLS = ("NVDA", "GME", "MARA")


def _parse_ts(v):
    """FMP timestamps come as 'YYYY-MM-DD HH:MM:SS' or bare 'YYYY-MM-DD'.
    Returns (datetime_or_None, has_time_component)."""
    if not v:
        return None, False
    s = str(v).strip().replace("T", " ")
    for fmt, has_time in (("%Y-%m-%d %H:%M:%S", True),
                          ("%Y-%m-%d %H:%M", True),
                          ("%Y-%m-%d", False)):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt))], fmt), has_time
        except ValueError:
            continue
    return None, False


def _date_field(row):
    for k in ("publishedDate", "date", "datetime", "publishedAt"):
        if row.get(k):
            return k, row[k]
    return None, None


def probe(log=print):
    now = datetime.now(timezone.utc)
    out = {"probed_at": now.isoformat(), "latest_feeds": {}, "history": {},
           "intraday": {}, "verdict": {}}

    # --- 1. which "latest news" feeds exist, and how stale are they? ---
    log("news probe: checking latest-news feeds...")
    for path in LATEST_ENDPOINTS:
        rec = {"available": False}
        try:
            rows = fmp._get(path, {"page": 0, "limit": 50})
            if isinstance(rows, list) and rows:
                rec["available"] = True
                rec["returned"] = len(rows)
                rec["fields"] = sorted(rows[0].keys())[:14]
                key, raw = _date_field(rows[0])
                rec["date_field"] = key
                rec["newest_raw"] = str(raw)
                lags = []
                has_time_any = False
                for r in rows[:50]:
                    _k, v = _date_field(r)
                    dt, has_time = _parse_ts(v)
                    has_time_any = has_time_any or has_time
                    if dt:
                        lags.append((now - dt.replace(tzinfo=timezone.utc)).total_seconds() / 60.0)
                rec["has_time_component"] = has_time_any
                if lags:
                    lags.sort()
                    rec["freshest_lag_min"] = round(lags[0], 1)
                    rec["median_lag_min"] = round(lags[len(lags) // 2], 1)
                rec["symbols_present"] = any(r.get("symbol") or r.get("tickers")
                                             for r in rows[:5])
            else:
                rec["note"] = "empty response"
        except Exception as e:
            rec["error"] = str(e)[:200]
        out["latest_feeds"][path] = rec
        if rec.get("available"):
            log(f"  {path}: OK, {rec['returned']} rows, field '{rec['date_field']}', "
                f"time component {rec['has_time_component']}, "
                f"freshest {rec.get('freshest_lag_min')}min old, "
                f"median {rec.get('median_lag_min')}min")
        else:
            log(f"  {path}: UNAVAILABLE ({rec.get('error') or rec.get('note')})")

    # --- 2. can we get news as it looked on a past date? ---
    log("news probe: checking historical news depth...")
    frm = (now - timedelta(days=120)).date().isoformat()
    to = (now - timedelta(days=90)).date().isoformat()
    for sym in SAMPLE_SYMBOLS:
        rec = {}
        try:
            rows = fmp._get("news/stock", {"symbols": sym, "from": frm, "to": to,
                                           "limit": 200})
            rows = rows if isinstance(rows, list) else []
            rec["returned"] = len(rows)
            stamps = []
            has_time_any = False
            for r in rows:
                _k, v = _date_field(r)
                dt, has_time = _parse_ts(v)
                has_time_any = has_time_any or has_time
                if dt:
                    stamps.append(dt)
            rec["has_time_component"] = has_time_any
            if stamps:
                stamps.sort()
                rec["oldest"] = stamps[0].isoformat()
                rec["newest"] = stamps[-1].isoformat()
                rec["in_requested_window"] = sum(
                    1 for s in stamps if frm <= s.date().isoformat() <= to)
                days = {s.date() for s in stamps}
                rec["distinct_days"] = len(days)
                rec["per_day"] = round(len(stamps) / max(len(days), 1), 1)
        except Exception as e:
            rec["error"] = str(e)[:200]
        out["history"][sym] = rec
        log(f"  {sym} news {frm}..{to}: {rec.get('returned', 0)} rows, "
            f"{rec.get('in_requested_window', 0)} actually inside the window, "
            f"time component {rec.get('has_time_component')}, "
            f"{rec.get('per_day', 0)}/day"
            + (f", ERROR {rec['error']}" if rec.get("error") else ""))

    # --- 3. intraday bars: the thing that would make day trading testable ---
    log("news probe: checking intraday bar availability...")
    d_to = now.date().isoformat()
    d_from = (now - timedelta(days=5)).date().isoformat()
    for path in INTRADAY_ENDPOINTS:
        rec = {"available": False}
        try:
            rows = fmp._get(path, {"symbol": "NVDA", "from": d_from, "to": d_to})
            if isinstance(rows, list) and rows:
                rec["available"] = True
                rec["returned"] = len(rows)
                rec["fields"] = sorted(rows[0].keys())[:10]
                rec["newest"] = str(rows[0].get("date"))
                rec["oldest"] = str(rows[-1].get("date"))
            else:
                rec["note"] = "empty response"
        except Exception as e:
            rec["error"] = str(e)[:200]
        out["intraday"][path] = rec
        if rec.get("available"):
            log(f"  {path}: OK, {rec['returned']} bars, "
                f"{rec['oldest']} .. {rec['newest']}")
        else:
            log(f"  {path}: UNAVAILABLE ({rec.get('error') or rec.get('note')})")

    # --- 4. how far back do intraday bars go? decides backtestability ---
    deep = {"available": False}
    try:
        old_from = (now - timedelta(days=400)).date().isoformat()
        old_to = (now - timedelta(days=395)).date().isoformat()
        rows = fmp._get("historical-chart/5min", {"symbol": "NVDA",
                                                  "from": old_from, "to": old_to})
        if isinstance(rows, list) and rows:
            deep = {"available": True, "returned": len(rows),
                    "oldest": str(rows[-1].get("date")),
                    "newest": str(rows[0].get("date")),
                    "window": f"{old_from}..{old_to}"}
    except Exception as e:
        deep["error"] = str(e)[:200]
    out["intraday_history_400d"] = deep
    log(f"  5min bars 400 days back: "
        + (f"OK, {deep['returned']} bars ({deep['oldest']} .. {deep['newest']})"
           if deep.get("available")
           else f"UNAVAILABLE ({deep.get('error') or 'empty'})"))

    # --- verdict ---
    feeds_ok = [p for p, r in out["latest_feeds"].items() if r.get("available")]
    timed = [p for p, r in out["latest_feeds"].items() if r.get("has_time_component")]
    hist_timed = any(r.get("has_time_component") for r in out["history"].values())
    hist_rows = any((r.get("in_requested_window") or 0) > 0 for r in out["history"].values())
    intra = [p for p, r in out["intraday"].items() if r.get("available")]
    out["verdict"] = {
        "live_feeds_available": feeds_ok,
        "live_feeds_with_time": timed,
        "historical_news_usable": bool(hist_rows and hist_timed),
        "intraday_bars_available": intra,
        "intraday_history_deep_enough": bool(deep.get("available")),
        "news_bot_backtestable": bool(hist_rows and hist_timed and deep.get("available")),
    }
    log("news probe VERDICT: "
        f"live feeds {feeds_ok or 'NONE'}; "
        f"historical news usable: {out['verdict']['historical_news_usable']}; "
        f"intraday bars {intra or 'NONE'}; "
        f"deep intraday history: {out['verdict']['intraday_history_deep_enough']}; "
        f"=> a news bot is backtestable: {out['verdict']['news_bot_backtestable']}")

    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    return out


if __name__ == "__main__":
    probe()
