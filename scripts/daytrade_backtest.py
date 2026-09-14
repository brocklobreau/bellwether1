"""
Day-trade strategy backtest -- does the day-trade SETUP SCORE actually pick
better than chance?
=========================================================================

Why this is a separate file from scripts/backtest.py
----------------------------------------------------
scripts/backtest.py is kept in deliberate lockstep with the INVEST side of
lib/bot.py: same exit rules, same ratchet, same universe of 43 mega-caps.
Bending it to also simulate a short-horizon strategy would mean bending the
thing whose job is to tell the truth about the live investing bot. So this
is its own module, its own universe, its own data shape, and it imports
nothing from the investing backtest.

What made this testable at all
------------------------------
On 2026-09-04 the TRADE side of the live bot was switched off, and the
comment explaining why is still in lib/bot.py: every backtest here runs on
daily bars, and a position opened and closed inside one day is invisible to
a daily bar, so the TRADE side "has been running on nothing but hope."

The spec for THIS bot removes that blocker. A trade that is allowed to hold
for a few sessions when it needs to is a trade daily bars can see. So the
whole measurement suite -- random-portfolio control, exit sweep,
walk-forward -- applies to it, which it never did to the old TRADE code.

The one question this file exists to answer
-------------------------------------------
Not "how much does it make". The headline return of any backtest over a
window that happened to be kind is close to meaningless. The question is:

    Does picking by day-trade score beat picking AT RANDOM from the same
    pool of volatile stocks, trading the same rules, over the same window?

That is the test the investing entry signal failed -- it landed in the
5.6th percentile of 500 coin-flip portfolios, i.e. worse than a monkey.
The day-trade score is built by the same hand-weighted method and has never
been put through it. Everything below is arranged so that comparison is
apples to apples: the random control draws from the SAME universe, trades
the SAME exits, pays the SAME costs, over the SAME sessions. Only the
choice of which names to hold differs.

Known biases, stated rather than buried
---------------------------------------
* SURVIVORSHIP. The universe is today's list of liquid-but-volatile names.
  Some of them are on it BECAUSE they did well. This inflates every
  absolute return in this file, the buy-and-hold benchmark included. It
  does NOT contaminate the headline comparison: the random control is drawn
  from the same biased list, so the bias cancels on the difference. Read
  the percentile, distrust the return.
* RECONSTRUCTED SCORE. Two of the eight components of the live
  lib/day_trade_score.py cannot be rebuilt from historical daily bars --
  `liquidity` (needs point-in-time market cap) and `catalyst` (needs the
  news feed as it looked that morning). The other six are reconstructed
  from the same inputs with the same weights, renormalised. So this tests
  80% of the live score by weight, not 100%. If the reconstructed score
  shows an edge, the live one might be better (it has a catalyst term) or
  worse (that term might be noise) -- but if the reconstructed score shows
  NO edge, the remaining 20% is a thin thing to pin a strategy on.
* SAME-BAR AMBIGUITY. When a session's low would have hit the stop and its
  high would have hit the target, a daily bar cannot say which came first.
  This file always assumes the STOP hit first. That is deliberately
  pessimistic; the alternative flatters every tight-target variant exactly
  where it matters most.

No lookahead
------------
The setup is scored on the CLOSE of day t using only bars up to and
including day t, and the position is opened at the OPEN of day t+1. Exits
are checked against each later session's own high/low. Nothing in the
simulation can see a price it would not have had.
"""
import json
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.indicators import macd, pct_change, range_position, rsi

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_PATH = os.path.join(BASE, "results", "daytrade_backtest.json")

STARTING_EQUITY = 100_000.0

# Own wallet, own slots. The investing bot is not touched by anything here;
# when this goes live it gets its own equity pool for the reason written up
# in lib/bot.py: two strategies sharing one wallet is not a design.
MAX_POSITIONS = 8
MAX_POSITION_PCT = 18.0       # cap on any one name's share of equity
RISK_PER_TRADE_PCT = 1.0      # % of equity lost if the stop hits
MAX_PER_SECTOR = 3

# Wider than the investing bot's 0.05%/side. These are more volatile names
# traded far more often, so spread and slippage are both worse and are paid
# many more times. Understating cost is the single easiest way to make a
# high-turnover strategy look profitable when it is not.
COST_PER_SIDE_PCT = 0.10

WARMUP_BARS = 60              # bars needed before the first score is meaningful


# --- universe -------------------------------------------------------------
#
# What the live day-trade screener actually surfaces, cycle after cycle:
# high-beta miners and crypto proxies, meme-liquid retail names, leveraged
# cyclicals, biotech, airlines -- not the mega-cap list the investing side
# trades. A day-trade universe of AAPL/JNJ/PG would be testing a strategy
# nobody proposed.
#
# Fixed rather than re-screened each day because the historical screener
# cannot be replayed: it needs point-in-time float, market cap and news,
# none of which are in a daily price file. A fixed list is the honest
# version of that limitation -- see the survivorship note at the top.
UNIVERSE = [
    # crypto / miners -- the volatility engine of the live scan pool
    ("MARA", "Crypto"), ("RIOT", "Crypto"), ("CLSK", "Crypto"),
    ("WULF", "Crypto"), ("HUT", "Crypto"), ("COIN", "Crypto"),
    # precious / industrial metals
    ("AG", "Materials"), ("HL", "Materials"), ("KGC", "Materials"),
    ("MP", "Materials"), ("FCX", "Materials"), ("UEC", "Materials"),
    # high-beta tech and semis
    ("AMD", "Technology"), ("MU", "Technology"), ("SMCI", "Technology"),
    ("PLTR", "Technology"), ("SOUN", "Technology"), ("AAOI", "Technology"),
    ("MRVL", "Technology"), ("ON", "Technology"),
    # retail / consumer momentum names
    ("GME", "Consumer"), ("CHWY", "Consumer"), ("CVNA", "Consumer"),
    ("RH", "Consumer"), ("W", "Consumer"), ("ULTA", "Consumer"),
    # travel and transport -- cyclical, headline-sensitive
    ("AAL", "Travel"), ("UAL", "Travel"), ("CCL", "Travel"),
    ("NCLH", "Travel"), ("ABNB", "Travel"),
    # energy
    ("OXY", "Energy"), ("DVN", "Energy"), ("HAL", "Energy"),
    ("EQT", "Energy"), ("PR", "Energy"),
    # biotech / healthcare movers
    ("MRNA", "Healthcare"), ("VKTX", "Healthcare"), ("EXAS", "Healthcare"),
    ("CRSP", "Healthcare"),
    # financial / fintech beta
    ("SOFI", "Financial"), ("UPST", "Financial"), ("HOOD", "Financial"),
    ("AFRM", "Financial"),
    # EV / industrial speculation
    ("RIVN", "Industrial"), ("LCID", "Industrial"), ("PLUG", "Industrial"),
    ("JOBY", "Industrial"),
]


# --- reconstructed day-trade setup score ----------------------------------
#
# The component scorers below are transcribed from lib/day_trade_score.py so
# the thing being tested is the thing that runs live, not a tidier cousin of
# it. Two of the eight components are unreconstructable from price history
# and are dropped; the remaining six keep their live weights and are
# renormalised over what is left (0.80 of the original total).
#
# NOTE, carried over faithfully including the wart: the live
# _inflection_score prose says "20-day range", but the value it is actually
# handed by lib.indicators.score_technical is range_position_pct, which is
# the 52-WEEK range position. The comment is wrong, not the code. This
# reconstruction matches the CODE, because lockstep with live behaviour is
# the whole point -- but that mislabel should be fixed in the live file.
LIVE_WEIGHTS = {
    "volatility": 0.15,
    "momentum": 0.15,
    "trend": 0.10,
    "inflection": 0.10,
    "relative_volume": 0.15,
    "extension": 0.15,
}
DROPPED_WEIGHT = 0.20         # liquidity (0.10) + catalyst (0.10)


def _volatility_score(v):
    if v is None:
        return None
    if v < 0.6:
        return 15
    if v < 1.2:
        return 45
    if v < 2.5:
        return 85
    if v < 5:
        return 95
    return 65


def _momentum_score(r):
    return None if r is None else min(100, abs(r - 50) * 2.4)


def _trend_score(macd_hist, mom10):
    if macd_hist is None or mom10 is None:
        return None
    agree = (macd_hist > 0 and mom10 > 0) or (macd_hist < 0 and mom10 < 0)
    return 90 if agree else 40


def _inflection_score(rp):
    return None if rp is None else min(100, abs(rp - 50) * 2.2)


def _relative_volume_score(volume, avg_volume):
    if volume is None or not avg_volume:
        return None
    rvol = volume / avg_volume
    if rvol < 0.7:
        return 20
    if rvol < 1.2:
        return 45
    if rvol < 2:
        return 75
    if rvol < 4:
        return 95
    return 88


def _extension_score(mom10):
    if mom10 is None:
        return None
    m = abs(mom10)
    if m < 15:
        return 85
    if m < 30:
        return 60
    if m < 60:
        return 30
    return 10


def _direction(macd_hist, mom10, r):
    """Transcribed from lib.day_trade_score.determine_direction."""
    if macd_hist is not None and mom10 is not None:
        if macd_hist > 0 and mom10 > 0:
            return "long"
        if macd_hist < 0 and mom10 < 0:
            return "short"
    if r is not None:
        return "long" if r >= 50 else "short"
    return "long"


def score_bars(bars, i):
    """Score the setup as it looked at the CLOSE of bars[i], using bars[:i+1]
    and nothing after. Returns (score, direction) or (None, direction)."""
    closes = [b["close"] for b in bars[:i + 1]]
    if len(closes) < WARMUP_BARS:
        return None, "long"
    # Bound the window. The live scorer is handed a ticker's whole history,
    # but every indicator here needs at most 252 bars (the 52-week range;
    # RSI needs 14, MACD 26+9, momentum 10). Passing the full series would
    # make each scoring O(len(history)) and the precompute O(n^2) across the
    # window -- minutes instead of seconds, for EMA differences in the
    # fourth decimal place, since a 26-period EMA is fully converged long
    # before 260 bars.
    closes = closes[-260:]

    price = closes[-1]
    window = closes[-252:]
    r = rsi(closes)
    _line, _sig, macd_hist = macd(closes)
    mom10 = pct_change(closes, 10)
    rp = range_position(price, min(window), max(window))

    moves = [abs(closes[k] - closes[k - 1]) / closes[k - 1] * 100
             for k in range(len(closes) - 20, len(closes)) if closes[k - 1]]
    vol_pct = round(statistics.mean(moves), 2) if moves else None

    vols = [b["volume"] for b in bars[max(0, i - 20):i] if b["volume"]]
    avg_vol = statistics.mean(vols) if vols else None

    parts = [
        ("volatility", _volatility_score(vol_pct)),
        ("momentum", _momentum_score(r)),
        ("trend", _trend_score(macd_hist, mom10)),
        ("inflection", _inflection_score(rp)),
        ("relative_volume", _relative_volume_score(bars[i]["volume"], avg_vol)),
        ("extension", _extension_score(mom10)),
    ]
    kept = [(LIVE_WEIGHTS[k], s) for k, s in parts if s is not None]
    direction = _direction(macd_hist, mom10, r)
    if not kept:
        return None, direction
    total_w = sum(w for w, _ in kept)
    return round(sum(w * s for w, s in kept) / total_w, 1), direction


# --- simulation -----------------------------------------------------------

def build_signals(series):
    """Precompute the setup score for every ticker on every bar, once.

    The score does not depend on which exit variant is being tested, so
    computing it inside the variant loop would redo the same ~25,000 scorings
    for each of twenty variants. Precomputing is not an optimisation detail
    here -- it is the difference between a run that finishes inside a refresh
    cycle and one that times out."""
    signals = {}
    for sym, bars in series.items():
        rows = []
        for i in range(len(bars)):
            if i < WARMUP_BARS:
                rows.append(None)
                continue
            score, direction = score_bars(bars, i)
            rows.append({"score": score, "direction": direction})
        signals[sym] = rows
    return signals


def _fill_exit(bar, level, is_stop):
    """Price a stop or target fill on a daily bar, honouring gaps.

    A stop is not a guarantee of its own price: if the session OPENS below
    the stop the position fills at the open, which is worse. Ignoring that
    is how a backtest quietly promises protection the market never offered.
    The mirror case applies to targets that gap up in our favour."""
    if is_stop:
        return min(bar["open"], level)
    return max(bar["open"], level)


def run_sim(signals, series, sectors, variant, entry_mode="score",
            threshold=70.0, date_from=None, date_to=None, seed=0,
            all_dates=None, idx=None):
    """One pass over the window under one exit variant and one pick rule.

    Ordering within a session, chosen to be realistic rather than flattering:
      1. Entries fill at the OPEN, decided by the PREVIOUS session's close.
      2. Exits are checked afterwards against this session's high/low, so a
         position opened this morning can still stop out this afternoon, but
         cash freed by an exit is NOT recycled into an entry the same day.
    """
    target_pct = variant["target_pct"]
    stop_pct = variant["stop_pct"]
    ladder = variant.get("ladder")
    breakeven = variant.get("breakeven_after_first", False)
    max_hold = variant.get("max_hold_sessions", 10)

    if all_dates is None:
        all_dates = sorted({b["date"] for bars in series.values() for b in bars})
    if idx is None:
        idx = {sym: {b["date"]: i for i, b in enumerate(bars)}
               for sym, bars in series.items()}

    test_dates = [d for d in all_dates
                  if (not date_from or d >= date_from)
                  and (not date_to or d <= date_to)]
    if len(test_dates) < 5:
        raise RuntimeError("not enough sessions in the requested window")

    rng = random.Random(seed)
    cash = STARTING_EQUITY
    positions = {}
    closed = []
    curve = []
    deploy = []
    entered = 0

    for di, day in enumerate(test_dates):
        prev_day = test_dates[di - 1] if di else None

        # --- entries, at today's open, on yesterday's signal ---
        if prev_day:
            sector_counts = {}
            for s in positions:
                sector_counts[sectors[s]] = sector_counts.get(sectors[s], 0) + 1

            cands = []
            for sym, bars in series.items():
                if sym in positions:
                    continue
                pi = idx[sym].get(prev_day)
                ti = idx[sym].get(day)
                if pi is None or ti is None:
                    continue
                sig = signals[sym][pi]
                if not sig:
                    continue
                if entry_mode == "random":
                    # The control: no score gate, no direction gate. Differs
                    # from the live rule ONLY in which names get picked.
                    cands.append((rng.random(), sym))
                    continue
                if sig["score"] is None or sig["direction"] != "long":
                    continue
                if entry_mode == "score":
                    if sig["score"] < threshold:
                        continue
                    cands.append((-sig["score"], sym))       # best first
                elif entry_mode == "any":
                    cands.append((rng.random(), sym))        # gate but no ranking
            cands.sort()

            equity_for_sizing = cash + sum(
                p["shares"] * p["last"] for p in positions.values())
            for _rank, sym in cands:
                if len(positions) >= MAX_POSITIONS:
                    break
                sec = sectors[sym]
                if sector_counts.get(sec, 0) >= MAX_PER_SECTOR:
                    continue
                bar = series[sym][idx[sym][day]]
                fill = bar["open"] * (1 + COST_PER_SIDE_PCT / 100.0)
                stop = fill * (1 - stop_pct / 100.0)
                risk_budget = equity_for_sizing * (RISK_PER_TRADE_PCT / 100.0)
                by_risk = risk_budget / max(fill - stop, 1e-9)
                by_weight = (equity_for_sizing * MAX_POSITION_PCT / 100.0) / fill
                by_cash = cash / fill
                shares = int(min(by_risk, by_weight, by_cash))
                if shares < 1:
                    continue
                cash -= shares * fill
                positions[sym] = {
                    "shares": shares, "orig_shares": shares, "entry_price": fill,
                    "entry_date": day, "stop": stop, "last": bar["open"],
                    "rungs_hit": 0, "peak_pct": 0.0, "held": 0, "realized": 0.0,
                }
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
                entered += 1

        # --- exits, against today's own range ---
        for sym in list(positions):
            ti = idx[sym].get(day)
            if ti is None:
                continue
            bar = series[sym][ti]
            pos = positions[sym]
            entry = pos["entry_price"]
            pos["held"] += 1
            pos["peak_pct"] = max(pos["peak_pct"],
                                  (bar["high"] - entry) / entry * 100.0)

            # Stop first, always. When a session's low would have stopped us
            # out AND its high would have hit the target, a daily bar cannot
            # say which came first, so this takes the loss. Assuming the win
            # instead is how tight-target variants get flattered into looking
            # like free money.
            if bar["low"] <= pos["stop"]:
                fill = _fill_exit(bar, pos["stop"], is_stop=True) * (1 - COST_PER_SIDE_PCT / 100.0)
                cash += pos["shares"] * fill
                pnl_d = pos["realized"] + (fill - entry) * pos["shares"]
                closed.append({
                    "ticker": sym, "sector": sectors[sym], "entry_date": pos["entry_date"],
                    "exit_date": day, "reason": "stop" if pos["rungs_hit"] == 0 else "trail",
                    "pnl_pct": round(pnl_d / (entry * pos["orig_shares"]) * 100, 2),
                    "pnl_dollars": round(pnl_d, 2), "held": pos["held"],
                    "peak_pct": round(pos["peak_pct"], 2),
                })
                del positions[sym]
                continue

            # Ladder rungs, lowest first, all reachable rungs in one session.
            rungs = ladder or ((target_pct, 1.0),)
            closed_out = False
            while pos["rungs_hit"] < len(rungs):
                gain_pct, frac = rungs[pos["rungs_hit"]]
                level = entry * (1 + gain_pct / 100.0)
                if bar["high"] < level:
                    break
                fill = _fill_exit(bar, level, is_stop=False) * (1 - COST_PER_SIDE_PCT / 100.0)
                sell = pos["orig_shares"] if frac >= 1.0 else int(pos["orig_shares"] * frac)
                sell = max(1, min(sell, pos["shares"]))
                cash += sell * fill
                pos["realized"] += (fill - entry) * sell
                pos["shares"] -= sell
                pos["rungs_hit"] += 1
                if breakeven and pos["shares"] > 0:
                    pos["stop"] = max(pos["stop"], entry)
                if pos["shares"] <= 0:
                    closed.append({
                        "ticker": sym, "sector": sectors[sym], "entry_date": pos["entry_date"],
                        "exit_date": day, "reason": "target",
                        "pnl_pct": round(pos["realized"] / (entry * pos["orig_shares"]) * 100, 2),
                        "pnl_dollars": round(pos["realized"], 2), "held": pos["held"],
                        "peak_pct": round(pos["peak_pct"], 2),
                    })
                    del positions[sym]
                    closed_out = True
                    break
            if closed_out:
                continue

            # Time stop. This is the "it can hold a bit if it needs to" rule
            # with a limit on how long "a bit" is allowed to become -- without
            # one, a short-horizon strategy silently turns into a portfolio of
            # broken trades nobody chose to own.
            if pos["held"] >= max_hold:
                fill = bar["close"] * (1 - COST_PER_SIDE_PCT / 100.0)
                cash += pos["shares"] * fill
                pnl_d = pos["realized"] + (fill - entry) * pos["shares"]
                closed.append({
                    "ticker": sym, "sector": sectors[sym], "entry_date": pos["entry_date"],
                    "exit_date": day, "reason": "time",
                    "pnl_pct": round(pnl_d / (entry * pos["orig_shares"]) * 100, 2),
                    "pnl_dollars": round(pnl_d, 2), "held": pos["held"],
                    "peak_pct": round(pos["peak_pct"], 2),
                })
                del positions[sym]
                continue

            pos["last"] = bar["close"]

        invested = sum(p["shares"] * p["last"] for p in positions.values())
        equity = cash + invested
        curve.append({"date": day, "equity": round(equity, 2)})
        deploy.append(100.0 * invested / equity if equity > 0 else 0.0)

    return _summarize(curve, closed, entered, test_dates, variant, deploy)


def _max_drawdown(curve):
    peak, worst = None, 0.0
    for pt in curve:
        e = pt["equity"]
        peak = e if peak is None else max(peak, e)
        if peak:
            worst = min(worst, (e - peak) / peak * 100.0)
    return round(worst, 2)


def _summarize(curve, closed, entered, test_dates, variant, deploy=None):
    final = curve[-1]["equity"] if curve else STARTING_EQUITY
    total_return = round((final / STARTING_EQUITY - 1) * 100, 2)
    wins = [c for c in closed if c["pnl_dollars"] > 0]
    losses = [c for c in closed if c["pnl_dollars"] <= 0]
    sessions = len(test_dates)
    years = sessions / 252.0 if sessions else 0

    gross_win = sum(c["pnl_dollars"] for c in wins)
    gross_loss = abs(sum(c["pnl_dollars"] for c in losses))

    return {
        "label": variant.get("label"),
        "total_return_pct": total_return,
        "annualized_pct": (round(((final / STARTING_EQUITY) ** (1 / years) - 1) * 100, 2)
                           if years > 0.2 and final > 0 else None),
        "max_drawdown_pct": _max_drawdown(curve),
        "trades": len(closed),
        "entered": entered,
        "still_open": entered - len(closed),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 1) if closed else None,
        "avg_win_pct": round(statistics.mean([c["pnl_pct"] for c in wins]), 2) if wins else None,
        "avg_loss_pct": round(statistics.mean([c["pnl_pct"] for c in losses]), 2) if losses else None,
        "avg_hold_sessions": round(statistics.mean([c["held"] for c in closed]), 1) if closed else None,
        # Profit factor is the number that actually decides whether a
        # high-turnover strategy survives costs: gross winnings divided by
        # gross losings. Below ~1.2 there is no room for a bad month.
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        # THE deployment-neutral number, and the reason it exists.
        #
        # Account return confounds two different things: how good the picks
        # were, and how much of the account was in the market. On a strategy
        # with negative expectancy, trading LESS produces a better account
        # return while picking exactly as badly -- measured here on synthetic
        # random-walk data, where the scored run "beat" the coin-flip control
        # by 12 points on account return while both arms had an identical
        # 43.7% win rate. It was not picking better. It was trading 20% less.
        #
        # avg_trade_pct is the average outcome of one trade, so a strategy
        # cannot score well on it by sitting in cash. Judge SELECTION on this
        # and on win rate; judge the STRATEGY on account return.
        "avg_trade_pct": round(statistics.mean([c["pnl_pct"] for c in closed]), 3) if closed else None,
        "avg_invested_pct": round(statistics.mean(deploy), 1) if deploy else None,
        "exit_mix": {r: sum(1 for c in closed if c["reason"] == r)
                     for r in ("target", "stop", "trail", "time")},
        "sessions": sessions,
        "first_day": test_dates[0] if test_dates else None,
        "last_day": test_dates[-1] if test_dates else None,
        "curve_points": len(curve),
    }


# --- exit variants --------------------------------------------------------
#
# The brief was "not greedy -- 2%, maybe 5%, maybe 8%, sell when it looks
# right". "When it looks right" is not testable and, on the investing side,
# turned out to be noise: walk-forward rank correlation between in-sample
# and out-of-sample exit tuning was 0.036, then went NEGATIVE (-0.4). So the
# sweep below is a set of concrete, falsifiable shapes covering that range,
# and a dumb fixed rule is included precisely so the clever ones have to
# beat something.
#
# ladder entries are (gain_pct, fraction_of_ORIGINAL_position_to_sell).
# A final entry with fraction 1.0 closes whatever remains.
VARIANTS = (
    # plain target/stop, no partials -- the baselines everything must beat
    {"label": "2% target / 2% stop", "target_pct": 2.0, "stop_pct": 2.0},
    {"label": "3% target / 3% stop", "target_pct": 3.0, "stop_pct": 3.0},
    {"label": "5% target / 3% stop", "target_pct": 5.0, "stop_pct": 3.0},
    {"label": "6% target / 3% stop", "target_pct": 6.0, "stop_pct": 3.0},
    {"label": "8% target / 4% stop", "target_pct": 8.0, "stop_pct": 4.0},
    {"label": "8% target / 3% stop", "target_pct": 8.0, "stop_pct": 3.0},
    {"label": "12% target / 5% stop", "target_pct": 12.0, "stop_pct": 5.0},
    # laddered partials -- "take some off, let some run"
    {"label": "half at 3%, rest at 6% / 3% stop", "target_pct": 6.0, "stop_pct": 3.0,
     "ladder": ((3.0, 0.5), (6.0, 1.0))},
    {"label": "half at 3%, rest at 6% / 3% stop, breakeven after first",
     "target_pct": 6.0, "stop_pct": 3.0, "ladder": ((3.0, 0.5), (6.0, 1.0)),
     "breakeven_after_first": True},
    {"label": "thirds at 2/5/8% / 3% stop, breakeven after first",
     "target_pct": 8.0, "stop_pct": 3.0,
     "ladder": ((2.0, 0.34), (5.0, 0.33), (8.0, 1.0)),
     "breakeven_after_first": True},
    {"label": "half at 5%, rest at 10% / 4% stop, breakeven after first",
     "target_pct": 10.0, "stop_pct": 4.0, "ladder": ((5.0, 0.5), (10.0, 1.0)),
     "breakeven_after_first": True},
)

# How long "it can hold it for a bit" is allowed to run, in sessions.
HOLD_SWEEP = (2, 3, 5, 10, 20)
DEFAULT_HOLD = 5

# Entry score floor. The live day-trade scorer calls >=70 a "Prime setup",
# which is what lib/day_trade_track_record.py already grades against, so
# that is the primary. The others test whether the threshold matters at all
# -- if every floor produces the same result, the score is not ranking
# anything and that IS the finding.
THRESHOLD_SWEEP = (55.0, 62.0, 70.0, 78.0)
DEFAULT_THRESHOLD = 70.0

# Draws for the random control. Each draw is a full simulation, not a
# buy-and-hold sample, so this is the expensive part of the run -- 150 is
# enough to place the strategy to within roughly one percentile point.
N_CONTROL_DRAWS = 150


def control_distribution(signals, series, sectors, variant, all_dates, idx,
                         date_from=None, date_to=None, n=N_CONTROL_DRAWS,
                         verbose=False):
    """Run the SAME rules with the pick rule replaced by a coin flip.

    This is the whole experiment. Everything else -- window, universe, exit
    ladder, position sizing, costs, slot limits -- is held identical, so any
    difference between the scored run and this distribution is attributable
    to the score and to nothing else."""
    out = {"return": [], "avg_trade": [], "win_rate": [], "trades": []}
    for s in range(n):
        try:
            r = run_sim(signals, series, sectors, variant, entry_mode="random",
                        seed=1000 + s, date_from=date_from, date_to=date_to,
                        all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        out["return"].append(r["total_return_pct"])
        if r["avg_trade_pct"] is not None:
            out["avg_trade"].append(r["avg_trade_pct"])
        if r["win_rate_pct"] is not None:
            out["win_rate"].append(r["win_rate_pct"])
        out["trades"].append(r["trades"])
    for k in out:
        out[k].sort()
    return out


def score_vs_control(result, ctl):
    """Both readings of the same comparison, so neither can hide the other."""
    return {
        "percentile_vs_random": percentile_of(ctl["return"], result["total_return_pct"]),
        "trade_percentile_vs_random": percentile_of(ctl["avg_trade"], result["avg_trade_pct"]),
        "control_median_pct": round(statistics.median(ctl["return"]), 2) if ctl["return"] else None,
        "control_median_trade_pct": round(statistics.median(ctl["avg_trade"]), 3) if ctl["avg_trade"] else None,
        "control_median_trades": round(statistics.median(ctl["trades"]), 0) if ctl["trades"] else None,
    }


def percentile_of(draws, value):
    """Where the strategy lands inside the coin-flip distribution. 50 means
    indistinguishable from chance; below 50 means the signal is actively
    costing money versus picking at random."""
    if not draws:
        return None
    below = sum(1 for d in draws if d < value)
    ties = sum(1 for d in draws if d == value)
    return round(100.0 * (below + 0.5 * ties) / len(draws), 1)


def _spearman(a, b):
    """Rank correlation, no scipy. Same implementation as the investing
    backtest uses for walk-forward."""
    if len(a) < 3 or len(a) != len(b):
        return None

    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((ra[i] - ma) ** 2 for i in range(n)) ** 0.5
    db = sum((rb[i] - mb) ** 2 for i in range(n)) ** 0.5
    return round(num / (da * db), 3) if da and db else None


# --- data -----------------------------------------------------------------

def fetch_bars(symbol, start, end):
    from scripts import fmp_client as fmp
    return fmp.historical_ohlcv(symbol, start, end)


def load_bars(days=730, universe=None, fetch=None, verbose=True):
    universe = universe or UNIVERSE
    fetch = fetch or fetch_bars
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days + 420))
    series, sectors = {}, {}
    for sym, sector in universe:
        try:
            bars = fetch(sym, start.isoformat(), end.isoformat())
        except Exception as e:
            if verbose:
                print(f"  skip {sym}: {e}")
            continue
        if len(bars) > WARMUP_BARS + 60:
            series[sym] = bars
            sectors[sym] = sector
        elif verbose:
            print(f"  skip {sym}: only {len(bars)} bars")
    return series, sectors


def buy_and_hold(series, first_day, last_day, idx):
    """Equal-weight buy-and-hold over the same universe and window. Inflated
    by the same survivorship bias as everything else here -- which is the
    point: it is the honest 'did you need a bot at all' line."""
    rets = []
    for sym, bars in series.items():
        i0, i1 = idx[sym].get(first_day), idx[sym].get(last_day)
        if i0 is None or i1 is None:
            continue
        buy = bars[i0]["open"] * (1 + COST_PER_SIDE_PCT / 100.0)
        sell = bars[i1]["close"] * (1 - COST_PER_SIDE_PCT / 100.0)
        if buy:
            rets.append((sell / buy - 1) * 100)
    return round(statistics.mean(rets), 2) if rets else None


# --- the whole experiment -------------------------------------------------

def run_all(days=730, universe=None, fetch=None, verbose=True,
            control_draws=N_CONTROL_DRAWS, sweep_draws=40):
    series, sectors = load_bars(days=days, universe=universe,
                                fetch=fetch, verbose=verbose)
    if not series:
        raise RuntimeError("no price history fetched -- cannot backtest")

    if verbose:
        print(f"  {len(series)} tickers loaded; precomputing setup scores...")
    signals = build_signals(series)
    all_dates = sorted({b["date"] for bars in series.values() for b in bars})
    idx = {sym: {b["date"]: i for i, b in enumerate(bars)}
           for sym, bars in series.items()}

    # Decisions start only after every ticker has enough history to be
    # scoreable, so early sessions are not silently a two-stock universe.
    test_dates = all_dates[WARMUP_BARS:]
    cutoff = len(all_dates) - int(days / 365.0 * 252)
    if cutoff > WARMUP_BARS:
        test_dates = all_dates[cutoff:]
    date_from, date_to = test_dates[0], test_dates[-1]

    base = dict(VARIANTS[7], max_hold_sessions=DEFAULT_HOLD)   # the laddered 3/6
    headline = run_sim(signals, series, sectors, base, entry_mode="score",
                       threshold=DEFAULT_THRESHOLD, date_from=date_from,
                       date_to=date_to, all_dates=all_dates, idx=idx)
    if verbose:
        print(f"  headline {headline['total_return_pct']:+.2f}% over "
              f"{headline['sessions']} sessions, {headline['trades']} trades")
        print(f"  running {control_draws} coin-flip control simulations...")
    draws = control_distribution(signals, series, sectors, base, all_dates, idx,
                                 date_from=date_from, date_to=date_to,
                                 n=control_draws)
    headline_cmp = score_vs_control(headline, draws)

    # --- exit-shape sweep, each against its OWN control ---
    variant_rows = []
    for v in VARIANTS:
        vv = dict(v, max_hold_sessions=DEFAULT_HOLD)
        try:
            r = run_sim(signals, series, sectors, vv, entry_mode="score",
                        threshold=DEFAULT_THRESHOLD, date_from=date_from,
                        date_to=date_to, all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        d = control_distribution(signals, series, sectors, vv, all_dates, idx,
                                 date_from=date_from, date_to=date_to,
                                 n=sweep_draws)
        r.update(score_vs_control(r, d))
        variant_rows.append(r)
        if verbose:
            print(f"    {v['label']}: {r['total_return_pct']:+.2f}% "
                  f"(pctile {r['percentile_vs_random']}, "
                  f"per-trade pctile {r['trade_percentile_vs_random']})")

    # --- how long to let it hold ---
    hold_rows = []
    for h in HOLD_SWEEP:
        vv = dict(base, max_hold_sessions=h, label=f"max hold {h} sessions")
        try:
            r = run_sim(signals, series, sectors, vv, entry_mode="score",
                        threshold=DEFAULT_THRESHOLD, date_from=date_from,
                        date_to=date_to, all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        d = control_distribution(signals, series, sectors, vv, all_dates, idx,
                                 date_from=date_from, date_to=date_to,
                                 n=sweep_draws)
        r.update(score_vs_control(r, d))
        hold_rows.append(r)

    # --- does the score threshold do anything? ---
    threshold_rows = []
    for t in THRESHOLD_SWEEP:
        vv = dict(base, label=f"score >= {t:.0f}")
        try:
            r = run_sim(signals, series, sectors, vv, entry_mode="score",
                        threshold=t, date_from=date_from, date_to=date_to,
                        all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        r["threshold"] = t
        r.update(score_vs_control(r, draws))
        threshold_rows.append(r)

    # "any": clears the score gate but picks randomly among those that clear.
    # Separates "the floor filters usefully" from "the ranking ranks usefully".
    gate_only = run_sim(signals, series, sectors, base, entry_mode="any",
                        threshold=DEFAULT_THRESHOLD, seed=7,
                        date_from=date_from, date_to=date_to,
                        all_dates=all_dates, idx=idx)
    gate_only["label"] = "score gate, random pick above it"
    gate_only.update(score_vs_control(gate_only, draws))

    # --- sub-period robustness ------------------------------------------
    #
    # The hardest-won lesson from the investing side. A single window
    # produced a percentile of 4.4, then the same idea on a different split
    # produced 14.4, and only cutting the window into four consecutive
    # periods -- each scored against coin flips drawn from THAT period --
    # showed what was actually going on. One percentile is one sample. A
    # strategy that beats chance in one period out of four has not beaten
    # chance; it has been lucky once, and reporting that single number as
    # the finding is how a backtest lies without stating a single falsehood.
    n_periods = 4
    size = len(test_dates) // n_periods
    period_rows = []
    for k in range(n_periods):
        p_from = test_dates[k * size]
        p_to = test_dates[(k + 1) * size - 1] if k < n_periods - 1 else test_dates[-1]
        try:
            r = run_sim(signals, series, sectors, base, entry_mode="score",
                        threshold=DEFAULT_THRESHOLD, date_from=p_from,
                        date_to=p_to, all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        d = control_distribution(signals, series, sectors, base, all_dates, idx,
                                 date_from=p_from, date_to=p_to, n=sweep_draws)
        row = {
            "period": k + 1, "from": p_from, "to": p_to,
            "return_pct": r["total_return_pct"], "avg_trade_pct": r["avg_trade_pct"],
            "trades": r["trades"], "win_rate_pct": r["win_rate_pct"],
            "profit_factor": r["profit_factor"],
            "avg_invested_pct": r["avg_invested_pct"],
        }
        row.update(score_vs_control(r, d))
        period_rows.append(row)
        if verbose:
            print(f"    period {k + 1} ({p_from}..{p_to}): "
                  f"{r['total_return_pct']:+.2f}% "
                  f"(pctile {period_rows[-1]['percentile_vs_random']})")
    pcts = [p["percentile_vs_random"] for p in period_rows
            if p["percentile_vs_random"] is not None]
    period_summary = {
        "periods": period_rows,
        "avg_percentile": round(statistics.mean(pcts), 1) if pcts else None,
        "beat_chance_in": sum(1 for p in pcts if p > 50),
        "of": len(pcts),
    }

    # --- walk-forward: does picking the best exit in-sample help out-of-sample? ---
    mid = test_dates[len(test_dates) // 2]
    wf = []
    for v in VARIANTS:
        vv = dict(v, max_hold_sessions=DEFAULT_HOLD)
        try:
            a = run_sim(signals, series, sectors, vv, entry_mode="score",
                        threshold=DEFAULT_THRESHOLD, date_from=date_from,
                        date_to=mid, all_dates=all_dates, idx=idx)
            b = run_sim(signals, series, sectors, vv, entry_mode="score",
                        threshold=DEFAULT_THRESHOLD, date_from=mid,
                        date_to=date_to, all_dates=all_dates, idx=idx)
        except RuntimeError:
            continue
        wf.append({"label": v["label"],
                   "train_pct": a["total_return_pct"],
                   "test_pct": b["total_return_pct"]})
    wf_corr = _spearman([r["train_pct"] for r in wf],
                        [r["test_pct"] for r in wf]) if len(wf) >= 3 else None
    if wf:
        best = max(wf, key=lambda r: r["train_pct"])
        ranked = sorted(wf, key=lambda r: r["test_pct"], reverse=True)
        best_rank = next(i + 1 for i, r in enumerate(ranked)
                         if r["label"] == best["label"])
    else:
        best, best_rank = None, None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result_schema": 1,
        "window": {"from": date_from, "to": date_to,
                   "sessions": len(test_dates), "days_requested": days},
        # Two different numbers on purpose. universe_size is how many
        # tickers actually returned usable history this run; universe_declared
        # is how many are in the list. The cache key must compare against the
        # DECLARED size -- keying on the loaded count would mark the cache
        # stale every time a single ticker's fetch hiccuped, re-running a
        # multi-minute job on every cycle forever.
        "universe_size": len(series),
        "universe_declared": len(UNIVERSE),
        "starting_equity": STARTING_EQUITY,
        "costs_per_side_pct": COST_PER_SIDE_PCT,
        "dropped_score_weight": DROPPED_WEIGHT,
        "headline": dict(headline, label=base.get("label"),
                         threshold=DEFAULT_THRESHOLD,
                         max_hold_sessions=DEFAULT_HOLD, **headline_cmp),
        "control": {
            "draws": len(draws["return"]),
            "median_pct": round(statistics.median(draws["return"]), 2) if draws["return"] else None,
            "p10_pct": round(draws["return"][int(len(draws["return"]) * 0.10)], 2) if draws["return"] else None,
            "p90_pct": round(draws["return"][int(len(draws["return"]) * 0.90)], 2) if draws["return"] else None,
            "median_trade_pct": round(statistics.median(draws["avg_trade"]), 3) if draws["avg_trade"] else None,
            "median_win_rate_pct": round(statistics.median(draws["win_rate"]), 1) if draws["win_rate"] else None,
            "median_trades": round(statistics.median(draws["trades"]), 0) if draws["trades"] else None,
        },
        "buy_and_hold_pct": buy_and_hold(series, date_from, date_to, idx),
        "variants": variant_rows,
        "hold_sweep": hold_rows,
        "threshold_sweep": threshold_rows,
        "gate_only": gate_only,
        "period_robustness": period_summary,
        "walk_forward": {"rows": wf, "rank_correlation": wf_corr,
                         "best_in_sample": best["label"] if best else None,
                         "its_out_of_sample_rank": best_rank,
                         "of": len(wf)},
    }


def run_and_save(days=730, universe=None, fetch=None, verbose=True, **kw):
    res = run_all(days=days, universe=universe, fetch=fetch,
                  verbose=verbose, **kw)
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        json.dump(res, f, indent=2)
    return res


if __name__ == "__main__":
    out = run_and_save(days=int(sys.argv[1]) if len(sys.argv) > 1 else 730)
    h = out["headline"]
    print(json.dumps({"headline": h["total_return_pct"],
                      "percentile": h["percentile_vs_random"],
                      "control_median": out["control"]["median_pct"],
                      "buy_and_hold": out["buy_and_hold_pct"]}, indent=2))
