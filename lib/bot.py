"""
Bellwether's paper-trading bot: a simulated portfolio that opens and closes
positions automatically from the same signals the dashboard already scores,
under strict risk rules. Added 2026-08-28 at user request.

READ THIS FIRST -- WHAT THIS IS AND ISN'T

It is PAPER trading. No brokerage is connected, no order is ever placed, no
real money moves. It starts with a simulated STARTING_EQUITY and marks
itself to market every cycle.

It is FORWARD-tested, not backtested. Every decision is made from the data
available at that cycle and written down immediately, so there is no
lookahead and nothing can be curve-fit after the fact. That also means it
starts with no record and has to earn one in real time -- a backtest could
show you a beautiful equity curve this afternoon, and it would be worth
nothing. This will take months to say anything meaningful.

It is NOT guaranteed to be profitable, and no amount of engineering makes
it so. What the design CAN do is enforce, without exception or emotion, the
things that separate a durable strategy from a lucky one:

  * Every position risks the same small fraction of equity (RISK_PER_TRADE_
    PCT). Size is derived from the distance to the stop, so a wide stop
    buys fewer shares. One bad trade cannot materially hurt the account.
  * No position is opened unless its reward:risk clears MIN_ENTRY_RR. Being
    right less than half the time is survivable at 2.5:1; it is fatal at
    1:1. This is the single rule doing the most work here.
  * Exits are mechanical and pre-committed at entry -- stop, target,
    trailing stop, thesis decay. The bot cannot talk itself into holding a
    loser, which is the failure mode that ends most real accounts.
  * Concentration is capped by position count and by sector, so the
    portfolio can't quietly become one bet expressed five ways.
  * Losses are recorded as loudly as wins. A paper bot that hid its losers
    would be worse than useless -- it would be misleading.

Two strategies share the account, tagged per position:
  INVEST -- composite-score driven, months-long horizon, wider stop.
  TRADE  -- day-trade-score driven, uses the computed swing levels and
            their real measured risk/reward, short horizon.

State lives in results/bot_state.json on the persistent disk, so the
portfolio survives redeploys. No network calls in this module.
"""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE, "results", "bot_state.json")

# --- Account and risk ---------------------------------------------------
STARTING_EQUITY = 100_000.0
RISK_PER_TRADE_PCT = 1.0      # % of CURRENT equity risked if the stop hits
MAX_POSITIONS = 10
MAX_PER_SECTOR = 2
MAX_POSITION_PCT = 15.0       # cap any single position's share of equity
MIN_CASH_BUFFER_PCT = 5.0     # never deploy the last of the cash

# --- Entry gates --------------------------------------------------------
MIN_INVEST_SCORE = 68.0       # composite score required to open an INVEST position
MIN_TRADE_SCORE = 70.0        # day-trade score required to open a TRADE position
MIN_ENTRY_RR = 2.0            # reward:risk floor -- the core rule of the whole bot

# --- Earnings gap risk ---------------------------------------------------
#
# Every risk number in this file assumes one thing: that the stop holds, so a
# loser costs RISK_PER_TRADE_PCT and no more. Overnight earnings gaps are the
# main case where that assumption is simply false -- a stop cannot fill while
# the market is shut, it fills at whatever price the stock reopens at.
#
# The damage is not marginal. At the measured 32.4% win rate the system runs
# a +3.8 point edge over its break-even; a 14% gap moves break-even to 35.9%
# and the edge is gone, and typical single-name earnings gaps are 8-15%.
#
# The response is NOT to ban the trade -- a company heading into a good print
# is a real opportunity, and refusing it forfeits that. The response is to
# size it for the risk that is actually present. The stop stays where it is
# (it still works intraday); SIZING pretends the stop is EARNINGS_GAP_PCT
# away, which roughly halves the position. Same 1% of equity at risk, now
# measured against the loss that can really happen. (2026-09-02)
EARNINGS_WINDOW_DAYS = 5      # size down when a report is this close
EARNINGS_GAP_PCT = 20.0       # the loss to size against, not the stop we set

# --- Exits: sized to EACH STOCK, not to a single fixed percentage --------
#
# A flat 10%/25% is correctly calibrated for exactly one kind of stock -- a
# ~2%-a-day mid-cap -- and mis-sized for everything else. Price wanders
# roughly vol*sqrt(days), so the time to travel a fixed distance scales with
# (distance/vol)^2:
#
#   * A 0.8%/day utility needs ~2.5 YEARS of drift to reach +25%. The target
#     is effectively unreachable, so those positions can only ever exit by
#     stop or thesis decay -- the reward half of the reward:risk never
#     happens, while the risk half works perfectly. That is a strategy that
#     only knows how to lose.
#   * A 4.5%/day momentum name has a 10% stop sitting INSIDE its ordinary
#     daily noise, so it gets stopped out at random and pays spread for it.
#
# Both levels are therefore expressed in multiples of the stock's own
# average daily move (technical.volatility_pct, already computed by
# lib.indicators). The RATIO is fixed by construction, so reward:risk is
# identical for every position and only the distances change. A 2%/day
# stock lands on -10%/+25%, i.e. exactly today's behaviour -- this
# generalises the current setting rather than replacing it. (2026-09-02,
# user-identified: "every stock is different")
# MEASURED AND REJECTED (2026-09-03). The reasoning above is sound and the
# result still went the other way: on two years of real prices, volatility-
# scaled levels returned +13.43% against +25.53% for fixed 10/25 on identical
# data -- twelve points worse, with more trades (325 vs 277) and a lower
# realized R:R (2.09 vs 2.64). Lower drawdown (-12.2% vs -14.5%), but not
# nearly enough to justify halving the return.
#
# The likeliest explanation: scaling shrinks the target for low-volatility
# names, so those positions bank small wins instead of running. In a trending
# market letting winners run beats sizing them "appropriately" -- the same
# lesson the +5% target experiment taught, arriving by a different route.
#
# The code stays because it is tested, documented and might well win in a
# choppy or bear regime that this window does not contain. It is OFF by
# default, and the sweep keeps running it head-to-head so the conclusion
# gets re-checked against new data rather than frozen.
USE_VOL_SCALED_LEVELS = False

TARGET_RR = 2.5               # reward:risk every position is built to
VOL_STOP_MULT = 5.0           # stop distance = this many average daily moves

# Clamps matter as much as the scaling. Five daily moves on a 6%/day meme
# stock is a 30% stop -- "only 5 vol units" is no comfort when the loss is
# real money. And a 2% stop on a sleepy utility is inside the bid-ask noise.
# The stop is clamped FIRST and the target derived from the clamped stop, so
# clamping can never quietly break the reward:risk ratio.
MIN_STOP_PCT = 4.0
MAX_STOP_PCT = 15.0

# Fallbacks for when volatility is unavailable (rare -- technicals are
# computed even for day-trade candidates).
INVEST_STOP_PCT = 10.0
INVEST_TARGET_PCT = 25.0
TRADE_STOP_PCT = 6.0          # tighter: short horizon, less room to be wrong
TRADE_TARGET_PCT = 14.0


def scaled_levels(volatility_pct, fallback_stop=INVEST_STOP_PCT):
    """(stop_pct, target_pct) sized to this stock's own daily range.

    Returns positive percentages. Falls back to the fixed stop when
    volatility is missing, and always derives the target from the FINAL
    (clamped) stop so the reward:risk ratio survives the clamps."""
    if not volatility_pct or volatility_pct <= 0:
        stop = fallback_stop
    else:
        stop = volatility_pct * VOL_STOP_MULT
    stop = max(MIN_STOP_PCT, min(MAX_STOP_PCT, stop))
    return round(stop, 2), round(stop * TARGET_RR, 2)


# Ratchet rungs as FRACTIONS of the target rather than fixed percentages, so
# gain protection scales with the trade the same way the levels do. On a 25%
# target these land on +8.75/+15/+20 locking 0/+7.5/+12.5 -- within a rounding
# error of the fixed rungs they replace. On an 8% day-trade target they become
# +2.8/+4.8/+6.4, which is the real fix: under fixed rungs a short-horizon
# trade never reached even the first one and ran with no gain protection at
# all. (fraction of target reached, fraction of target locked in)
RATCHET_FRACTIONS = ((0.80, 0.50), (0.60, 0.30), (0.35, 0.0))


def ratchet_rungs(target_pct):
    """Concrete (gain_reached, gain_locked) rungs for a given target."""
    if not target_pct or target_pct <= 0:
        return ()
    return tuple((round(target_pct * hit, 2), round(target_pct * lock, 2))
                 for hit, lock in RATCHET_FRACTIONS)
# --- Gain protection: a RATCHET, not a percentage giveback ---------------
#
# The old rule ("once up 10%, close on giving back a third of the peak") was
# measured doing the opposite of its job. With a +25% target it made that
# target almost unreachable: a position had to run from +10% to +25% without
# ever retracing a third of its peak, so any ordinary pullback closed it in
# the low teens. The backtest showed the damage precisely -- designed 2.5:1,
# realized 1.81:1, which drags the break-even win rate from 28.6% up to
# 35.6% against an actual 37%. Nearly the whole edge was being given away by
# the mechanism meant to protect it.
#
# The replacement ratchets the STOP upward at fixed milestones instead of
# scaling with the peak. Gains get locked in progressively, but the position
# keeps full room to reach target, because the exit level no longer rises in
# proportion to how well the trade is doing. (2026-09-02)
#
# Each entry: (gain reached, stop moves to this gain). Applied highest-first.
RATCHET_STEPS = ((20.0, 12.0), (15.0, 7.0), (8.0, 0.0))
SCORE_DROP_EXIT = 10.0

# A thesis exit is for a position that ISN'T working. Once a trade is up
# meaningfully, the ratcheted stop is the better instrument -- closing a
# winner because a noisy score slipped is exactly the leakage above in
# another form. Above this gain, only price decides.
THESIS_EXIT_MAX_GAIN_PCT = 5.0
MAX_TRADE_HOLD_CYCLES = 96    # ~1 day of 15-min cycles; a day trade isn't a hold

# --- Thesis review: matching decision cadence to the holding horizon -----
#
# THE PROBLEM THIS SOLVES (found in simulation, 2026-08-28): the bot runs
# every 15 minutes, but an INVEST position is a months-long thesis. Checking
# "has the thesis broken?" 96 times a day means ~2,000 tests a month against
# a score that carries intraday noise -- so the rule fired constantly on
# nothing, closing 78 of 103 positions and turning the strategy into a churn
# machine. The first patch (require 4 consecutive cycles) was a band-aid: it
# raised the bar on a test that was still being run far too often, and with
# that many trials noise still wins.
#
# THE ACTUAL FIX is to separate the two kinds of risk by how fast they move:
#
#   PRICE risk is continuous and can gap in seconds, so stop / target /
#   trailing are still checked EVERY cycle and fire instantly. The stop is
#   what protects the account between thesis reviews -- that is precisely
#   its job, and it means a slow thesis review costs nothing in safety.
#
#   THESIS risk moves on the timescale of earnings and fundamentals, not
#   minutes. It is reviewed at most ONCE PER DAY, and then only on evidence
#   that is itself de-noised:
#     * the score is compared as a smoothed EMA, not a raw instantaneous
#       reading, so one bad tick can't trigger anything;
#     * it is compared against a BASELINE averaged over the position's first
#       few cycles rather than the single instant of entry -- entering on a
#       noisy high print otherwise makes every later normal reading look
#       like deterioration, which is measuring noise against noise;
#     * and two consecutive daily reviews must agree before it closes.
#
# Net effect: a thesis exit needs a real, sustained, multi-day decline --
# while a price collapse still gets cut the same cycle it happens.
SCORE_EMA_ALPHA = 0.15        # smoothing on the per-position composite score
BASELINE_CYCLES = 4           # cycles averaged to establish the entry baseline
THESIS_CONFIRM_REVIEWS = 2    # consecutive DAILY reviews that must agree

EXIT_LABEL = {
    "target": "target hit",
    "stop": "stop-loss",
    "trailing": "trailing stop",
    "score_drop": "signal faded",
    "signal_flip": "signal reversed",
    "timeout": "held too long",
    "untracked": "lost coverage",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def new_state():
    return {
        "starting_equity": STARTING_EQUITY,
        "cash": STARTING_EQUITY,
        "positions": [],
        "closed_trades": [],
        "equity_curve": [],
        "actions": [],
        "created_at": _now(),
    }


def load_state():
    if not os.path.exists(STATE_PATH):
        return new_state()
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        # A corrupt state file must not take the refresh cycle down, but it
        # also must not silently wipe the record -- so start fresh and say so.
        state = new_state()
        state["actions"] = [{"ts": _now(), "kind": "error",
                             "detail": "bot_state.json unreadable -- portfolio restarted"}]
    for key, default in (("positions", []), ("closed_trades", []),
                         ("equity_curve", []), ("actions", [])):
        state.setdefault(key, default)
    state.setdefault("cash", STARTING_EQUITY)
    state.setdefault("starting_equity", STARTING_EQUITY)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)  # atomic: a crash mid-write can't corrupt the portfolio
    return state


# --- Sizing -------------------------------------------------------------

def size_position(equity, cash, entry_price, stop_price):
    """Shares to buy so that being stopped out costs exactly
    RISK_PER_TRADE_PCT of equity -- then clamped by the per-position cap and
    by cash actually on hand.

    Risk-based sizing is the whole point: a stock whose stop is 3% away gets
    a much larger position than one whose stop is 12% away, so both cost the
    same if they're wrong. Sizing by dollar amount instead (the intuitive
    way) silently makes the volatile names the big bets.

    Returns (shares, dollars_at_risk, reason_if_rejected)."""
    if not entry_price or entry_price <= 0:
        return 0, 0.0, "no price"
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0, 0.0, "stop is not below entry"

    risk_budget = equity * (RISK_PER_TRADE_PCT / 100.0)
    shares = int(risk_budget / risk_per_share)
    if shares <= 0:
        return 0, 0.0, "risk budget too small for one share"

    # Cap by share of equity -- risk sizing alone can produce a huge position
    # when the stop is very tight, which is a concentration risk even though
    # the stop-loss risk is correct.
    max_by_weight = int((equity * MAX_POSITION_PCT / 100.0) / entry_price)
    if max_by_weight <= 0:
        return 0, 0.0, "share price exceeds position cap"
    shares = min(shares, max_by_weight)

    # Cap by cash, keeping a buffer so the account is never fully deployed.
    spendable = cash - (equity * MIN_CASH_BUFFER_PCT / 100.0)
    max_by_cash = int(spendable / entry_price) if spendable > 0 else 0
    if max_by_cash <= 0:
        return 0, 0.0, "not enough free cash"
    shares = min(shares, max_by_cash)

    if shares <= 0:
        return 0, 0.0, "sized to zero shares"
    return shares, round(shares * risk_per_share, 2), None


def equity_of(state, price_lookup):
    """Cash plus marked-to-market value of every open position. Positions
    whose ticker is missing from this cycle fall back to their last known
    mark rather than vanishing from the total."""
    total = state.get("cash", 0.0)
    for p in state.get("positions", []):
        px = price_lookup.get(p["ticker"], p.get("last_price") or p["entry_price"])
        total += p["shares"] * px
    return round(total, 2)


# --- Exit logic ---------------------------------------------------------

def _position_pnl_pct(pos, price):
    entry = pos["entry_price"]
    if not entry:
        return 0.0
    return (price - entry) / entry * 100.0


def update_marks(pos, price, score):
    """Per-cycle bookkeeping that must happen before any exit test: the peak
    (for the trailing stop), the smoothed score, and -- for the position's
    first few cycles -- the baseline the thesis is later judged against.

    Kept separate from check_exit so the noise-reduction state advances every
    single cycle even on days when no thesis review is due."""
    pnl = _position_pnl_pct(pos, price)
    pos["peak_pct"] = max(pos.get("peak_pct") or 0.0, pnl)

    # Ratchet the stop upward through the milestones the trade has reached.
    # Never downward -- a stop that can loosen is not a stop. Rungs are
    # derived from this position's OWN target, so a tight day trade and a
    # wide high-volatility hold each get protection proportional to what
    # they are actually trying to capture.
    entry = pos.get("entry_price")
    if entry:
        tgt = pos.get("target_price")
        target_pct = ((tgt / entry) - 1) * 100 if tgt else None
        for reached, lock_at in (ratchet_rungs(target_pct) or RATCHET_STEPS):
            if pos["peak_pct"] >= reached:
                new_stop = entry * (1 + lock_at / 100.0)
                if new_stop > pos.get("stop_price", 0):
                    pos["stop_price"] = round(new_stop, 4)
                    pos["stop_locked_at_pct"] = lock_at
                break

    if score is None:
        return
    prev = pos.get("score_ema")
    pos["score_ema"] = round(
        score if prev is None else SCORE_EMA_ALPHA * score + (1 - SCORE_EMA_ALPHA) * prev, 2)

    if pos.get("baseline_n", 0) < BASELINE_CYCLES:
        pos["baseline_sum"] = pos.get("baseline_sum", 0.0) + score
        pos["baseline_n"] = pos.get("baseline_n", 0) + 1
        pos["baseline_score"] = round(pos["baseline_sum"] / pos["baseline_n"], 2)


def check_exit(pos, price, signal, ts):
    """Which pre-committed exit fires, if any.

    Call update_marks() first. Mutates the position's review state, so call
    this exactly once per cycle per position.

    Hard exits are evaluated every cycle. The thesis is reviewed at most once
    per calendar day -- see the block comment on SCORE_EMA_ALPHA above for
    why the cadences differ."""
    pnl = _position_pnl_pct(pos, price)

    # --- Hard exits: price risk, checked every cycle, no delay ---
    if price >= pos["target_price"]:
        return "target"
    if price <= pos["stop_price"]:
        # Distinguish a real loss from a ratcheted stop that banked a gain --
        # they are completely different events and lumping them together
        # would make the exit statistics meaningless.
        return "trailing" if pos.get("stop_locked_at_pct") is not None else "stop"

    if pos["strategy"] == "TRADE":
        # A day trade that has quietly become a long-term hold is a failed
        # day trade, not a position -- close it rather than let it drift.
        if pos.get("cycles_held", 0) >= MAX_TRADE_HOLD_CYCLES:
            return "timeout"
        return None

    # --- Thesis review: at most once per calendar day ---
    # Skipped entirely while the trade is working. A position up 12% with a
    # ratcheted stop underneath it does not need a thesis opinion; letting a
    # drifting score close it is the same leak that capped realized R:R at
    # 1.81 against a designed 2.5.
    if pnl > THESIS_EXIT_MAX_GAIN_PCT:
        pos["thesis_strikes"] = 0
        pos["thesis_kind"] = None
        return None

    day = (ts or "")[:10]
    if not day or pos.get("last_review_day") == day:
        return None
    if pos.get("baseline_n", 0) < BASELINE_CYCLES:
        return None  # baseline not established yet; nothing stable to judge against
    pos["last_review_day"] = day

    baseline = pos.get("baseline_score")
    ema = pos.get("score_ema")
    verdict = None
    if baseline is not None and ema is not None and ema <= baseline - SCORE_DROP_EXIT:
        verdict = "score_drop"
    elif signal == "SELL":
        verdict = "signal_flip"

    if verdict is None:
        pos["thesis_strikes"] = 0
        pos["thesis_kind"] = None
        return None

    # Consecutive DAILY reviews must agree, and agree on the same problem --
    # one day's warning followed by a recovery resets the count entirely.
    if pos.get("thesis_kind") == verdict:
        pos["thesis_strikes"] = pos.get("thesis_strikes", 0) + 1
    else:
        pos["thesis_kind"] = verdict
        pos["thesis_strikes"] = 1

    return verdict if pos["thesis_strikes"] >= THESIS_CONFIRM_REVIEWS else None


def _close_position(state, pos, price, reason, ts):
    proceeds = pos["shares"] * price
    state["cash"] = round(state["cash"] + proceeds, 2)
    pnl_dollars = round((price - pos["entry_price"]) * pos["shares"], 2)
    pnl_pct = round(_position_pnl_pct(pos, price), 2)
    trade = {
        **{k: pos[k] for k in ("ticker", "name", "sector", "strategy", "shares",
                               "entry_price", "entry_ts", "stop_price", "target_price",
                               "entry_rr", "entry_reasons", "risk_dollars")},
        "exit_price": round(price, 2),
        "exit_ts": ts,
        "exit_reason": reason,
        "exit_label": EXIT_LABEL.get(reason, reason),
        "pnl_dollars": pnl_dollars,
        "pnl_pct": pnl_pct,
        "peak_pct": round(pos.get("peak_pct") or 0.0, 2),
        "win": pnl_dollars > 0,
    }
    state["closed_trades"].append(trade)
    state["actions"].append({
        "ts": ts, "kind": "sell", "ticker": pos["ticker"], "strategy": pos["strategy"],
        "shares": pos["shares"], "price": round(price, 2),
        "detail": f"{EXIT_LABEL.get(reason, reason)} — {pnl_pct:+.2f}% (${pnl_dollars:+,.0f})",
    })
    return trade


# --- Entry logic --------------------------------------------------------

def earnings_proximity(r):
    """(days_until, is_close) for a candidate, from the earnings_risk block
    lib.pipeline already computes. Returns (None, False) when the data isn't
    there at all -- which is the honest answer for day-trade candidates,
    since that scan uses full=False and deliberately skips the earnings
    fetch to stay fast. Those positions are NOT sized down, and that is a
    real remaining hole rather than something this function papers over."""
    er = r.get("earnings_risk") or {}
    days = er.get("days_until")
    if days is None:
        return None, False
    return days, (0 <= days <= EARNINGS_WINDOW_DAYS)


def _entry_reasons(r, strategy):
    """The 'why' behind a buy, in plain language, captured AT ENTRY so it
    can't be rewritten later to fit the outcome."""
    out = []
    fund = r.get("fundamental") or {}
    tech = r.get("technical") or {}
    sent = r.get("sentiment") or {}
    ins = r.get("insider") or {}

    if strategy == "INVEST":
        if r.get("composite_score") is not None:
            out.append(f"Composite score {r['composite_score']:.0f} (signal {r.get('signal')}).")
        if fund.get("pe_ratio"):
            out.append(f"P/E {fund['pe_ratio']:.1f}.")
        if fund.get("revenue_growth_pct") is not None:
            out.append(f"Revenue {fund['revenue_growth_pct']:+.1f}% YoY.")
        if fund.get("analyst_upside_pct") is not None:
            out.append(f"Analyst target implies {fund['analyst_upside_pct']:+.0f}%.")
        if ins.get("insider_buys"):
            out.append(f"{ins['insider_buys']} insider purchase(s) recently.")
    else:
        if r.get("day_trade_score") is not None:
            out.append(f"Day-trade setup score {r['day_trade_score']:.0f} ({r.get('day_trade_rating')}).")
        if tech.get("rsi") is not None:
            out.append(f"RSI {tech['rsi']:.0f}.")
        if tech.get("volatility_pct") is not None:
            out.append(f"Averaging {tech['volatility_pct']:.1f}%/day range.")
        if tech.get("range_position_pct") is not None:
            out.append(f"At {tech['range_position_pct']:.0f}% of its 20-day range.")
    if sent.get("big_news"):
        out.append("Real news catalyst behind the move.")
    return out


def _candidate_entries(payload):
    """Every scored name this cycle, tagged with which strategy would take
    it. INVEST wins ties -- a name qualifying on both is the stronger case
    as a position than as a scalp."""
    seen = {}
    pools = (payload.get("watchlist_results") or []) + \
            (payload.get("discovered_candidates") or []) + \
            (payload.get("value_picks") or []) + \
            (payload.get("screener_picks") or [])
    for r in pools:
        t = r.get("ticker")
        if t and t not in seen:
            seen[t] = r
    return seen


def evaluate_entry(r):
    """Decide whether this name is buyable and on what terms. Returns
    (strategy, entry_price, stop, target, rr, reasons) or None.

    The reward:risk gate is applied to EVERY candidate regardless of how
    good the score is. A brilliant setup with 1:1 risk/reward is declined --
    that is the rule the whole account depends on."""
    price = r.get("price")
    if not price or price <= 0:
        return None

    composite = r.get("composite_score")
    dts = r.get("day_trade_score")

    # --- INVEST ---
    if composite is not None and composite >= MIN_INVEST_SCORE and r.get("signal") == "BUY":
        if USE_VOL_SCALED_LEVELS:
            vol = (r.get("technical") or {}).get("volatility_pct")
            stop_pct, target_pct = scaled_levels(vol, fallback_stop=INVEST_STOP_PCT)
        else:
            stop_pct, target_pct = INVEST_STOP_PCT, INVEST_TARGET_PCT
        stop = price * (1 - stop_pct / 100.0)
        target = price * (1 + target_pct / 100.0)
        rr = (target - price) / (price - stop)
        if rr >= MIN_ENTRY_RR:
            return ("INVEST", price, stop, target, round(rr, 2), _entry_reasons(r, "INVEST"))

    # --- TRADE ---
    if dts is not None and dts >= MIN_TRADE_SCORE and r.get("day_trade_direction") == "long":
        levels = r.get("day_trade_levels") or {}
        stop = levels.get("stop")
        exit_zone = levels.get("exit_zone")
        target = exit_zone[0] if exit_zone else None
        # Fall back to fixed percentages when the computed swing levels are
        # unusable, rather than skipping an otherwise-valid setup.
        # Swing levels are already volatility-aware (compute_day_trade_levels
        # sizes them off the stock's own recent range), so they are preferred.
        # The fallback now scales too, instead of a flat 6/14.
        if USE_VOL_SCALED_LEVELS:
            vol = (r.get("technical") or {}).get("volatility_pct")
            f_stop, f_target = scaled_levels(vol, fallback_stop=TRADE_STOP_PCT)
        else:
            f_stop, f_target = TRADE_STOP_PCT, TRADE_TARGET_PCT
        if not stop or stop >= price:
            stop = price * (1 - f_stop / 100.0)
        if not target or target <= price:
            target = price * (1 + f_target / 100.0)
        rr = (target - price) / (price - stop)
        if rr >= MIN_ENTRY_RR:
            return ("TRADE", price, stop, target, round(rr, 2), _entry_reasons(r, "TRADE"))

    return None


def run_cycle(payload, state=None, ts=None):
    """One full bot pass: mark to market, run exits, then run entries.

    Exits before entries, deliberately -- freeing cash from a closed loser
    should be available to the same cycle's best new idea, and a position
    that just hit its stop must never be re-bought on the same pass."""
    state = state if state is not None else load_state()
    ts = ts or _now()
    candidates = _candidate_entries(payload)
    price_lookup = {t: r["price"] for t, r in candidates.items() if r.get("price")}

    # --- 1. Mark to market + exits ---
    still_open = []
    for pos in state["positions"]:
        r = candidates.get(pos["ticker"]) or {}
        price = r.get("price") or pos.get("last_price") or pos["entry_price"]
        pos["last_price"] = round(price, 4)
        pos["cycles_held"] = pos.get("cycles_held", 0) + 1

        update_marks(pos, price, r.get("composite_score"))
        reason = check_exit(pos, price, r.get("signal"), ts)
        if reason:
            _close_position(state, pos, price, reason, ts)
        else:
            still_open.append(pos)
    state["positions"] = still_open

    # --- 2. Entries ---
    equity = equity_of(state, price_lookup)
    held = {p["ticker"] for p in state["positions"]}
    sector_counts = {}
    for p in state["positions"]:
        sector_counts[p.get("sector") or "Other"] = sector_counts.get(p.get("sector") or "Other", 0) + 1

    ranked = []
    for t, r in candidates.items():
        if t in held:
            continue
        decision = evaluate_entry(r)
        if decision:
            # Rank by reward:risk, not by score -- given two acceptable
            # setups the account should always prefer the one that pays more
            # for the same risk.
            ranked.append((decision[4], t, r, decision))
    ranked.sort(key=lambda x: x[0], reverse=True)

    for rr, ticker, r, (strategy, entry, stop, target, rr_val, reasons) in ranked:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        sector = r.get("sector") or "Other"
        if sector_counts.get(sector, 0) >= MAX_PER_SECTOR:
            continue
        # Size against the loss that can actually occur. The stop we SET is
        # unchanged -- it still protects intraday. But into a report the
        # realistic downside is a gap, so sizing uses that instead, which
        # cuts the position roughly in half and keeps the 1%-of-equity risk
        # honest rather than nominal.
        days_to_earnings, near_earnings = earnings_proximity(r)
        sizing_stop = entry * (1 - EARNINGS_GAP_PCT / 100.0) if near_earnings else stop
        shares, risk_dollars, reject = size_position(equity, state["cash"], entry, sizing_stop)
        if reject:
            continue
        cost = shares * entry
        state["cash"] = round(state["cash"] - cost, 2)
        pos = {
            "ticker": ticker, "name": r.get("name"), "sector": sector,
            "strategy": strategy, "shares": shares,
            "entry_price": round(entry, 4), "entry_ts": ts,
            "stop_price": round(stop, 4), "target_price": round(target, 4),
            "entry_rr": rr_val, "entry_score": r.get("composite_score"),
            "volatility_pct": (r.get("technical") or {}).get("volatility_pct"),
            "stop_pct": round((1 - stop / entry) * 100, 2),
            "target_pct": round((target / entry - 1) * 100, 2),
            "days_to_earnings": days_to_earnings,
            "earnings_sized_down": bool(near_earnings),
            "entry_reasons": reasons, "risk_dollars": risk_dollars,
            "last_price": round(entry, 4), "peak_pct": 0.0, "cycles_held": 0,
            # Thesis-review state (see the SCORE_EMA_ALPHA block comment).
            # baseline_score is seeded from the entry score but keeps
            # averaging over the first BASELINE_CYCLES cycles.
            "score_ema": r.get("composite_score"),
            "baseline_sum": 0.0, "baseline_n": 0, "baseline_score": None,
            "last_review_day": (ts or "")[:10],
            "thesis_strikes": 0, "thesis_kind": None,
        }
        state["positions"].append(pos)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        state["actions"].append({
            "ts": ts, "kind": "buy", "ticker": ticker, "strategy": strategy,
            "shares": shares, "price": round(entry, 2),
            "detail": (f"{rr_val}:1 reward:risk, risking ${risk_dollars:,.0f} "
                       f"({RISK_PER_TRADE_PCT:.0f}% of equity)"
                       + (f" — HALF SIZE, earnings in {days_to_earnings}d"
                          if near_earnings else "")),
        })

    # --- 3. Mark the books ---
    equity = equity_of(state, price_lookup)
    state["equity_curve"].append({"ts": ts, "equity": equity})
    state["equity_curve"] = state["equity_curve"][-2000:]
    state["actions"] = state["actions"][-200:]
    state["last_run"] = ts
    return state


# --- Reporting ----------------------------------------------------------

def summarize(state):
    """Everything the dashboard needs, computed from state alone."""
    curve = state.get("equity_curve") or []
    equity = curve[-1]["equity"] if curve else state.get("cash", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    total_return_pct = round((equity / start - 1) * 100, 2) if start else None

    # Max drawdown: worst peak-to-trough fall the account actually lived
    # through. A strategy's return is only meaningful next to the pain it
    # took to get it.
    peak = start
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        if peak > 0:
            max_dd = min(max_dd, (pt["equity"] / peak - 1) * 100)

    closed = state.get("closed_trades") or []
    wins = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    avg_win = round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None
    realized_rr = round(avg_win / abs(avg_loss), 2) if (avg_win and avg_loss and avg_loss < 0) else None
    hit_rate = round(100 * len(wins) / len(closed), 1) if closed else None
    expectancy = round(sum(t["pnl_pct"] for t in closed) / len(closed), 2) if closed else None
    breakeven = round(100 / (1 + realized_rr), 1) if realized_rr else None

    open_val = sum(p["shares"] * (p.get("last_price") or p["entry_price"]) for p in state.get("positions", []))
    return {
        "equity": equity,
        "starting_equity": start,
        "cash": round(state.get("cash", 0.0), 2),
        "invested": round(open_val, 2),
        "total_return_pct": total_return_pct,
        "realized_pnl": round(sum(t["pnl_dollars"] for t in closed), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "open_count": len(state.get("positions", [])),
        "closed_count": len(closed),
        "hit_rate_pct": hit_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "realized_rr": realized_rr,
        "expectancy_pct": expectancy,
        "breakeven_hit_rate_pct": breakeven,
        "profitable": (expectancy > 0) if expectancy is not None else None,
        "cycles": len(curve),
    }


def public_snapshot(state):
    """What gets embedded in results/latest.json for the dashboard."""
    positions = []
    for p in sorted(state.get("positions", []), key=lambda x: x.get("entry_ts") or "", reverse=True):
        price = p.get("last_price") or p["entry_price"]
        positions.append({
            **p,
            "market_value": round(p["shares"] * price, 2),
            "unrealized_pct": round(_position_pnl_pct(p, price), 2),
            "unrealized_dollars": round((price - p["entry_price"]) * p["shares"], 2),
        })
    return {
        "summary": summarize(state),
        "positions": positions,
        "closed_trades": sorted(state.get("closed_trades", []),
                                key=lambda t: t.get("exit_ts") or "", reverse=True)[:60],
        "actions": list(reversed(state.get("actions", [])))[:40],
        # 400 points is only ~15 sessions at one cycle per 15 minutes, which
        # is not enough behind the chart's 1M/3M ranges. The state keeps 2000
        # (~77 sessions); expose all of it.
        "equity_curve": state.get("equity_curve", [])[-2000:],
        "config": {
            "starting_equity": STARTING_EQUITY,
            "risk_per_trade_pct": RISK_PER_TRADE_PCT,
            "min_entry_rr": MIN_ENTRY_RR,
            "max_positions": MAX_POSITIONS,
            "max_per_sector": MAX_PER_SECTOR,
            "invest_stop_pct": INVEST_STOP_PCT,
            "invest_target_pct": INVEST_TARGET_PCT,
            "trade_stop_pct": TRADE_STOP_PCT,
            "trade_target_pct": TRADE_TARGET_PCT,
        },
    }
