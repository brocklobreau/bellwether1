"""
One-year walk-forward backtest of the bot's RISK ENGINE.

WHAT THIS TESTS, AND WHAT IT DOES NOT
=====================================

It tests: the exit framework (stop / target / trailing / deterioration), the
1%-risk position sizing, and the portfolio caps -- against a year of real
daily prices, benchmarked against equal-weight buy-and-hold of the same
universe over the same window.

It does NOT test the live bot's entry signal, and its result is NOT "where
the bot would be after a year". The live bot enters on `composite_score`,
which blends fundamentals, analyst consensus and news sentiment. None of
those can be reconstructed for a past date on this data plan -- FMP returns
only CURRENT ratios, targets and ratings. Backtesting with today's
fundamentals against last year's prices is lookahead bias: a stock that
doubled has a great-looking profile *now*, so "buying" it a year ago is
guaranteed to look brilliant and proves nothing. Rather than produce that
number, this restricts entries to the one signal family that genuinely can
be reconstructed point-in-time: technicals computed from closes up to and
including the decision day, never after it.

KNOWN LIMITATIONS, stated plainly because a backtest that hides these is
worse than no backtest:

  * SURVIVORSHIP BIAS. The universe is tickers that exist today. Companies
    that were delisted, acquired or wiped out during the window are absent,
    which flatters any result. The benchmark is computed over the same
    biased universe so the COMPARISON stays fair even though both numbers
    are optimistic in absolute terms.
  * COSTS ARE MODELLED, not ignored -- but they are an ESTIMATE. Every
    fill is charged COST_PER_SIDE_PCT against it (buys fill higher, sells
    fill lower). At ~160 trades a year this is the difference between a
    real edge and an imaginary one, which is why it is on by default rather
    than left as a footnote. Real slippage varies with size, liquidity and
    volatility, and is worse in fast markets than this flat estimate.
    The buy-and-hold benchmark is charged one round trip, not none, so the
    comparison stays fair rather than quietly favouring the strategy.
  * DAILY RESOLUTION. Stops are checked against daily closes, not intraday.
    A real stop would often fill worse (gap-downs) and occasionally better.
  * ONE PERIOD, ONE REGIME. A single year is a single sample. A strategy
    that wins in this window can lose in the next.

Point-in-time discipline is enforced structurally: the walk-forward loop
slices `closes[:i+1]` and every indicator is computed from that slice only,
so future data is not merely "not used" -- it is not reachable.

Run via the /run-backtest endpoint on the deployed service (this needs
network access to FMP, which the dev sandbox does not have).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from scripts import fmp_client as fmp
from lib.indicators import score_technical
# Import the REAL engine constants and sizer rather than reimplementing them,
# so this cannot silently drift from what the live bot actually does.
from lib.bot import (
    size_position, STARTING_EQUITY, RISK_PER_TRADE_PCT, MAX_POSITIONS,
    MAX_PER_SECTOR, INVEST_STOP_PCT, INVEST_TARGET_PCT,
    RATCHET_STEPS, SCORE_DROP_EXIT, THESIS_EXIT_MAX_GAIN_PCT,
)

RESULT_PATH = os.path.join(BASE, "results", "backtest.json")

# Fixed, sector-diversified, liquid universe. Deliberately hard-coded rather
# than pulled from today's screener: screening today and testing backwards
# would add a second layer of survivorship bias on top of the unavoidable one.
UNIVERSE = [
    ("AAPL", "Technology"), ("MSFT", "Technology"), ("NVDA", "Technology"),
    ("AVGO", "Technology"), ("ORCL", "Technology"), ("CRM", "Technology"),
    ("JPM", "Financial Services"), ("BAC", "Financial Services"),
    ("GS", "Financial Services"), ("V", "Financial Services"),
    ("JNJ", "Healthcare"), ("UNH", "Healthcare"), ("LLY", "Healthcare"),
    ("ABBV", "Healthcare"), ("MRK", "Healthcare"),
    ("XOM", "Energy"), ("CVX", "Energy"), ("COP", "Energy"), ("SLB", "Energy"),
    ("PG", "Consumer Defensive"), ("KO", "Consumer Defensive"),
    ("PEP", "Consumer Defensive"), ("COST", "Consumer Defensive"),
    ("HD", "Consumer Cyclical"), ("MCD", "Consumer Cyclical"),
    ("NKE", "Consumer Cyclical"), ("TSLA", "Consumer Cyclical"),
    ("CAT", "Industrials"), ("HON", "Industrials"), ("UNP", "Industrials"),
    ("RTX", "Industrials"), ("GE", "Industrials"),
    ("LIN", "Basic Materials"), ("FCX", "Basic Materials"), ("NEM", "Basic Materials"),
    ("NEE", "Utilities"), ("DUK", "Utilities"),
    ("AMT", "Real Estate"), ("PLD", "Real Estate"),
    ("VZ", "Communication Services"), ("GOOGL", "Communication Services"),
    ("META", "Communication Services"), ("DIS", "Communication Services"),
]

WARMUP_DAYS = 260          # trading days of history needed before the first decision
# technical_score gate -- the only reconstructable signal. Set from the
# scorer's ACTUAL observed range, not from intuition: lib.indicators
# dampens and re-centres its components, so scores cluster around 45-60 and
# a "looks selective" threshold of 62 is simply unreachable (measured: 0% of
# days clear it, which is how the first run produced zero trades). 55 sits
# near the 85th percentile -- selective, but attainable. Candidates are also
# ranked above this floor, so the floor sets admissibility and the ranking
# decides who actually gets the capital.
TECH_ENTRY_MIN = 55.0

# Round-trip execution cost, charged per side. 0.05% per side (0.10% round
# trip) is a reasonable estimate for liquid US large-caps at retail size:
# commissions are ~zero at most brokers now, so this is essentially spread
# plus slippage. A strategy turning over ~160 positions a year pays this
# ~160 times, so leaving it out would overstate returns by several points.
COST_PER_SIDE_PCT = 0.05
LOOKBACK_52W = 252


def fetch_history(symbol, start, end):
    """Daily closes OLDEST->NEWEST as (date, close) pairs."""
    rows = fmp.historical_price_full(symbol, start, end)
    out = []
    for r in rows:
        d, c = r.get("date"), r.get("close")
        if d and c:
            out.append((d[:10], float(c)))
    out.sort(key=lambda x: x[0])
    return out


def _exit_reason(pos, price, tech_score):
    """Identical rule set to lib.bot.check_exit, expressed against a daily
    bar -- including the ratcheted stop and the rule that a working trade is
    governed by price, not by thesis drift. Kept in lockstep deliberately:
    a backtest that tests different rules than the live bot is worthless."""
    entry = pos["entry_price"]
    pnl = (price - entry) / entry * 100.0
    pos["peak_pct"] = max(pos.get("peak_pct", 0.0), pnl)

    # Ratchet the stop upward through whatever milestones have been reached.
    for reached, lock_at in RATCHET_STEPS:
        if pos["peak_pct"] >= reached:
            new_stop = entry * (1 + lock_at / 100.0)
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
                pos["stop_locked"] = True
            break

    if price >= pos["target"]:
        return "target"
    if price <= pos["stop"]:
        return "trailing" if pos.get("stop_locked") else "stop"

    # A trade that is working is left to price alone.
    if pnl > THESIS_EXIT_MAX_GAIN_PCT:
        pos["decay_streak"] = 0
        return None

    if (pos["entry_tech"] is not None and tech_score is not None
            and tech_score <= pos["entry_tech"] - SCORE_DROP_EXIT):
        pos["decay_streak"] = pos.get("decay_streak", 0) + 1
        if pos["decay_streak"] >= 2:      # two consecutive daily reviews, as live
            return "deterioration"
    else:
        pos["decay_streak"] = 0
    return None


def load_series(days=365, universe=None, fetch=None, verbose=True):
    """Fetch every ticker's history ONCE. Split out from run_backtest so the
    sensitivity sweep reuses one download instead of re-pulling the whole
    universe per variant (7 variants x 43 tickers = 300 needless API calls)."""
    universe = universe or UNIVERSE
    fetch = fetch or fetch_history
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days + WARMUP_DAYS * 1.5))

    series, sectors = {}, {}
    for sym, sector in universe:
        try:
            h = fetch(sym, start.isoformat(), end.isoformat())
        except Exception as e:
            if verbose:
                print(f"  skip {sym}: {e}")
            continue
        if len(h) > WARMUP_DAYS + 30:
            series[sym] = h
            sectors[sym] = sector
    return series, sectors


def run_backtest(days=365, stop_pct=None, target_pct=None, universe=None,
                 fetch=None, verbose=True, series=None, sectors=None):
    """Walk forward one day at a time. `fetch` is injectable so the logic can
    be tested offline against synthetic series with no network; `series` lets
    a caller supply already-downloaded history."""
    stop_pct = INVEST_STOP_PCT if stop_pct is None else stop_pct
    target_pct = INVEST_TARGET_PCT if target_pct is None else target_pct

    if series is None:
        series, sectors = load_series(days=days, universe=universe,
                                      fetch=fetch, verbose=verbose)
    if not series:
        raise RuntimeError("no price history fetched -- cannot backtest")

    # A single shared calendar: every date any ticker traded on.
    all_dates = sorted({d for h in series.values() for d, _ in h})
    idx = {sym: {d: i for i, (d, _) in enumerate(h)} for sym, h in series.items()}
    test_dates = all_dates[WARMUP_DAYS:]
    if not test_dates:
        raise RuntimeError("not enough history for the requested window")

    cash = STARTING_EQUITY
    positions = {}
    closed = []
    curve = []

    for day in test_dates:
        # --- price + point-in-time technicals for this day only ---
        today = {}
        for sym, h in series.items():
            i = idx[sym].get(day)
            if i is None or i < WARMUP_DAYS:
                continue
            closes = [c for _, c in h[:i + 1]]        # <= today. Future unreachable.
            window = closes[-LOOKBACK_52W:]
            tech = score_technical(closes, price_52w_low=min(window), price_52w_high=max(window))
            today[sym] = (closes[-1], tech.get("technical_score"))

        # --- exits first, so freed cash is reusable the same day ---
        for sym in list(positions):
            if sym not in today:
                continue
            price, tscore = today[sym]
            pos = positions[sym]
            pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
            pos["peak_pct"] = max(pos["peak_pct"], pnl)
            reason = _exit_reason(pos, price, tscore)
            if reason:
                fill = price * (1 - COST_PER_SIDE_PCT / 100.0)   # sells fill lower
                cash += pos["shares"] * fill
                closed.append({
                    "ticker": sym, "sector": sectors[sym],
                    "entry_date": pos["entry_date"], "exit_date": day,
                    "entry_price": round(pos["entry_price"], 2), "exit_price": round(fill, 2),
                    "shares": pos["shares"], "reason": reason,
                    "pnl_pct": round((fill - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                    "pnl_dollars": round((fill - pos["entry_price"]) * pos["shares"], 2),
                    "peak_pct": round(pos["peak_pct"], 2),
                    "held_days": (datetime.fromisoformat(day) - datetime.fromisoformat(pos["entry_date"])).days,
                })
                del positions[sym]

        equity = cash + sum(p["shares"] * today.get(s, (p["last"], None))[0]
                            for s, p in positions.items())
        for s, p in positions.items():
            if s in today:
                p["last"] = today[s][0]

        # --- entries ---
        sector_counts = {}
        for s in positions:
            sector_counts[sectors[s]] = sector_counts.get(sectors[s], 0) + 1

        cands = []
        for sym, (price, tscore) in today.items():
            if sym in positions or tscore is None or tscore < TECH_ENTRY_MIN:
                continue
            cands.append((tscore, sym, price))
        cands.sort(reverse=True)

        for tscore, sym, price in cands:
            if len(positions) >= MAX_POSITIONS:
                break
            sec = sectors[sym]
            if sector_counts.get(sec, 0) >= MAX_PER_SECTOR:
                continue
            fill = price * (1 + COST_PER_SIDE_PCT / 100.0)      # buys fill higher
            stop = fill * (1 - stop_pct / 100.0)
            shares, risk_dollars, reject = size_position(equity, cash, fill, stop)
            if reject:
                continue
            cash -= shares * fill
            positions[sym] = {
                "shares": shares, "entry_price": fill, "entry_date": day,
                "stop": stop, "target": fill * (1 + target_pct / 100.0), "stop_locked": False,
                "peak_pct": 0.0, "entry_tech": tscore, "last": price, "decay_streak": 0,
            }
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        equity = cash + sum(p["shares"] * p["last"] for p in positions.values())
        curve.append({"date": day, "equity": round(equity, 2)})

    # --- benchmark: equal-weight buy & hold, same universe, same window ---
    first_day, last_day = test_dates[0], test_dates[-1]
    rets = []
    for sym, h in series.items():
        i0, i1 = idx[sym].get(first_day), idx[sym].get(last_day)
        if i0 is not None and i1 is not None and h[i0][1]:
            buy = h[i0][1] * (1 + COST_PER_SIDE_PCT / 100.0)
            sell = h[i1][1] * (1 - COST_PER_SIDE_PCT / 100.0)
            rets.append((sell / buy - 1) * 100)
    benchmark_pct = round(sum(rets) / len(rets), 2) if rets else None

    return _summarize(curve, closed, positions, cash, benchmark_pct,
                      first_day, last_day, stop_pct, target_pct, len(series))


def _summarize(curve, closed, positions, cash, benchmark_pct, first_day, last_day,
               stop_pct, target_pct, universe_size):
    final = curve[-1]["equity"] if curve else STARTING_EQUITY
    total_return = round((final / STARTING_EQUITY - 1) * 100, 2)

    peak, max_dd = STARTING_EQUITY, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        max_dd = min(max_dd, (pt["equity"] / peak - 1) * 100)

    wins = [t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in closed if t["pnl_pct"] <= 0]
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    realized_rr = round(avg_win / abs(avg_loss), 2) if (avg_win and avg_loss and avg_loss < 0) else None
    hit = round(100 * len(wins) / len(closed), 1) if closed else None
    expectancy = round(sum(t["pnl_pct"] for t in closed) / len(closed), 2) if closed else None

    reasons = {}
    for t in closed:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    held = [t["held_days"] for t in closed]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"from": first_day, "to": last_day, "universe_size": universe_size},
        "config": {"stop_pct": stop_pct, "target_pct": target_pct,
                   "risk_per_trade_pct": RISK_PER_TRADE_PCT,
                   "max_positions": MAX_POSITIONS, "starting_equity": STARTING_EQUITY,
                   "target_rr": round(target_pct / stop_pct, 2),
                   "cost_per_side_pct": COST_PER_SIDE_PCT,
                   "ratchet_steps": list(RATCHET_STEPS),
                   "thesis_exit_max_gain_pct": THESIS_EXIT_MAX_GAIN_PCT},
        "final_equity": round(final, 2),
        "total_return_pct": total_return,
        "benchmark_buy_hold_pct": benchmark_pct,
        "excess_vs_benchmark_pct": (round(total_return - benchmark_pct, 2)
                                    if benchmark_pct is not None else None),
        "max_drawdown_pct": round(max_dd, 2),
        "closed_trades": len(closed),
        "open_at_end": len(positions),
        "hit_rate_pct": hit,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "realized_rr": realized_rr,
        "expectancy_pct": expectancy,
        "avg_hold_days": round(sum(held) / len(held), 1) if held else None,
        "exit_reasons": reasons,
        "equity_curve": curve[::max(1, len(curve) // 300)],
        "trades": sorted(closed, key=lambda t: t["exit_date"], reverse=True)[:80],
    }


def run_and_save(days=365, sweep=True, universe=None, fetch=None):
    """Main entry point. Also runs a small stop/target sensitivity sweep --
    a single parameter set that looks good is usually luck; a whole
    neighbourhood that looks good is closer to a real effect.

    History is downloaded once and shared across every variant."""
    series, sectors = load_series(days=days, universe=universe, fetch=fetch)
    out = run_backtest(days=days, series=series, sectors=sectors)
    if sweep:
        variants = []
        for stop, target in ((10, 25), (8, 24), (12, 24), (10, 20), (10, 30), (6, 18)):
            try:
                r = run_backtest(days=days, stop_pct=stop, target_pct=target,
                                 series=series, sectors=sectors, verbose=False)
                variants.append({
                    "stop_pct": stop, "target_pct": target, "rr": round(target / stop, 2),
                    "return_pct": r["total_return_pct"], "max_dd_pct": r["max_drawdown_pct"],
                    "trades": r["closed_trades"], "hit_rate_pct": r["hit_rate_pct"],
                    "expectancy_pct": r["expectancy_pct"],
                })
            except Exception as e:
                variants.append({"stop_pct": stop, "target_pct": target, "error": str(e)[:120]})
        out["sensitivity"] = variants
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, RESULT_PATH)
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    res = run_and_save(days=n)
    print(json.dumps({k: v for k, v in res.items()
                      if k not in ("equity_curve", "trades")}, indent=2))
