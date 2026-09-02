"""
Turns the accumulated results/history/ snapshots into a track record: did
past BUY/SELL calls actually work out? No network, pure math over what's
already been saved by lib/save_results.save_run().

A "call" is a signal entering BUY or SELL for a ticker and staying there
until one of FIVE exits fires. They're checked in this order every cycle,
and the order matters -- risk first, then profit, then thesis:

1. STOP_LOSS_PCT (-10%) -- a hard loss cut, checked before anything else
   and independent of what the signal says. A stock can crash while the
   blended composite score hasn't reversed yet, and "wait for the opposite
   signal" would ride that all the way down.

2. TAKE_PROFIT_PCT (+25%) -- books a big win outright. Without this a
   winner could only ever be closed by a reversal or by giving gains back.

3. Trailing stop -- once a call has been up TRAILING_ACTIVATE_PCT (10%) at
   any point, it closes if it gives back TRAILING_GIVEBACK_FRAC (a third)
   of its PEAK gain. This is the one that fixes the worst flaw in the
   original design: the stop-loss measures from ENTRY, not from the peak,
   so a call that ran +50% and collapsed back to flat closed at ~0% having
   never booked a cent. Peak is tracked per call as it goes (2026-08-28,
   user-requested).

4. SCORE_DROP_EXIT (10 points below the score at entry) -- "the signals
   started looking bad" made mechanical. Deliberately NOT "the signal
   touched HOLD": a single dip into HOLD is noise, and closing on it was
   already tried and reverted (2026-08-25) because it chopped one thesis
   into several meaningless fragments. A sustained 10-point fall in the
   composite is real deterioration, and it fires while the signal may still
   nominally read BUY (2026-08-28, user-requested).

5. A real reversal -- BUY flips all the way to SELL, or vice versa. Closes
   the old call and opens a new one in the opposite direction. A fade to
   HOLD still does not close a call on its own; exit 4 is what catches a
   thesis quietly falling apart.

Every threshold above is a tunable judgment call, not derived from your
data. Widen them if they're firing on normal noise, tighten them if calls
are running too long before closing. Changing one re-grades the ENTIRE
history on the next run, since the whole track record is recomputed from
the saved snapshots -- so you can see immediately how a different rule set
would have performed on the same data.

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

# All five exits are judgment calls, not derived from the data. See the
# module docstring for what each one is for and why the order matters.
STOP_LOSS_PCT = 10.0            # hard loss cut, measured from entry
TAKE_PROFIT_PCT = 25.0          # book the win outright
TRAILING_ACTIVATE_PCT = 10.0    # trailing stop only arms once up this much
TRAILING_GIVEBACK_FRAC = 1 / 3  # ...then closes on giving back this much of PEAK gain
SCORE_DROP_EXIT = 10.0          # composite points below entry score = thesis deteriorating

# Editing watchlist.json does NOT reset this history -- past snapshots are
# immutable, so closed calls stay closed and graded. But a ticker you remove
# leaves its open call with no further price data, and without this it would
# sit in "Open calls" forever, frozen at a stale price, looking like a live
# position. After this many consecutive snapshots without the ticker, the
# call is closed at its LAST KNOWN price (the last point real data existed)
# rather than left hanging (2026-08-28).
UNTRACKED_CLOSE_AFTER = 3

CLOSE_REASON_LABEL = {
    "stop_loss": "stop-loss",
    "take_profit": "target hit",
    "trailing_stop": "trailing stop",
    "score_drop": "signal faded",
    "reversal": "reversal",
    "untracked": "removed from watchlist",
}


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
    """Return IN THE CALL'S OWN FAVOUR: positive = the call is winning.
    Every exit threshold is expressed against this, so short (SELL) calls
    get the same rules as long ones without any sign-flipping at the call
    sites."""
    start_price = call["start_price"]
    if not start_price:
        return None
    move_pct = (price - start_price) / start_price * 100
    # For a SELL call, being "down" means the price went UP against you.
    return move_pct if call["signal"] == "BUY" else -move_pct


def _exit_reason(call, move, score):
    """Which exit (if any) fires this cycle, checked worst-case first.
    `move` is the in-favour return from _unrealized_pct; `call['peak_pct']`
    is the best that return has ever been."""
    if move is None:
        return None
    if move <= -STOP_LOSS_PCT:
        return "stop_loss"
    if move >= TAKE_PROFIT_PCT:
        return "take_profit"
    peak = call.get("peak_pct") or 0.0
    if peak >= TRAILING_ACTIVATE_PCT and move <= peak * (1 - TRAILING_GIVEBACK_FRAC):
        return "trailing_stop"
    start_score = call.get("start_score")
    if start_score is not None and score is not None and score <= start_score - SCORE_DROP_EXIT:
        return "score_drop"
    return None


def _open_call(r, ticker, signal, ts, price, score):
    return {
        "ticker": ticker, "name": r.get("name"), "signal": signal,
        "start_ts": ts, "start_price": price, "start_score": score,
        "last_ts": ts, "last_price": price,
        "peak_pct": 0.0,  # best in-favour return seen so far; drives the trailing stop
    }


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
        seen_this_snapshot = set()
        for r in results:
            ticker = r.get("ticker")
            signal = r.get("signal")
            price = r.get("price")
            score = r.get("composite_score")
            if ticker:
                # Counts as "still in the cohort" even if this cycle's fetch
                # came back without a usable price -- one bad fetch is not a
                # removal from the watchlist.
                seen_this_snapshot.add(ticker)
            if not ticker or price is None:
                continue
            cur = active.get(ticker)

            # Price/score-based exits are checked first, against whatever
            # call is open and regardless of this cycle's signal -- a loser
            # gets cut, a winner gets booked, and a decaying thesis gets
            # closed even if the composite hasn't flipped to SELL yet.
            if cur is not None:
                move = _unrealized_pct(cur, price)
                if move is not None:
                    # Track the high-water mark BEFORE testing the trailing
                    # stop, so a call that peaks and reverses inside a single
                    # cycle is still measured against that peak.
                    cur["peak_pct"] = max(cur.get("peak_pct") or 0.0, move)
                reason = _exit_reason(cur, move, score)
                if reason:
                    closed_calls.append(_close_call(cur, ts, price, reason=reason))
                    active[ticker] = None
                    cur = None

            if signal in GRADED_SIGNALS:
                if cur is None:
                    active[ticker] = _open_call(r, ticker, signal, ts, price, score)
                elif cur["signal"] == signal:
                    cur["last_ts"] = ts
                    cur["last_price"] = price
                elif signal == REVERSAL_OF.get(cur["signal"]):
                    # Real reversal (BUY -> SELL or SELL -> BUY) -- close the
                    # old call and open a new one in the opposite direction.
                    closed_calls.append(_close_call(cur, ts, price, reason="reversal"))
                    active[ticker] = _open_call(r, ticker, signal, ts, price, score)
            else:
                # HOLD (or NO DATA) -- conviction faded but the thesis hasn't
                # reversed. Leave the call open; just track the latest price
                # so an open call's "return so far" stays current.
                if cur is not None:
                    cur["last_ts"] = ts
                    cur["last_price"] = price

        # Any open call whose ticker has now been absent from the cohort for
        # UNTRACKED_CLOSE_AFTER snapshots gets closed at its last known
        # price. Graded on the move it actually made while it was tracked --
        # not discarded (that would rewrite history every time the watchlist
        # is edited) and not left open forever against a stale price.
        for tkr, cur in list(active.items()):
            if cur is None:
                continue
            if tkr in seen_this_snapshot:
                cur["missing_snapshots"] = 0
                continue
            cur["missing_snapshots"] = cur.get("missing_snapshots", 0) + 1
            if cur["missing_snapshots"] >= UNTRACKED_CLOSE_AFTER:
                closed_calls.append(
                    _close_call(cur, cur["last_ts"], cur["last_price"], reason="untracked")
                )
                active[tkr] = None

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
        # pnl_pct is the return IN THE CALL'S FAVOUR: a SELL call that made
        # money has a negative return_pct (the price fell) but a positive
        # pnl_pct. Every profitability statistic must use this one -- using
        # raw return_pct counted winning shorts as losses and dragged the
        # cumulative-return figure down for being right (fixed 2026-08-28).
        "pnl_pct": round(ret_pct if call["signal"] == "BUY" else -ret_pct, 2)
                   if ret_pct is not None else None,
        "correct": correct, "status": "closed", "close_reason": reason,
        "close_reason_label": CLOSE_REASON_LABEL.get(reason, reason),
        "peak_pct": round(call.get("peak_pct") or 0.0, 2),
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
        "pnl_pct": round(ret_pct if call["signal"] == "BUY" else -ret_pct, 2)
                   if ret_pct is not None else None,
        "correct": correct_so_far, "status": "open",
        "peak_pct": round(call.get("peak_pct") or 0.0, 2),
    }


def target_risk_reward():
    """The reward:risk this system is CONFIGURED for, straight from the exit
    constants: +25% target against a -10% stop = 2.5:1. Paired with the hit
    rate it implies, this is the whole thesis of the thing in two numbers --
    you do not need to be right most of the time, you need the wins to be
    bigger than the losses by more than you are wrong."""
    rr = TAKE_PROFIT_PCT / STOP_LOSS_PCT
    return rr, breakeven_hit_rate(rr)


def breakeven_hit_rate(rr):
    """The hit rate at which a given reward:risk exactly breaks even.
    At 2.5:1 it's 28.6% -- below that you lose money however good the setups
    feel, above it you make money even while being wrong most of the time.
    This is the number that makes 'no one knows for sure' survivable."""
    if not rr or rr <= 0:
        return None
    return round(100 / (1 + rr), 1)


def _summarize(closed_calls):
    if not closed_calls:
        rr, be = target_risk_reward()
        return {
            "total_closed": 0, "hit_rate_pct": None,
            "buy_hit_rate_pct": None, "sell_hit_rate_pct": None,
            "avg_return_pct": None, "avg_win_pct": None, "avg_loss_pct": None,
            "stop_loss_count": 0, "cumulative_return_pct": None,
            "realized_rr": None, "expectancy_pct": None,
            "breakeven_hit_rate_pct": be, "edge_vs_breakeven_pct": None,
            "target_rr": round(rr, 2), "profitable": None,
        }

    def hit_rate(calls):
        graded = [c for c in calls if c["correct"] is not None]
        if not graded:
            return None
        return round(100 * sum(1 for c in graded if c["correct"]) / len(graded), 1)

    buys = [c for c in closed_calls if c["signal"] == "BUY"]
    sells = [c for c in closed_calls if c["signal"] == "SELL"]
    # All profitability math runs on pnl_pct (return in the call's favour),
    # never raw return_pct -- see the note in _close_call.
    pnls = [c["pnl_pct"] for c in closed_calls if c.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Cumulative return: $1 into each call in the order they closed,
    # compounding -- an illustration of whether these calls' returns build
    # on each other or erode each other, NOT a real position-sized backtest
    # (it doesn't account for calls that were open on different tickers at
    # the same time the way an actual portfolio would).
    equity = 1.0
    for c in sorted(closed_calls, key=lambda c: c["end_ts"]):
        if c.get("pnl_pct") is not None:
            equity *= (1 + c["pnl_pct"] / 100)
    cumulative_return_pct = round((equity - 1) * 100, 2)

    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None

    # --- Risk/reward scoreboard -------------------------------------------
    # realized_rr: how big the average win actually was against the average
    # loss. expectancy_pct: what one average call is worth -- the single
    # number that decides whether this makes money. Being right 68% of the
    # time is neither necessary nor sufficient; expectancy > 0 is both.
    realized_rr = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        realized_rr = round(avg_win / abs(avg_loss), 2)

    hit = hit_rate(closed_calls)
    expectancy_pct = round(sum(pnls) / len(pnls), 2) if pnls else None

    be = breakeven_hit_rate(realized_rr) if realized_rr else target_risk_reward()[1]
    edge = round(hit - be, 1) if (hit is not None and be is not None) else None

    return {
        "total_closed": len(closed_calls),
        "hit_rate_pct": hit,
        "buy_hit_rate_pct": hit_rate(buys),
        "sell_hit_rate_pct": hit_rate(sells),
        "avg_return_pct": expectancy_pct,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "stop_loss_count": sum(1 for c in closed_calls if c.get("close_reason") == "stop_loss"),
        "cumulative_return_pct": cumulative_return_pct,
        "realized_rr": realized_rr,
        "target_rr": round(target_risk_reward()[0], 2),
        "expectancy_pct": expectancy_pct,
        "breakeven_hit_rate_pct": be,
        "edge_vs_breakeven_pct": edge,
        "profitable": (expectancy_pct > 0) if expectancy_pct is not None else None,
    }
