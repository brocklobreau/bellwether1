"""
Does news actually move price -- and how fast does it happen?
=============================================================

Why this study exists
---------------------
The data probe (scripts/news_probe.py, run 2026-09-14) came back with two
facts that pull in opposite directions:

  * The LIVE news feed on this plan is roughly four hours stale. Freshest
    article in news/general-latest: 256 minutes old. Median: 418 minutes.
    You cannot react to that. By the time this account can see a headline,
    the trading day has moved on.

  * HISTORICAL news carries real timestamps (with a time component, not
    just a date), and 5-minute bars go back at least 400 days.

So the reaction bot cannot be built on this plan -- but the question of
whether it is WORTH buying a plan that could is now measurable. That is all
this file does. It places no trades and scores no setups. It measures what
price does around a timestamped headline, and how quickly.

The three questions, in order of how much they matter
----------------------------------------------------
1. DOES NEWS MOVE PRICE AT ALL? Measured against a control of random
   timestamps in the same sessions on the same tickers. Without that
   control the answer is trivially "yes" -- volatile stocks move all the
   time, news or no news, and any study that skips this step is measuring
   the stock, not the headline.

2. DOES THE MOVE HAPPEN BEFORE OR AFTER THE HEADLINE? This is the one that
   usually kills the idea. If price has already moved by the time the
   article is stamped, the article is REPORTING the move, not causing it,
   and there was never anything to react to -- the edge belonged to whoever
   moved it. Measured as the run-up from T-30min to T versus the drift from
   T to T+120min.

3. HOW FAST DO YOU HAVE TO BE? The decay curve: how much of the eventual
   move is already gone at 5 minutes, at 30, at 60. This is what actually
   prices a real-time feed. If the whole move is spent in the first five
   minutes, then a feed that is even one minute late is worth nothing, and
   no amount of code makes up the difference.

Direction, and the trap in it
-----------------------------
ABSOLUTE move (did it move) and SIGNED move (did it move the way you could
have bet) are reported separately and must be read separately. News reliably
produces the first. Trading requires the second. A strategy cannot be built
on "the stock moved a lot" unless you knew which way beforehand.

Timezone
--------
FMP returns both bars and news timestamps in US market local time. That is
an assumption, not a documented guarantee, so the run logs the hourly
distribution of news timestamps: if it clusters in the 9:30-16:00 band the
alignment is right, and if it does not, every number in this file is
measuring noise at the wrong offset and should be thrown out.
"""
import json
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import fmp_client as fmp

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_PATH = os.path.join(BASE, "results", "news_study.json")

# High-news-volume, liquid, volatile. Deliberately a MIX of news rates: the
# probe measured NVDA at 25 articles/day but GME at 1.7 and MARA at 1.2, and
# a study run only on the noisiest names would describe mega-cap news flow
# rather than the kind of name a day-trade bot would actually touch.
UNIVERSE = [
    "NVDA", "AMD", "TSLA", "PLTR", "SMCI", "MU",
    "GME", "MARA", "RIOT", "COIN", "SOFI", "HOOD",
    "AAL", "CVNA", "MRNA", "RIVN",
]

DAYS_BACK = 90
BAR_MINUTES = 5
BARS_PER_DAY = 78                 # 09:30-16:00 in 5-minute bars

# Horizons in MINUTES after the headline timestamp.
HORIZONS = (5, 15, 30, 60, 120)
PRE_WINDOW_MIN = 30               # run-up measured over the 30 min BEFORE

# Skip headlines in the first and last 15 minutes of a session: the open is
# its own volatility regime and the close leaves no room to measure a
# 120-minute horizon, so including either would confound the decay curve
# with time-of-day effects.
SESSION_EDGE_MIN = 15

CHUNK_DAYS = 5                    # bar fetches are windowed; FMP caps the range
MAX_BAR_CALLS = 700               # hard budget so a run cannot bleed the API


# --- data ------------------------------------------------------------------

def _parse_dt(v):
    if not v:
        return None
    s = str(v).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:len("2020-01-01 00:00:00" if len(fmt) > 14 else "2020-01-01 00:00")], fmt)
        except ValueError:
            continue
    return None


def fetch_bars(symbol, start, end, log=print, budget=None):
    """5-minute bars over [start, end], oldest first.

    Fetched in short windows because the endpoint caps how much it returns
    per call; asking for 90 days in one request silently returns a truncated
    range, which would look like a quiet stock rather than a partial answer.
    """
    out = {}
    day = start
    while day < end:
        stop = min(day + timedelta(days=CHUNK_DAYS), end)
        if budget is not None:
            if budget["calls"] >= MAX_BAR_CALLS:
                log(f"  bar budget exhausted at {budget['calls']} calls -- stopping early")
                break
            budget["calls"] += 1
        try:
            rows = fmp._get(f"historical-chart/{BAR_MINUTES}min",
                            {"symbol": symbol, "from": day.date().isoformat(),
                             "to": stop.date().isoformat()})
        except Exception as e:
            log(f"  {symbol} bars {day.date()}..{stop.date()}: {str(e)[:90]}")
            day = stop
            continue
        for r in rows if isinstance(rows, list) else []:
            dt = _parse_dt(r.get("date"))
            if dt is None:
                continue
            try:
                out[dt] = {"open": float(r["open"]), "high": float(r["high"]),
                           "low": float(r["low"]), "close": float(r["close"])}
            except (KeyError, TypeError, ValueError):
                continue
        day = stop
    return [(k, out[k]) for k in sorted(out)]


def fetch_news(symbol, start, end, log=print):
    """Timestamped headlines, oldest first. Paged, because one call returns
    at most a couple hundred rows and a busy ticker produces far more than
    that over 90 days -- taking only the first page would quietly restrict
    the study to the most recent fortnight."""
    seen = {}
    for page in range(0, 10):
        try:
            rows = fmp._get("news/stock", {"symbols": symbol, "limit": 250,
                                           "page": page,
                                           "from": start.date().isoformat(),
                                           "to": end.date().isoformat()})
        except Exception as e:
            log(f"  {symbol} news page {page}: {str(e)[:90]}")
            break
        rows = rows if isinstance(rows, list) else []
        if not rows:
            break
        before = len(seen)
        for r in rows:
            dt = _parse_dt(r.get("publishedDate") or r.get("date"))
            if dt and start <= dt <= end:
                seen[(dt, (r.get("title") or "")[:80])] = dt
        if len(seen) == before:
            break                  # page added nothing new -- end of the feed
    return sorted(seen.values())


# --- measurement -----------------------------------------------------------

def _session_map(bars):
    """date -> list of (datetime, bar) for that session, in order."""
    sessions = {}
    for dt, b in bars:
        sessions.setdefault(dt.date(), []).append((dt, b))
    return sessions


def _measure_at(session, i):
    """Returns from the OPEN of bar i out to each horizon, plus the run-up
    over the 30 minutes before it.

    Entry is bar i's OPEN, not its close: the headline lands during bar i,
    so the first price actually obtainable is that bar's open. Using its
    close would hand the study the first five minutes of the move for free,
    which is precisely the interval the decay curve exists to measure."""
    entry = session[i][1]["open"]
    if not entry:
        return None
    out = {}
    for h in HORIZONS:
        j = i + h // BAR_MINUTES
        if j >= len(session):
            out[h] = None
        else:
            out[h] = (session[j][1]["close"] - entry) / entry * 100.0
    back = i - PRE_WINDOW_MIN // BAR_MINUTES
    out["pre"] = (((entry - session[back][1]["open"]) / session[back][1]["open"] * 100.0)
                  if back >= 0 and session[back][1]["open"] else None)
    return out


def _eligible_indices(session):
    edge = SESSION_EDGE_MIN // BAR_MINUTES
    need = max(HORIZONS) // BAR_MINUTES
    return list(range(edge, max(edge, len(session) - need)))


def study_symbol(symbol, bars, news, rng, log=print):
    """Measure price around every in-session headline, and around an equal
    number of RANDOM in-session moments on the same days.

    The control is drawn from the same sessions, not from the whole window,
    so a day when the stock happened to be wild contributes to both arms
    equally. Without that, a study comparing news days against all days
    would mostly be rediscovering that news clusters on volatile days."""
    sessions = _session_map(bars)
    news_rows, ctrl_rows = [], []
    hours = {}
    matched = 0

    for t in news:
        hours[t.hour] = hours.get(t.hour, 0) + 1
        session = sessions.get(t.date())
        if not session:
            continue
        elig = _eligible_indices(session)
        if not elig:
            continue
        # First bar at or after the headline: the earliest moment a reader
        # of that headline could have acted.
        idx = None
        for k in elig:
            if session[k][0] >= t:
                idx = k
                break
        if idx is None:
            continue
        m = _measure_at(session, idx)
        if m:
            news_rows.append(m)
            matched += 1

    # One control draw per matched headline, same session, random moment.
    for t in news[:matched] if matched else []:
        session = sessions.get(t.date())
        if not session:
            continue
        elig = _eligible_indices(session)
        if not elig:
            continue
        m = _measure_at(session, rng.choice(elig))
        if m:
            ctrl_rows.append(m)

    return {"symbol": symbol, "news_events": len(news), "matched": matched,
            "news": news_rows, "control": ctrl_rows, "hours": hours,
            "sessions": len(sessions), "bars": len(bars)}


def _agg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean_abs": round(statistics.mean([abs(v) for v in vals]), 3),
        "median_abs": round(statistics.median([abs(v) for v in vals]), 3),
        "mean_signed": round(statistics.mean(vals), 4),
        "pct_up": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
    }


# --- the study -------------------------------------------------------------

def run_study(universe=None, days_back=DAYS_BACK, log=print, seed=11):
    universe = universe or UNIVERSE
    end = datetime.now() - timedelta(days=1)
    start = end - timedelta(days=days_back)
    rng = random.Random(seed)
    budget = {"calls": 0}

    per_symbol, all_news, all_ctrl = [], [], []
    hours = {}
    log(f"news study: {len(universe)} tickers, {start.date()}..{end.date()}, "
        f"{BAR_MINUTES}-minute bars")

    for sym in universe:
        bars = fetch_bars(sym, start, end, log=log, budget=budget)
        if len(bars) < BARS_PER_DAY * 5:
            log(f"  {sym}: only {len(bars)} bars -- skipping")
            continue
        news = fetch_news(sym, start, end, log=log)
        res = study_symbol(sym, bars, news, rng, log=log)
        per_symbol.append({k: res[k] for k in
                           ("symbol", "news_events", "matched", "sessions", "bars")})
        all_news.extend(res["news"])
        all_ctrl.extend(res["control"])
        for h, c in res["hours"].items():
            hours[h] = hours.get(h, 0) + c
        log(f"  {sym}: {len(bars)} bars over {res['sessions']} sessions, "
            f"{res['news_events']} headlines, {res['matched']} landed in-session")

    if not all_news:
        raise RuntimeError("no in-session headlines matched to bars -- nothing to measure")

    news_agg = {str(h): _agg(all_news, h) for h in HORIZONS}
    ctrl_agg = {str(h): _agg(all_ctrl, h) for h in HORIZONS}
    news_agg["pre"] = _agg(all_news, "pre")
    ctrl_agg["pre"] = _agg(all_ctrl, "pre")

    # Decay: share of the eventual 120-minute absolute move already spent by
    # each horizon. This is the number that prices a real-time feed.
    full = news_agg[str(max(HORIZONS))].get("mean_abs") or 0
    decay = {str(h): (round(100.0 * (news_agg[str(h)].get("mean_abs") or 0) / full, 1)
                      if full else None) for h in HORIZONS}

    # Excess over control, per horizon: the actual "does news do anything"
    # number. Anything near 1.0 means a headline is indistinguishable from a
    # randomly chosen moment in the same session.
    excess = {}
    for h in HORIZONS:
        n = news_agg[str(h)].get("mean_abs")
        c = ctrl_agg[str(h)].get("mean_abs")
        excess[str(h)] = round(n / c, 3) if (n and c) else None

    pre = news_agg["pre"].get("mean_abs") or 0
    post = news_agg[str(max(HORIZONS))].get("mean_abs") or 0
    runup_ratio = round(pre / post, 3) if post else None

    hour_line = ", ".join(f"{h:02d}:00={hours[h]}" for h in sorted(hours))
    in_band = sum(c for h, c in hours.items() if 9 <= h <= 16)
    total_h = sum(hours.values()) or 1

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result_schema": 1,
        "window": {"from": start.date().isoformat(), "to": end.date().isoformat(),
                   "days": days_back},
        "bar_minutes": BAR_MINUTES,
        "universe": universe,
        "per_symbol": per_symbol,
        "headlines_measured": len(all_news),
        "controls_measured": len(all_ctrl),
        "news": news_agg,
        "control": ctrl_agg,
        "excess_vs_control": excess,
        "decay_pct_of_120min": decay,
        "runup_vs_drift": {"pre_30min_mean_abs": round(pre, 3),
                           "post_120min_mean_abs": round(post, 3),
                           "ratio": runup_ratio},
        "timezone_check": {"by_hour": hours,
                           "pct_in_market_hours": round(100.0 * in_band / total_h, 1)},
        "api_bar_calls": budget["calls"],
    }

    # --- report ---
    log(f"news study: {len(all_news)} headlines measured against "
        f"{len(all_ctrl)} random in-session controls")
    log(f"  TIMEZONE CHECK: {out['timezone_check']['pct_in_market_hours']}% of "
        f"headline stamps fall in 09:00-16:00 -- if this is low, the bars and "
        f"the news are on different clocks and everything below is noise")
    log(f"    by hour: {hour_line}")
    log("  absolute move after a headline vs a random moment in the same session:")
    for h in HORIZONS:
        n, c = news_agg[str(h)], ctrl_agg[str(h)]
        log(f"    +{h:>3}min: news {n.get('mean_abs')}% (n={n.get('n')}) vs "
            f"control {c.get('mean_abs')}% -> {excess[str(h)]}x")
    log("  DIRECTION (the part you can actually trade):")
    for h in HORIZONS:
        n = news_agg[str(h)]
        log(f"    +{h:>3}min: mean signed {n.get('mean_signed')}%, "
            f"up {n.get('pct_up')}% of the time "
            f"(50% = a coin flip, and a coin flip is not a strategy)")
    log(f"  RUN-UP vs DRIFT: price moved {pre:.3f}% in the 30min BEFORE the "
        f"headline, {post:.3f}% in the 120min after (ratio {runup_ratio}) "
        f"-- a high ratio means the headline reports the move rather than causing it")
    log("  DECAY -- share of the 120-minute move already spent:")
    for h in HORIZONS:
        log(f"    by +{h:>3}min: {decay[str(h)]}%")

    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    return out


if __name__ == "__main__":
    run_study()
