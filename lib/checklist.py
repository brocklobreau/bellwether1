"""
Two transparent, rules-based checklists over the same underlying data --
one tuned for short-term day trading, one for long-term investing. Each
criterion is a plain pass/fail rule with a human-readable reason, so the
"why" is visible at a glance rather than buried in a single opaque score.

Both take the sub-score dicts already produced by indicators/fundamentals/
sentiment (plus price and price_levels) -- no network, no new data needed
beyond what score_ticker already collects.
"""


def _item(label, passed, detail):
    return {"label": label, "passed": bool(passed), "detail": detail}


def day_trade_checklist(technical: dict, fundamental: dict, sentiment: dict, price):
    items = []

    mcap = fundamental.get("market_cap_usd")
    liquid = (mcap is not None and mcap >= 10_000_000_000)
    items.append(_item(
        "Liquid",
        liquid,
        f"Market cap ${mcap/1e9:,.0f}B — large-cap, easy to get in and out." if mcap
        else "Market cap unknown — verify liquidity before sizing a trade."
    ))

    vol = technical.get("volatility_pct")
    volatile = vol is not None and vol >= 1.5
    items.append(_item(
        "Volatile enough",
        volatile,
        f"Averaging {vol:.1f}%/day over the last 20 sessions." if vol is not None
        else "Not enough price history to gauge daily volatility."
    ))

    rsi = technical.get("rsi")
    directional = rsi is not None and (rsi <= 35 or rsi >= 60)
    items.append(_item(
        "Momentum, not dead zone",
        directional,
        f"RSI {rsi} — {'clear directional push' if directional else 'stuck in the 35-60 no-mans-land'}."
        if rsi is not None else "RSI unavailable."
    ))

    price_val, sma20, hist = price, technical.get("sma20"), technical.get("macd_hist")
    trend_confirmed = None
    if price_val is not None and sma20 is not None and hist is not None:
        trend_confirmed = (price_val > sma20 and hist > 0) or (price_val < sma20 and hist < 0)
    items.append(_item(
        "Trend confirmed",
        bool(trend_confirmed),
        "Price vs. 20-day average and MACD histogram agree on direction." if trend_confirmed
        else "Short-term trend and momentum are pulling different ways (or data's missing)."
    ))

    big_news = sentiment.get("big_news", False)
    items.append(_item(
        "News catalyst today",
        big_news,
        "There's a real headline behind the move, not just drift."
        if big_news else "No standout headline in the recent news pull."
    ))

    low20, high20 = technical.get("recent_low_20d"), technical.get("recent_high_20d")
    near_level = False
    if price_val is not None and low20 and high20 and high20 > low20:
        band = (high20 - low20) * 0.08
        near_level = (price_val - low20 <= band) or (high20 - price_val <= band)
    items.append(_item(
        "At an inflection point",
        near_level,
        "Trading within ~8% of its 20-day high or low — near a level that tends to trigger a move."
        if near_level else "Sitting mid-range, away from its recent high or low."
    ))

    passed = sum(1 for i in items if i["passed"])
    total = len(items)
    if passed >= 5:
        rating = "Prime setup"
    elif passed >= 3:
        rating = "Worth watching"
    else:
        rating = "Not today"

    return {"items": items, "passed": passed, "total": total, "rating": rating}


def investing_checklist(technical: dict, fundamental: dict, sentiment: dict, price, price_levels: dict):
    items = []

    pe = fundamental.get("pe_ratio")
    growth = fundamental.get("revenue_growth_pct")
    reasonable_valuation = pe is not None and 0 < pe < 40
    items.append(_item(
        "Reasonable valuation",
        reasonable_valuation,
        f"P/E {pe} — {'within a normal range for a quality large-cap' if reasonable_valuation else 'stretched or undefined'}."
        if pe is not None else "P/E unavailable."
    ))

    growing = growth is not None and growth >= 5
    items.append(_item(
        "Growing revenue",
        growing,
        f"Revenue up {growth:+.1f}% YoY." if growth is not None else "Growth figure unavailable."
    ))

    margin = fundamental.get("profit_margin_pct")
    profitable = margin is not None and margin >= 10
    items.append(_item(
        "Genuinely profitable",
        profitable,
        f"Profit margin {margin:.1f}%." if margin is not None else "Margin unavailable."
    ))

    rating = (fundamental.get("analyst_rating") or "").lower()
    analyst_confident = "buy" in rating
    items.append(_item(
        "Analysts on board",
        analyst_confident,
        f"Consensus: {fundamental.get('analyst_rating')}." if rating else "No analyst consensus fetched."
    ))

    sma50 = technical.get("sma50")
    long_uptrend = price is not None and sma50 is not None and price > sma50
    items.append(_item(
        "Long-term uptrend",
        long_uptrend,
        "Price is above its 50-day average." if sma50 is not None else "Not enough history for a 50-day trend read."
    ))

    reasonable_entry = False
    buy_zone = (price_levels or {}).get("entry_zone")
    if buy_zone and price is not None:
        reasonable_entry = price <= buy_zone[1] * 1.03
    items.append(_item(
        "Not chasing the price",
        reasonable_entry,
        "Price is at or near its buy zone rather than stretched above it." if reasonable_entry
        else "Price is running well above its computed buy zone — you'd be paying up here."
    ))

    passed = sum(1 for i in items if i["passed"])
    total = len(items)
    if passed >= 5:
        rating_label = "Strong candidate"
    elif passed >= 3:
        rating_label = "Reasonable, some caveats"
    else:
        rating_label = "Not compelling right now"

    return {"items": items, "passed": passed, "total": total, "rating": rating_label}
