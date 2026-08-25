"""
Algorithmic day-trade setup score. Turns the same technical/sentiment data
already computed for every ticker into a single 0-100 number, instead of
just a pass/fail checklist -- so candidates can be ranked against each
other, not just individually graded. Pure math, no network.

This scores whether a stock is CURRENTLY a good short-horizon trading
SETUP (liquid, moving, with real interest behind it, a real catalyst, near
a level that tends to trigger a break) -- it is not a prediction of
magnitude, and it does not place trades. See lib/checklist.day_trade_checklist
for the plain-language pass/fail version of the same idea (kept alongside
this, not replaced by it, since "6 clear yes/no reasons" and "one ranked
number" serve different purposes on the dashboard).

Also determines DIRECTION (long vs short) from the same short-term momentum
data, since a good setup can be a breakdown just as easily as a breakout --
lib.price_levels.compute_day_trade_levels uses this to build the right
kind of entry/target/stop zone (short-side levels are mirror images of the
long-side ones: entry near resistance, target near support, stop above
entry instead of below).
"""

WEIGHTS = {
    "liquidity": 0.10,
    "volatility": 0.15,
    "momentum": 0.15,
    "trend": 0.10,
    "catalyst": 0.10,
    "inflection": 0.10,
    "relative_volume": 0.15,
    "extension": 0.15,
}


def _liquidity_score(market_cap_usd):
    if market_cap_usd is None:
        return None, None
    if market_cap_usd >= 200e9:
        return 95, f"Market cap ${market_cap_usd/1e9:,.0f}B — mega-cap, ample liquidity."
    if market_cap_usd >= 50e9:
        return 85, f"Market cap ${market_cap_usd/1e9:,.0f}B — large-cap, easy in and out."
    if market_cap_usd >= 10e9:
        return 70, f"Market cap ${market_cap_usd/1e9:,.0f}B — solid liquidity."
    if market_cap_usd >= 2e9:
        return 45, f"Market cap ${market_cap_usd/1e9:,.0f}B — mid-cap, workable but thinner."
    return 20, f"Market cap ${market_cap_usd/1e9:,.1f}B — small-cap, watch spreads and slippage."


def _volatility_score(volatility_pct):
    if volatility_pct is None:
        return None, None
    if volatility_pct < 0.6:
        return 15, f"Averaging {volatility_pct:.1f}%/day — too quiet for a fast trade."
    if volatility_pct < 1.2:
        return 45, f"Averaging {volatility_pct:.1f}%/day — modest range."
    if volatility_pct < 2.5:
        return 85, f"Averaging {volatility_pct:.1f}%/day — a real intraday range to work with."
    if volatility_pct < 5:
        return 95, f"Averaging {volatility_pct:.1f}%/day — plenty of movement."
    return 65, f"Averaging {volatility_pct:.1f}%/day — very volatile, tradeable but higher risk."


def _momentum_score(rsi):
    if rsi is None:
        return None, None
    score = min(100, abs(rsi - 50) * 2.4)
    note = f"RSI {rsi:.1f} — " + ("a clear directional push." if score >= 50 else "stuck near neutral, no real push either way.")
    return round(score, 1), note


def _trend_score(macd_hist, momentum_10d_pct):
    if macd_hist is None or momentum_10d_pct is None:
        return None, None
    agree = (macd_hist > 0 and momentum_10d_pct > 0) or (macd_hist < 0 and momentum_10d_pct < 0)
    score = 90 if agree else 40
    note = ("MACD and 10-day momentum point the same direction — trend confirmed."
            if agree else "MACD and 10-day momentum disagree — trend isn't confirmed yet.")
    return score, note


def _catalyst_score(big_news):
    if big_news is None:
        return None, None
    score = 90 if big_news else 40
    note = "Real news catalyst behind the move, not just drift." if big_news else "No standout headline right now."
    return score, note


def _inflection_score(range_position_pct):
    if range_position_pct is None:
        return None, None
    score = min(100, abs(range_position_pct - 50) * 2.2)
    note = (f"Trading at {range_position_pct:.0f}% of its 20-day range — right at an edge, near a breakout/breakdown level."
            if score >= 60 else
            f"Trading at {range_position_pct:.0f}% of its 20-day range — sitting mid-range, away from an inflection point.")
    return round(score, 1), note


def _relative_volume_score(volume, avg_volume):
    if volume is None or not avg_volume:
        return None, None
    rvol = volume / avg_volume
    if rvol < 0.7:
        score = 20
    elif rvol < 1.2:
        score = 45
    elif rvol < 2:
        score = 75
    elif rvol < 4:
        score = 95
    else:
        score = 88  # extreme rvol is still real interest, but capped slightly below the sweet spot
    note = f"Trading at {rvol:.1f}x its average volume — " + (
        "real interest showing up today, not just chart noise." if rvol >= 1.2 else "no unusual volume behind this yet."
    )
    return score, note


def _extension_score(momentum_10d_pct):
    """Penalize stocks that have already made their move. Everything else in
    this file rewards momentum/volatility/volume without asking HOW MUCH the
    stock has already moved -- which means a name that just spiked 100%+ over
    a handful of sessions (the move already happened) can outscore a name
    that's only just starting to break out (the move hasn't happened yet).
    That's backwards for a "find setups before it happens, not after" tool:
    a huge recent move is a chase, not a fresh entry, and the reward for
    catching it was already collected by whoever was in it days ago.
    (added 2026-08-21, user-reported: a "Prime setup" day-trade pick whose
    entry zone was already 55%+ below current price -- the move behind the
    high score had already fully played out days before it was surfaced.)"""
    if momentum_10d_pct is None:
        return None, None
    m = abs(momentum_10d_pct)
    if m < 15:
        return 85, f"Only {momentum_10d_pct:+.1f}% over the last 10 sessions — hasn't made its move yet."
    if m < 30:
        return 60, f"{momentum_10d_pct:+.1f}% over the last 10 sessions — already moving; some room may be left."
    if m < 60:
        return 30, f"{momentum_10d_pct:+.1f}% over the last 10 sessions — already extended; this is closer to a chase than a fresh setup."
    return 10, f"{momentum_10d_pct:+.1f}% over the last 10 sessions — a huge move already happened; treat this as a chase, not a new entry."


def determine_direction(technical: dict):
    """Long vs short lean from the same short-term momentum data used above.
    MACD histogram + 10-day momentum agreeing is the primary signal (same
    check as _trend_score); RSI vs. 50 is the fallback when they disagree or
    are missing. Defaults to "long" only when there's truly nothing to go on."""
    tech = technical or {}
    mh = tech.get("macd_hist")
    mom = tech.get("momentum_10d_pct")
    if mh is not None and mom is not None:
        if mh > 0 and mom > 0:
            return "long"
        if mh < 0 and mom < 0:
            return "short"
    rsi = tech.get("rsi")
    if rsi is not None:
        return "long" if rsi >= 50 else "short"
    return "long"


def score_day_trade_setup(technical: dict, sentiment: dict, market_cap_usd=None, volume=None, avg_volume=None):
    tech = technical or {}
    sent = sentiment or {}

    components = [
        ("liquidity", *_liquidity_score(market_cap_usd)),
        ("volatility", *_volatility_score(tech.get("volatility_pct"))),
        ("momentum", *_momentum_score(tech.get("rsi"))),
        ("trend", *_trend_score(tech.get("macd_hist"), tech.get("momentum_10d_pct"))),
        ("catalyst", *_catalyst_score(sent.get("big_news"))),
        ("inflection", *_inflection_score(tech.get("range_position_pct"))),
        ("relative_volume", *_relative_volume_score(volume, avg_volume)),
        ("extension", *_extension_score(tech.get("momentum_10d_pct"))),
    ]

    parts = [(WEIGHTS[key], score) for key, score, note in components if score is not None]
    notes = [note for _key, score, note in components if note is not None]
    direction = determine_direction(tech)

    if not parts:
        return {"day_trade_score": None, "day_trade_rating": None, "direction": direction,
                "notes": ["Not enough technical data to score a day-trade setup."]}

    total_w = sum(w for w, _ in parts)
    score = round(sum(w * s for w, s in parts) / total_w, 1)

    if score >= 70:
        rating = "Prime setup"
    elif score >= 50:
        rating = "Worth watching"
    else:
        rating = "Not today"

    notes = [f"Leaning {direction.upper()} on short-term momentum."] + notes

    return {"day_trade_score": score, "day_trade_rating": rating, "direction": direction, "notes": notes}
