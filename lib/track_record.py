"""
Turns the accumulated results/history/ snapshots into a track record: did
past BUY/SELL calls actually work out? No network, pure math over what's
already been saved by lib/save_results.save_run().

A "call" is defined as a signal entering BUY or SELL for a ticker (from
whatever it was before) and staying there until one of two things happens:

1. A real reversal -- a BUY call closes when the signal flips all the way
   to SELL (and vice versa). A fade to HOLD does NOT close the call -- HOLD
   just means conviction softened, not that the thesis reversed, and
   closing on every dip into HOLD was chopping single theses into several
   noisy, meaningless "calls" that didn't reflect how anyone would actually
   trade this (2026-08-25, user-requested: "make it as realistic and
   actually profitable as possible").
2. The stop-loss safety net below fires first.

STOP_LOSS_PCT exists because #1 alone is dangerous on its own: a stock can
crash while the *composite* score (fundamentals/sentiment/technicals
blended) still hasn't fully reversed to SELL, and a pure "wait for the
opposite signal" rule would ride that loss the entire way down. Real risk
management cuts a loser fast even if the longer thesis hasn't technically
flipped yet -- so any open call that's down past STOP_LOSS_PCT closes right
there, graded as a loss, independent of what the signal is doing. This is a
single tunable constant, not derived from your data -- adjust it if it's
firing on normal noise (too tight) or not protecting you (too loose).

Only lib/watchlist_results is used (a stable, consistent cohort run every
cycle); discovered_candidates rotate day to day and would make the history
noisy and hard to compare.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR = os.path.join(BASE, "results", "history")

GRADED_SIGNALS = {"BUY", "SELL"}
REVERSAL_OF = {"BUY": "SELL", "SELL": "BUY"}
STOP_LOSS_PCT = 10.0  # see module docstring -- a judgment call, not derived


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


def _unrealized_pct(call, price):
    start_price = call["start_price"]
    if not start_price:
        return None
    move_pct = (price - start_price) / start_price * 100
    # For a SELL call, being "down" means the price went UP against you.
    return move_pct if call["signal"] == "BUY" else -move_pct


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
        for r in results:
            ticker = r.get("ticker")
            signal = r.get("signal")
            price = r.get("price")
            score = r.get("composite_score")
            if not ticker or price is None:
                continue
            cur = active.get(ticker)

            # Stop-loss check happens first, against whatever call is open,
            # regardless of this cycle's signal -- a loser gets cut even if
            # the composite score hasn't caught up yet.
            if cur is not None:
                move = _unrealized_pct(cur, price)
                if move is not None and move <= -STOP_LOSS_PCT:
                    closed_calls.append(_close_call(cur, ts, price, reason="stop_loss"))
                    active[ticker] = None
                    cur = None

            if signal in GRADED_SIGNALS:
                if cur is None:
                    active[ticker] = {
                        "ticker": ticker, "name": r.get("name"), "signal": signal,
                        "start_ts": ts, "start_price": price, "start_score": score,
                        "last_ts": ts, "last_price": price,
                    }
                elif cur["signal"] == signal:
                    cur["last_ts"] = ts
                    cur["last_price"] = price
                elif signal == REVERSAL_OF.get(cur["signal"]):
                    # Real reversal (BUY -> SELL or SELL -> BUY) -- close the
                    # old call and open a new one in the opposite direction.
                    closed_calls.append(_close_call(cur, ts, price, reason="reversal"))
                    active[ticker] = {
                        "ticker": ticker, "name": r.get("name"), "signal": signal,
                        "start_ts": ts, "start_price": price, "start_score": score,
                        "last_ts": ts, "last_price": price,
                    }
            else:
                # HOLD (or NO DATA) -- conviction faded but the thesis hasn't
                # reversed. Leave the call open; just track the latest price
                # so an open call's "return so far" stays current.
                if cur is not None:
                    cur["last_ts"] = ts
                    cur["last_price"] = price

    open_calls = [c for c in active.values() if c is not None]
    open_calls = [_grade_open(c) for c in open_calls]
    closed_calls.sort(key=lambda c: c["end_ts"], reverse=True)
    open_calls.sort(key=lambda c: c["start_ts"], reverse=True)

    summary = _summarize(closed_calls)

    return {
        "insufficient_history": False,
        "snapshot_count": len(snapshots),
        "first_snapshot_at": snapshots[0][0],
        "latest_snapshot_at": snapshots[-1][0],
        "closed_calls": closed_calls,
        "open_calls": open_calls,
        "summary": summary,
    }


def _close_call(call, end_ts, end_price, reason):
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
        "correct": correct, "status": "closed", "close_reason": reason,
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
            "stop_loss_count": 0, "cumulative_return_pct": None,
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

    # Cumulative return: $1 into each call in the order they closed,
    # compounding -- an illustration of whether these calls' returns build
    # on each other or erode each other, NOT a real position-sized backtest
    # (it doesn't account for calls that were open on different tickers at
    # the same time the way an actual portfolio would).
    equity = 1.0
    for c in sorted(closed_calls, key=lambda c: c["end_ts"]):
        if c["return_pct"] is not None:
            equity *= (1 + c["return_pct"] / 100)
    cumulative_return_pct = round((equity - 1) * 100, 2)

    return {
        "total_closed": len(closed_calls),
        "hit_rate_pct": hit_rate(closed_calls),
        "buy_hit_rate_pct": hit_rate(buys),
        "sell_hit_rate_pct": hit_rate(sells),
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "stop_loss_count": sum(1 for c in closed_calls if c.get("close_reason") == "stop_loss"),
        "cumulative_return_pct": cumulative_return_pct,
    }
