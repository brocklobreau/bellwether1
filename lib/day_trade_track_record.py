"""
Day-trade-specific track record: grades "Prime setup" (day_trade_score >= 70)
calls against what price actually did ~1 day and ~3 days later -- the
horizon a day trade is meant to resolve in, unlike the main Track Record
(lib/track_record.py), which grades BUY/SELL composite calls over however
long the signal happens to stay changed (weeks, sometimes). Two different
tools for two different holding periods; neither replaces the other.

A "call" starts the moment a ticker's day_trade_score crosses UP into
Prime-setup territory (>=70) having not been there in the immediately
preceding snapshot -- so a ticker that stays elevated for several hourly
runs in a row only counts once, not once per snapshot. Direction (long vs
short) comes from day_trade_direction at the moment the call started.
Watchlist tickers AND discovered/screened day-trade candidates both feed
this (unlike the main track record, which is watchlist-only) -- day-trade
setups are inherently a rotating cohort, so excluding discovered candidates
would throw away most of the real data.

No network, pure math over results/history/ snapshots already saved by
lib.save_results.save_run().
"""
from datetime import datetime, timedelta

from lib.track_record import _load_snapshots

THRESHOLD = 70

# Horizons in TRADING SESSIONS, not wall-clock hours.
#
# This used to be {"1d": 20h, "3d": 68h} matched against the call's own price
# path within a +/-10h grace window, and it graded essentially nothing --
# every return rendered as a dash. Two independent reasons, both fatal:
#
#   1. The path only contains snapshots where the ticker was STILL scoring
#      >= 70. A day-trade setup decays within hours by construction, so the
#      path typically spans 1-3 hours and never reaches +20h. The price 20
#      hours later was sitting in the snapshot history the whole time; the
#      grader just wasn't allowed to look at it.
#   2. Wall-clock hours ignore the weekend. A Friday-afternoon call's +20h
#      window lands on Saturday, when no snapshot exists, so Thursday-late
#      and Friday calls could never resolve at 1d even in principle.
#
# Sessions fix both: "1 day later" means the next day the market was open,
# graded at that session's last snapshot. (2026-09-04)
HORIZON_SESSIONS = {"1d": 1, "3d": 3}


def _parse(ts):
    return datetime.fromisoformat(ts)


def _all_scored(snap):
    """Every entry from this snapshot carrying a day_trade_score, watchlist
    and discovered candidates alike."""
    out = list(snap.get("watchlist_results", []))
    out.extend(snap.get("discovered_candidates", []))
    return out


def build_day_trade_track_record():
    snapshots = _load_snapshots()

    if len(snapshots) < 2:
        return {"insufficient_history": True, "snapshot_count": len(snapshots), "closed_calls": [], "open_calls": [], "summary": None}

    prices, sessions = _build_price_index(snapshots)

    active = {}  # ticker -> in-progress call, or absent if not currently >=70
    closed_calls = []

    for ts, snap in snapshots:
        seen = set()
        for r in _all_scored(snap):
            ticker = r.get("ticker")
            score = r.get("day_trade_score")
            price = r.get("price")
            if not ticker or price is None or score is None:
                continue
            seen.add(ticker)
            is_prime = score >= THRESHOLD
            cur = active.get(ticker)

            if is_prime:
                if cur is None:
                    active[ticker] = {
                        "ticker": ticker, "name": r.get("name"),
                        "direction": r.get("day_trade_direction", "long"),
                        "start_ts": ts, "start_price": price, "start_score": score,
                        "path": [(ts, price)],
                    }
                else:
                    cur["path"].append((ts, price))
            else:
                if cur is not None:
                    closed_calls.append(_grade_call(cur, final=True, prices=prices, sessions=sessions))
                    del active[ticker]

        # Tickers that dropped out of this snapshot entirely (e.g. rotated out of the
        # discovered pool) are deliberately left in `active` rather than force-closed --
        # the discovered pool rotates day to day and a ticker may reappear in a later
        # snapshot. Anything still active when the loop ends surfaces below as an open call.

    open_calls = [_grade_call(c, final=False, prices=prices, sessions=sessions)
                  for c in active.values()]
    closed_calls.sort(key=lambda c: c["start_ts"], reverse=True)
    open_calls.sort(key=lambda c: c["start_ts"], reverse=True)

    summary = _summarize(closed_calls)

    return {
        "insufficient_history": False,
        "snapshot_count": len(snapshots),
        "closed_calls": closed_calls,
        "open_calls": open_calls,
        "summary": summary,
    }


def _build_price_index(snapshots):
    """ticker -> {session date: (last ts that session, last price that session)}.

    Built from EVERY snapshot, independent of score. A call is graded against
    what the price actually did afterwards, which has nothing to do with
    whether the setup was still scoring highly at that later moment -- that
    conflation is what made the old grader return nothing.

    Also returns the ordered list of session dates, so a horizon of "1 day"
    means the next session rather than a fixed number of hours."""
    prices = {}
    sessions = set()
    for ts, snap in snapshots:
        date = ts[:10]
        sessions.add(date)
        for r in _all_scored(snap):
            ticker, price = r.get("ticker"), r.get("price")
            if not ticker or price is None:
                continue
            per_ticker = prices.setdefault(ticker, {})
            prev = per_ticker.get(date)
            # Keep the LAST reading of each session -- closest to the close,
            # and deterministic regardless of how many cycles ran that day.
            if prev is None or ts >= prev[0]:
                per_ticker[date] = (ts, price)
    return prices, sorted(sessions)


def _price_after_sessions(prices, sessions, ticker, start_ts, n_sessions):
    """Price for `ticker` n sessions after the call started.

    Returns (price, ts, status) where status is:
      "ok"      -- graded
      "pending" -- that session has not happened yet; check back later
      "no_data" -- the session exists but the ticker wasn't in it (it rotated
                   out of the discovered pool), so it can never be graded
    """
    start_date = start_ts[:10]
    try:
        i = sessions.index(start_date)
    except ValueError:
        return None, None, "no_data"
    j = i + n_sessions
    if j >= len(sessions):
        return None, None, "pending"
    target_date = sessions[j]
    hit = (prices.get(ticker) or {}).get(target_date)
    if not hit:
        return None, None, "no_data"
    return hit[1], hit[0], "ok"


def _grade_call(call, final, prices=None, sessions=None):
    direction = call["direction"]
    start_price = call["start_price"]
    path = call["path"]

    result = {
        "ticker": call["ticker"], "name": call.get("name"), "direction": direction,
        "start_ts": call["start_ts"], "start_price": start_price, "start_score": call["start_score"],
        "status": "closed" if final else "open",
    }
    for label, n in HORIZON_SESSIONS.items():
        price, at_ts, status = _price_after_sessions(
            prices or {}, sessions or [], call["ticker"], call["start_ts"], n)
        result[f"status_{label}"] = status
        if price is None:
            result[f"return_pct_{label}"] = None
            result[f"correct_{label}"] = None
            continue
        ret = ((price - start_price) / start_price * 100 if direction == "long"
               else (start_price - price) / start_price * 100)
        result[f"return_pct_{label}"] = round(ret, 2)
        result[f"correct_{label}"] = ret > 0
        result[f"graded_at_{label}"] = at_ts
    return result


def _summarize(closed_calls):
    empty = {"total_closed": 0, "hit_rate_1d_pct": None, "hit_rate_3d_pct": None,
              "avg_return_1d_pct": None, "avg_return_3d_pct": None,
              "long_count": 0, "short_count": 0}
    if not closed_calls:
        return empty

    def hit_rate(label):
        graded = [c for c in closed_calls if c.get(f"correct_{label}") is not None]
        if not graded:
            return None
        return round(100 * sum(1 for c in graded if c[f"correct_{label}"]) / len(graded), 1)

    def avg_return(label):
        vals = [c[f"return_pct_{label}"] for c in closed_calls if c.get(f"return_pct_{label}") is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "total_closed": len(closed_calls),
        "hit_rate_1d_pct": hit_rate("1d"),
        "hit_rate_3d_pct": hit_rate("3d"),
        "avg_return_1d_pct": avg_return("1d"),
        "avg_return_3d_pct": avg_return("3d"),
        "long_count": sum(1 for c in closed_calls if c["direction"] == "long"),
        "short_count": sum(1 for c in closed_calls if c["direction"] == "short"),
    }
