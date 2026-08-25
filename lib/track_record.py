"""
Turns the accumulated results/history/ snapshots into a track record: did
past BUY/SELL calls actually work out? No network, pure math over what's
already been saved by lib/save_results.save_run().

A "call" is defined as a signal entering BUY or SELL for a ticker (from
whatever it was before) and staying there until it changes again. HOLD isn't
graded -- it's not a directional bet. Only lib/watchlist_results is used
(a stable, consistent cohort run every hour); discovered_candidates rotate
day to day and would make the history noisy and hard to compare.

A call is CLOSED once the signal moves off BUY/SELL to something else --
graded against the price at the moment it closed. A call still showing the
same signal as of the latest snapshot is OPEN -- graded provisionally
against the latest known price, and can still flip either way.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR = os.path.join(BASE, "results", "history")

GRADED_SIGNALS = {"BUY", "SELL"}


def _load_snapshots():
    if not os.path.isdir(HISTORY_DIR):
        return []
    snaps = []
    for fname in sorted(os.listdir(HISTORY_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(HISTORY_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        ts = data.get("generated_at")
        if not ts:
            continue
        snaps.append((ts, data))
    snaps.sort(key=lambda x: x[0])
    return snaps


def build_track_record():
    snapshots = _load_snapshots()

    if len(snapshots) < 2:
        return {
            "insufficient_history": True,
            "snapshot_count": len(snapshots),
            "closed_calls": [],
            "open_calls": [],
            "summary": None,
        }

    # ticker -> in-progress call dict, or None
    active = {}
    closed_calls = []

    for ts, snap in snapshots:
        results = snap.get("watchlist_results", [])
        seen_tickers = set()
        for r in results:
            ticker = r.get("ticker")
            signal = r.get("signal")
            price = r.get("price")
            score = r.get("composite_score")
            if not ticker or price is None:
                continue
            seen_tickers.add(ticker)
            cur = active.get(ticker)

            if signal in GRADED_SIGNALS:
                if cur is None or cur["signal"] != signal:
                    # a new call starts -- close out whatever was open first
                    if cur is not None:
                        closed_calls.append(_close_call(cur, ts, price))
                    active[ticker] = {
                        "ticker": ticker, "name": r.get("name"), "signal": signal,
                        "start_ts": ts, "start_price": price, "start_score": score,
                        "last_ts": ts, "last_price": price,
                    }
                else:
                    cur["last_ts"] = ts
                    cur["last_price"] = price
            else:
                if cur is not None:
                    closed_calls.append(_close_call(cur, ts, price))
                    active[ticker] = None

    open_calls = [c for c in active.values() if c is not None]
    open_calls = [_grade_open(c) for c in open_calls]
    closed_calls.sort(key=lambda c: c["end_ts"], reverse=True)
    open_calls.sort(key=lambda c: c["start_ts"], reverse=True)

    graded = [c for c in closed_calls]  # every closed call has a real outcome
    summary = _summarize(graded)

    return {
        "insufficient_history": False,
        "snapshot_count": len(snapshots),
        "first_snapshot_at": snapshots[0][0],
        "latest_snapshot_at": snapshots[-1][0],
        "closed_calls": closed_calls,
        "open_calls": open_calls,
        "summary": summary,
    }


def _close_call(call, end_ts, end_price):
    start_price = call["start_price"]
    ret_pct = (end_price - start_price) / start_price * 100 if start_price else None
    correct = None
    if ret_pct is not None:
        correct = (ret_pct > 0) if call["signal"] == "BUY" else (ret_pct < 0)
    return {
        "ticker": call["ticker"], "name": call.get("name"), "signal": call["signal"],
        "start_ts": call["start_ts"], "start_price": start_price,
        "end_ts": end_ts, "end_price": end_price,
        "return_pct": round(ret_pct, 2) if ret_pct is not None else None,
        "correct": correct, "status": "closed",
    }


def _grade_open(call):
    start_price = call["start_price"]
    last_price = call["last_price"]
    ret_pct = (last_price - start_price) / start_price * 100 if start_price else None
    correct_so_far = None
    if ret_pct is not None:
        correct_so_far = (ret_pct > 0) if call["signal"] == "BUY" else (ret_pct < 0)
    return {
        "ticker": call["ticker"], "name": call.get("name"), "signal": call["signal"],
        "start_ts": call["start_ts"], "start_price": start_price,
        "end_ts": call["last_ts"], "end_price": last_price,
        "return_pct": round(ret_pct, 2) if ret_pct is not None else None,
        "correct": correct_so_far, "status": "open",
    }


def _summarize(closed_calls):
    if not closed_calls:
        return {
            "total_closed": 0, "hit_rate_pct": None,
            "buy_hit_rate_pct": None, "sell_hit_rate_pct": None,
            "avg_return_pct": None, "avg_win_pct": None, "avg_loss_pct": None,
        }

    def hit_rate(calls):
        graded = [c for c in calls if c["correct"] is not None]
        if not graded:
            return None
        return round(100 * sum(1 for c in graded if c["correct"]) / len(graded), 1)

    buys = [c for c in closed_calls if c["signal"] == "BUY"]
    sells = [c for c in closed_calls if c["signal"] == "SELL"]
    returns = [c["return_pct"] for c in closed_calls if c["return_pct"] is not None]
    wins = [c["return_pct"] for c in closed_calls if c["correct"] is True]
    losses = [c["return_pct"] for c in closed_calls if c["correct"] is False]

    return {
        "total_closed": len(closed_calls),
        "hit_rate_pct": hit_rate(closed_calls),
        "buy_hit_rate_pct": hit_rate(buys),
        "sell_hit_rate_pct": hit_rate(sells),
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
    }
