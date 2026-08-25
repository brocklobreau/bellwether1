"""
Portfolio drift / rebalance flag: a position built at a modest weight can
grow well past a sane single-name concentration purely from price
appreciation (or from sizing that wasn't disciplined to begin with).
This is a DIFFERENT risk than lib.sectors's cross-position sector
concentration check -- that one is about correlated sector exposure across
many positions, this one is about a single ticker's weight regardless of
sector. Pure math over lib.portfolio.compute_portfolio's own output; no
network, no new data, no execution -- a flag to consider, not an order to
trim.
"""

MAX_POSITION_PCT = 20   # a tracked position at/above this % of tracked market value gets a "trim" flag
WATCH_POSITION_PCT = 15  # below the hard line but worth a heads-up


def flag_drift(open_positions, tracked_market_value):
    """open_positions / tracked_market_value: straight from
    lib.portfolio.compute_portfolio()'s "open_positions" and
    totals["tracked_market_value"]. Returns {ticker: {"weight_pct", "flag",
    "note"}} for every TRACKED position (untracked positions have no live
    price to weigh against, so they're skipped -- same convention the rest
    of the Portfolio tab already uses)."""
    out = {}
    if not tracked_market_value:
        return out
    for p in open_positions:
        if not p.get("tracked") or p.get("current_price") is None:
            continue
        value = p["current_price"] * p["shares"]
        weight_pct = round(value / tracked_market_value * 100, 1)
        if weight_pct >= MAX_POSITION_PCT:
            flag = "trim"
            note = (f"{weight_pct:.1f}% of the tracked portfolio — at/above the {MAX_POSITION_PCT}% "
                     "single-position guideline, worth trimming back toward target.")
        elif weight_pct >= WATCH_POSITION_PCT:
            flag = "watch"
            note = f"{weight_pct:.1f}% of the tracked portfolio — approaching the {MAX_POSITION_PCT}% single-position guideline."
        else:
            flag, note = None, None
        out[p["ticker"]] = {"weight_pct": weight_pct, "flag": flag, "note": note}
    return out
