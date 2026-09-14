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
BAR_MIN = 5

INTRADAY_ENDPOINTS = (
    "historical-chart/1min",
    "historical-chart/5min",
    "historical-chart/15min",
    "historical-chart/1hour",
)
SAMPLE_SYMBOLS = ("NVDA", "GME", "MARA")


# FMP stamps both bars and news in US Eastern, not UTC. The first version of
# this probe compared those stamps against datetime.now(timezone.utc) and
# reported the news feed as FOUR HOURS stale -- which was the EDT offset,
# not a delay. The real figure was 9-16 minutes. That error nearly cost the
# user a data-plan upgrade they did not need.
#
# The tell was sitting in the probe's own output the whole time: the newest
# 5-minute bar came back stamped 14:30 while the probe ran at 18:36 UTC,
# mid-session. A six-minute-old bar cannot look four hours old unless the
# clocks differ. Any latency measured against a vendor timestamp must first
# establish what timezone that timestamp is in.
try:
    from zoneinfo import ZoneInfo
    _MARKET_TZ = ZoneInfo("America/New_York")
except Exception:
    _MARKET_TZ = None


def _market_now():
    """Wall-clock 'now' in the same timezone FMP stamps its data in."""
    if _MARKET_TZ is not None:
        return datetime.now(_MARKET_TZ).replace(tzinfo=None)
    # No tz database available: fall back to deriving the offset from a live
    # quote rather than hardcoding -4, which would be wrong half the year.
    return datetime.utcnow() - timedelta(hours=4)


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
    now = _market_now()
    out = {"probed_at_market_time": now.isoformat(),
           "probed_at_utc": datetime.now(timezone.utc).isoformat(),
           "latest_feeds": {}, "history": {},
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
                        lags.append((now - dt).total_seconds() / 60.0)
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


# --- rolling latency tracker ----------------------------------------------
#
# "The freshest article is 9 minutes old" is NOT a latency measurement. It is
# consistent with a feed that delivers instantly and simply had nothing to
# publish for nine minutes. The two cases look identical in a single sample
# and they have opposite consequences for a trading bot.
#
# Real delivery latency needs repeated observation: watch the feed, and for
# each article the first time it appears, record how long ago it CLAIMS to
# have been published. That difference is the delay, and only the minimum
# across many observations is trustworthy -- a quiet feed inflates the mean
# with articles that were simply published a while before anyone looked.
LATENCY_PATH = os.path.join(BASE, "results", "news_latency.json")
LATENCY_KEEP = 400


def track_latency(log=print, feed="news/stock-latest"):
    """One API call. Call every cycle; the picture sharpens over a day."""
    try:
        state = {}
        if os.path.exists(LATENCY_PATH):
            with open(LATENCY_PATH) as f:
                state = json.load(f) or {}
        seen = set(state.get("seen_ids") or [])
        samples = list(state.get("samples_min") or [])

        now = _market_now()
        rows = fmp._get(feed, {"page": 0, "limit": 50})
        rows = rows if isinstance(rows, list) else []
        fresh = 0
        for r in rows:
            _k, raw = _date_field(r)
            dt, has_time = _parse_ts(raw)
            if not dt or not has_time:
                continue
            ident = f"{raw}|{(r.get('title') or '')[:60]}"
            if ident in seen:
                continue
            seen.add(ident)
            lag = (now - dt).total_seconds() / 60.0
            # Guard against clock/timezone surprises rather than silently
            # folding them into the statistics.
            if -30 <= lag <= 720:
                samples.append(round(lag, 2))
                fresh += 1

        samples = samples[-LATENCY_KEEP:]
        seen_list = list(seen)[-4000:]
        state = {"samples_min": samples, "seen_ids": seen_list,
                 "feed": feed, "updated_at": now.isoformat()}
        os.makedirs(os.path.dirname(LATENCY_PATH), exist_ok=True)
        with open(LATENCY_PATH, "w") as f:
            json.dump(state, f)

        if samples:
            srt = sorted(samples)
            p10 = srt[int(len(srt) * 0.10)]
            med = srt[len(srt) // 2]
            log(f"news latency: {fresh} new article(s) this cycle; over "
                f"{len(samples)} observations the fastest was {srt[0]:.1f} min, "
                f"10th pct {p10:.1f} min, median {med:.1f} min "
                f"(the FASTEST figure is the feed's real delay -- the median "
                f"mostly reflects how often anything gets published)")
        return state
    except Exception as e:
        log(f"news latency tracking failed (non-fatal): {e}")
        return None


# --- what does THIS key actually allow? -----------------------------------
#
# Written because the pricing page and the account holder disagreed about
# which plan is active, and the endpoints had already contradicted both:
# 5-minute bars returned data (which the page lists as a paid-tier feature
# above Starter) while 1-minute returned 402. A vendor's marketing table
# describes the plan they want to sell; only the key describes the key.
#
# This matters beyond the upgrade question. scripts/backtest.py reaches back
# STRESS_HISTORY_DAYS = 3000 (~8 years) for the COVID and 2018 crash windows.
# If the plan truncates daily history to 5 years, those windows are not
# available and the crash analysis is describing less data than it appears
# to. (run_stress_tests guards against this and reports a per-window error
# rather than a truncated result -- but knowing the real limit means knowing
# whether to expect that error.)
PLAN_PATH = os.path.join(BASE, "results", "plan_capabilities.json")

DAILY_DEPTH_PROBES = (
    ("1 year", 365), ("2 years", 730), ("5 years", 1825),
    ("7 years", 2555), ("10 years", 3650), ("15 years", 5475),
)
INTRADAY_DEPTH_PROBES = (
    ("30 days", 30), ("180 days", 180), ("400 days", 400),
    ("800 days", 800), ("1500 days", 1500),
)


def probe_plan(log=print, symbol="NVDA"):
    now = _market_now()
    out = {"probed_at": now.isoformat(), "symbol": symbol,
           "daily_depth": {}, "intraday_depth": {}, "endpoints": {}}

    log(f"plan probe: measuring what this API key actually returns ({symbol})")

    # --- how far back does DAILY history really go? ---
    deepest_daily = None
    for label, days in DAILY_DEPTH_PROBES:
        frm = (now - timedelta(days=days)).date().isoformat()
        to = (now - timedelta(days=days - 20)).date().isoformat()
        rec = {"requested_from": frm}
        try:
            rows = fmp.historical_price_full(symbol, frm, to)
            rec["rows"] = len(rows)
            if rows:
                rec["oldest_returned"] = rows[0].get("date") or rows[0].get("Date")
                deepest_daily = label
        except Exception as e:
            rec["error"] = str(e)[:120]
        out["daily_depth"][label] = rec
        log(f"  daily {label:>9} back ({frm}): "
            + (f"{rec['rows']} bars" if rec.get("rows")
               else f"NONE ({rec.get('error', 'empty')})"))

    # --- how far back do 5-MINUTE bars go? ---
    deepest_intra = None
    for label, days in INTRADAY_DEPTH_PROBES:
        frm = (now - timedelta(days=days)).date().isoformat()
        to = (now - timedelta(days=days - 4)).date().isoformat()
        rec = {"requested_from": frm}
        try:
            rows = fmp._get(f"historical-chart/{BAR_MIN}min",
                            {"symbol": symbol, "from": frm, "to": to})
            rows = rows if isinstance(rows, list) else []
            rec["rows"] = len(rows)
            if rows:
                rec["oldest_returned"] = str(rows[-1].get("date"))
                deepest_intra = label
        except Exception as e:
            rec["error"] = str(e)[:120]
        out["intraday_depth"][label] = rec
        log(f"  5-min {label:>9} back ({frm}): "
            + (f"{rec['rows']} bars" if rec.get("rows")
               else f"NONE ({rec.get('error', 'empty')})"))

    # --- which endpoints the bot uses / would want are actually open? ---
    checks = (
        ("quote", {"symbol": symbol}),
        ("historical-price-eod/full", {"symbol": symbol,
                                       "from": (now - timedelta(days=10)).date().isoformat(),
                                       "to": now.date().isoformat()}),
        ("company-screener", {"limit": 5}),
        ("news/stock", {"symbols": symbol, "limit": 5}),
        ("news/stock-latest", {"page": 0, "limit": 5}),
        ("news/general-latest", {"page": 0, "limit": 5}),
        ("news/press-releases-latest", {"page": 0, "limit": 5}),
        ("historical-chart/1min", {"symbol": symbol,
                                   "from": (now - timedelta(days=3)).date().isoformat(),
                                   "to": now.date().isoformat()}),
        ("historical-chart/5min", {"symbol": symbol,
                                   "from": (now - timedelta(days=3)).date().isoformat(),
                                   "to": now.date().isoformat()}),
        ("shares-float-all", {"page": 0, "limit": 10}),
        ("most-actives", {}),
        ("earnings", {"symbol": symbol}),
    )
    for path, params in checks:
        rec = {}
        try:
            rows = fmp._get(path, params)
            n = len(rows) if isinstance(rows, list) else (1 if rows else 0)
            rec = {"ok": bool(n), "rows": n}
        except Exception as e:
            msg = str(e)
            rec = {"ok": False,
                   "restricted": "402" in msg or "Restricted" in msg,
                   "error": msg[:120]}
        out["endpoints"][path] = rec
        log(f"  {path:<30} " + ("OK" if rec.get("ok")
            else ("RESTRICTED (plan)" if rec.get("restricted") else "FAIL")))

    out["summary"] = {
        "deepest_daily_history": deepest_daily,
        "deepest_5min_history": deepest_intra,
        "open_endpoints": [p for p, r in out["endpoints"].items() if r.get("ok")],
        "plan_restricted": [p for p, r in out["endpoints"].items() if r.get("restricted")],
    }
    log(f"plan probe SUMMARY: daily history reaches {deepest_daily or 'UNKNOWN'}; "
        f"5-minute history reaches {deepest_intra or 'NONE'}; "
        f"{len(out['summary']['plan_restricted'])} endpoint(s) blocked by plan: "
        f"{out['summary']['plan_restricted'] or 'none'}")
    log("  (backtest.py wants ~8 years of daily history for the COVID and 2018 "
        "stress windows -- if daily stops short of that, those windows report "
        "an error instead of a result)")

    os.makedirs(os.path.dirname(PLAN_PATH), exist_ok=True)
    with open(PLAN_PATH, "w") as f:
        json.dump(out, f, indent=2)
    return out
