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
HORIZONS = {"1d": timedelta(hours=20), "3d": timedelta(hours=68)}
GRACE = timedelta(hours=10)  # how much slop is allowed when looking for the "closest" snapshot to a horizon


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
                    closed_calls.append(_grade_call(cur, final=True))
                    del active[ticker]

        # Tickers that dropped out of this snapshot entirely (e.g. rotated out of the
        # discovered pool) are deliberately left in `active` rather than force-closed --
        # the discovered pool rotates day to day and a ticker may reappear in a later
        # snapshot. Anything still active when the loop ends surfaces below as an open call.

    open_calls = [_grade_call(c, final=False) for c in active.values()]
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


def _price_at_horizon(path, start_ts, horizon):
    """Closest path point to start_ts + horizon, within +/- GRACE. path is a
    list of (ts, price) tuples in chronological order. Returns (price, actual_ts)
    or (None, None) if nothing falls in the window yet."""
    target = _parse(start_ts) + horizon
    best = None
    best_delta = None
    for ts, price in path:
        delta = abs((_parse(ts) - target).total_seconds())
        if delta <= GRACE.total_seconds() and (best_delta is None or delta < best_delta):
            best, best_delta = (price, ts), delta
    return best if best else (None, None)


def _grade_call(call, final):
    direction = call["direction"]
    start_price = call["start_price"]
    path = call["path"]

    result = {
        "ticker": call["ticker"], "name": call.get("name"), "direction": direction,
        "start_ts": call["start_ts"], "start_price": start_price, "start_score": call["start_score"],
        "status": "closed" if final else "open",
    }
    for label, horizon in HORIZONS.items():
        price, at_ts = _price_at_horizon(path, call["start_ts"], horizon)
        if price is None:
            result[f"return_pct_{label}"] = None
            result[f"correct_{label}"] = None
            continue
        ret = (price - start_price) / start_price * 100 if direction == "long" else (start_price - price) / start_price * 100
        result[f"return_pct_{label}"] = round(ret, 2)
        result[f"correct_{label}"] = ret > 0
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
