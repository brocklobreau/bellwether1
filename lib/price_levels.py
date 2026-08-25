"""
Concrete entry/exit PRICE zones for a trade, not just a signal word.
Two distinct horizons, because "a good price" means something different
depending on the style:

  compute_investing_levels  -- long-horizon: moving averages, 52-week range,
                                analyst price target. For a position you might
                                hold for months.
  compute_day_trade_levels  -- short-horizon: 5/10-session swing points and
                                the stock's own recent volatility. For a trade
                                meant to resolve in hours to a few days.

Everything here is derived from real, already-fetched numbers -- no invented
precision -- and returned as zones/ranges, because that's what the underlying
data actually supports, never a single "magic number".

Caveat that matters: both are built from DAILY closes (see RUNBOOK.md), not
live intraday ticks -- the day-trade zones are genuinely short-horizon swing
levels, not minute-by-minute scalping levels.
"""

# Common field shape returned by both functions:
#   entry_zone: (low, high) -- price range where entering is attractive
#   exit_zone:  (low, high) -- price range where taking profit is attractive
#   stop: a single downside risk-management level, below entry_zone
#   in_entry_zone / at_exit_target: bool flags against the current price
#   notes: plain-language explanation of where each number came from


def _support_resistance(closes, window):
    if not closes:
        return None, None
    w = closes[-window:] if len(closes) >= window else closes
    return min(w), max(w)


def compute_investing_levels(closes, price, sma20=None, sma50=None,
                              price_52w_low=None, price_52w_high=None,
                              analyst_upside_pct=None):
    """Long-horizon entry/exit zones for a buy-and-hold position."""
    if not closes or price is None:
        return {"entry_zone": None, "exit_zone": None, "stop": None, "notes": []}

    low20, high20 = _support_resistance(closes, 20)
    low50, high50 = _support_resistance(closes, 50)

    notes = []

    # --- Entry zone: nearest real support(s) at/below current price ---
    # A months-long hold has more room than a day trade, but a support level
    # from before a large recent gap (a real move that already happened) is
    # still not "nearby support" -- it's a stale pre-move price. Cap how far
    # below current price a candidate can sit before it's treated as stale
    # rather than a real level to wait for. (added 2026-08-21, same fix as
    # compute_day_trade_levels below, user-reported)
    ENTRY_REACH_PCT = 0.35
    support_candidates = sorted({round(x, 2) for x in [sma20, sma50, low20, low50, price_52w_low] if x})
    below = [s for s in support_candidates if s < price and s >= price * (1 - ENTRY_REACH_PCT)]
    if below:
        entry_high = max(below)
        entry_low = min(below) if len(below) > 1 else round(entry_high * 0.95, 2)
        notes.append(
            f"Entry zone (${entry_low:,.2f}-${entry_high:,.2f}) is built from nearby technical "
            f"support: recent swing lows and moving averages currently sitting below price."
        )
    else:
        entry_high = price
        entry_low = round(price * 0.95, 2)
        notes.append(
            f"Price (${price:,.2f}) is already at or below its recent technical support levels — "
            f"there isn't much room below before it, so the entry zone is tight around current price."
        )

    # --- Exit zone: nearest resistance / target(s) at/above current price ---
    resistance_candidates = [x for x in [high20, high50, price_52w_high] if x]
    analyst_target = None
    if analyst_upside_pct is not None:
        analyst_target = round(price * (1 + analyst_upside_pct / 100), 2)
        resistance_candidates.append(analyst_target)
    above = sorted({round(x, 2) for x in resistance_candidates if x > price})
    if above:
        exit_low = above[0]
        exit_high = above[-1]
        src = []
        if analyst_target and analyst_target in (exit_low, exit_high):
            src.append("the analyst consensus price target")
        if price_52w_high and round(price_52w_high, 2) in (exit_low, exit_high):
            src.append("its 52-week high")
        if not src:
            src.append("recent swing highs")
        notes.append(
            f"Exit/take-profit zone (${exit_low:,.2f}-${exit_high:,.2f}) is anchored to "
            + " and ".join(src) + "."
        )
    else:
        exit_low = round(price * 1.03, 2)
        exit_high = round(price * 1.08, 2)
        notes.append(
            f"Price (${price:,.2f}) has already pushed past its usual resistance markers (recent highs"
            + (", 52-week high," if price_52w_high and price >= price_52w_high else "")
            + " and/or the analyst target) — treat any further upside target loosely."
        )

    stop = round(entry_low * 0.92, 2)
    notes.append(
        f"Suggested protective stop: ${stop:,.2f} (~8% below the entry zone) -- a common risk-management "
        f"guideline for a long-term position, not a prediction it will get there."
    )

    in_entry_zone = entry_low <= price <= entry_high * 1.02
    at_exit_target = price >= exit_low * 0.98

    return {
        "entry_zone": (entry_low, entry_high),
        "exit_zone": (exit_low, exit_high),
        "stop": stop,
        "analyst_target": analyst_target,
        "in_entry_zone": in_entry_zone,
        "at_exit_target": at_exit_target,
        "notes": notes,
    }


def compute_day_trade_levels(closes, price, volatility_pct=None, direction="long"):
    """Short-horizon entry/exit zones sized off 5/10-session swing points and
    the stock's own recent volatility -- meant for a trade resolving in hours
    to a few days, not a months-long hold.

    direction="long" (default): buy low near support, sell high near
    resistance, stop below entry -- the original behavior.
    direction="short": mirror image -- sell/short near resistance, cover
    (target) near support, stop ABOVE entry, since the losing direction for
    a short is upside. Pick direction from lib.day_trade_score.determine_direction
    (short-term momentum), not from the long-horizon composite signal --
    day trades work off their own, faster read on direction.

    Also returns risk_reward_ratio: reward (entry to target) divided by risk
    (entry to stop), using the near-price edge of each zone as the reference
    point -- the fill you're most likely to actually get, not the optimistic
    edge. None if the stop and entry coincide (can't divide by zero risk)."""
    if not closes or price is None:
        return {"entry_zone": None, "exit_zone": None, "stop": None, "direction": direction,
                "risk_reward_ratio": None, "notes": []}

    vol = volatility_pct if volatility_pct is not None else 1.5  # sane fallback if unmeasured

    low5, high5 = _support_resistance(closes, 5)
    low10, high10 = _support_resistance(closes, 10)

    # A day trade is meant to resolve in hours to a few days -- a swing level
    # more than ~20% away from the current price isn't a realistic zone for
    # that horizon. It usually means the 5/10-session window straddles a
    # large recent gap (a real move that already happened -- earnings,
    # a catalyst, a squeeze) and is offering up a pre-gap price as if it
    # were still live support/resistance. Reject those rather than propose
    # an entry the stock would have to round-trip an already-finished move
    # to ever reach. (added 2026-08-21, user-reported: a "buy the dip" entry
    # zone sitting 55%+ below a stock that had already spiked days earlier)
    REACH_PCT = 0.20
    notes = []

    if direction == "short":
        # Entry: near resistance (recent highs) -- short into strength.
        resistance_candidates = sorted({round(x, 2) for x in [high5, high10] if x})
        above = [r for r in resistance_candidates if r > price and r <= price * (1 + REACH_PCT)]
        if above:
            entry_low = min(above)
            entry_high = max(above) if len(above) > 1 else round(entry_low * (1 + vol / 100), 2)
            notes.append(
                f"Short entry zone (${entry_low:,.2f}-${entry_high:,.2f}) uses the last 5-10 sessions' swing highs."
            )
        else:
            entry_low = round(price * (1 + 0.3 * vol / 100), 2)
            entry_high = round(price * (1 + 1.2 * vol / 100), 2)
            notes.append(
                f"Price is already through its recent short-term highs, so the short entry zone is sized off "
                f"volatility (~{vol:.1f}%/day) instead of a real resistance level."
            )

        # Target (cover): near support (recent lows).
        support_candidates = sorted({round(x, 2) for x in [low5, low10] if x})
        below = [s for s in support_candidates if s < price and s >= price * (1 - REACH_PCT)]
        if below:
            exit_high = max(below)
            exit_low = min(below) if len(below) > 1 else round(exit_high * (1 - vol / 100), 2)
            notes.append(
                f"Cover/target zone (${exit_low:,.2f}-${exit_high:,.2f}) uses the last 5-10 sessions' pullback lows."
            )
        else:
            exit_high = round(price * (1 - 0.8 * vol / 100), 2)
            exit_low = round(price * (1 - 2 * vol / 100), 2)
            notes.append(
                f"No recent swing low sits below price, so the cover/target zone is sized off "
                f"volatility (~{vol:.1f}%/day) instead."
            )

        stop = round(entry_high * (1 + vol / 100), 2)
        notes.append(
            f"Tight stop ${stop:,.2f} (~one typical day's move above entry) -- a short needs a fast exit "
            f"if it squeezes higher, not an 8% cushion."
        )

        in_entry_zone = entry_low * 0.99 <= price <= entry_high
        at_exit_target = price <= exit_high * 1.01
        entry_ref, target_ref = entry_low, exit_high  # near-price edges
        risk = stop - entry_ref
        reward = entry_ref - target_ref

    else:
        support_candidates = sorted({round(x, 2) for x in [low5, low10] if x})
        below = [s for s in support_candidates if s < price and s >= price * (1 - REACH_PCT)]
        if below:
            entry_high = max(below)
            entry_low = min(below) if len(below) > 1 else round(entry_high * (1 - vol / 100), 2)
            notes.append(
                f"Entry zone (${entry_low:,.2f}-${entry_high:,.2f}) uses the last 5-10 sessions' pullback lows."
            )
        else:
            entry_high = round(price * (1 - 0.3 * vol / 100), 2)
            entry_low = round(price * (1 - 1.2 * vol / 100), 2)
            notes.append(
                f"No recent swing low sits below price, so the entry zone is sized off the stock's own "
                f"volatility (~{vol:.1f}%/day) instead of a real pullback level."
            )

        resistance_candidates = sorted({round(x, 2) for x in [high5, high10] if x})
        above = [r for r in resistance_candidates if r > price and r <= price * (1 + REACH_PCT)]
        if above:
            exit_low = min(above)
            exit_high = max(above) if len(above) > 1 else round(exit_low * (1 + vol / 100), 2)
            notes.append(
                f"Target zone (${exit_low:,.2f}-${exit_high:,.2f}) uses the last 5-10 sessions' swing highs."
            )
        else:
            exit_low = round(price * (1 + 0.8 * vol / 100), 2)
            exit_high = round(price * (1 + 2 * vol / 100), 2)
            notes.append(
                f"Price is already through its recent short-term highs, so the target zone is sized off "
                f"volatility (~{vol:.1f}%/day) instead."
            )

        stop = round(entry_low * (1 - vol / 100), 2)
        notes.append(
            f"Tight stop ${stop:,.2f} (~one typical day's move below entry) -- a day trade needs a fast exit "
            f"if it's wrong, not an 8% cushion."
        )

        in_entry_zone = entry_low <= price <= entry_high * 1.01
        at_exit_target = price >= exit_low * 0.99
        entry_ref, target_ref = entry_high, exit_low  # near-price edges
        risk = entry_ref - stop
        reward = target_ref - entry_ref

    risk_reward_ratio = round(reward / risk, 2) if risk and risk > 0 else None
    if risk_reward_ratio is not None:
        verdict = "a favorable setup" if risk_reward_ratio >= 2 else ("borderline" if risk_reward_ratio >= 1 else "a poor risk/reward setup")
        notes.append(f"Risk/reward: {risk_reward_ratio:.1f}:1 (reward vs. risk to the near edges of each zone) — {verdict}.")

    return {
        "entry_zone": (entry_low, entry_high),
        "exit_zone": (exit_low, exit_high),
        "stop": stop,
        "direction": direction,
        "risk_reward_ratio": risk_reward_ratio,
        # near-price edges used for the R:R math above -- also the realistic
        # reference price for lib.position_sizing (the fill you'd actually
        # expect, not the optimistic edge of the zone)
        "entry_ref": entry_ref,
        "target_ref": target_ref,
        "in_entry_zone": in_entry_zone,
        "at_exit_target": at_exit_target,
        "notes": notes,
    }
