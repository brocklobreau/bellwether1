"""
Insider-trading signal from FMP's Form 4 filings feed (/stable/insider-trading/search).
No institutional/13F ownership data -- that endpoint returned 402 (payment
required) on the current FMP plan tier, so it's left out rather than faked.

Only genuine open-market transactions carry a real market opinion:
  P-Purchase (acquisitionOrDisposition "A") -- an insider spent their own
    money to buy on the open market. Rare, and meaningful when it happens.
  S-Sale (acquisitionOrDisposition "D") -- an open-market sale. Common and
    often routine (diversification, pre-scheduled 10b5-1 plans), so it's a
    much weaker signal than a purchase -- only worth flagging when several
    different insiders sell in the same window with no offsetting buys.
Everything else in the feed (M-Exempt option/RSU conversions, F-InKind tax
withholding, G-Gift, A-Award grants) isn't an economic decision by the
insider and is excluded from scoring.
"""

PURCHASE_TYPES = {"P-Purchase"}
SALE_TYPES = {"S-Sale"}


def score_insider(filings: list):
    """filings: raw records from FMP's insider-trading/search endpoint (order
    doesn't matter). Returns insider_score (0-100, or None if no open-market
    activity at all) plus the raw counts/values and a plain-language note."""
    filings = filings or []

    buys = [f for f in filings if f.get("transactionType") in PURCHASE_TYPES]
    sells = [f for f in filings if f.get("transactionType") in SALE_TYPES]

    if not buys and not sells:
        return {
            "insider_score": None,
            "insider_buys": 0,
            "insider_sells": 0,
            "insider_buy_value": 0.0,
            "insider_sell_value": 0.0,
            "notes": [],
        }

    buy_value = sum((f.get("price") or 0) * (f.get("securitiesTransacted") or 0) for f in buys)
    sell_value = sum((f.get("price") or 0) * (f.get("securitiesTransacted") or 0) for f in sells)
    distinct_buyers = len({f.get("reportingName") for f in buys if f.get("reportingName")})
    distinct_sellers = len({f.get("reportingName") for f in sells if f.get("reportingName")})

    notes = []
    if buys:
        score = 78 if distinct_buyers >= 2 else 68
        who = f"{distinct_buyers} insider{'s' if distinct_buyers != 1 else ''}"
        notes.append(
            f"{who} bought ${buy_value:,.0f} on the open market recently — a real bullish "
            f"signal, since insiders spend their own money to buy and rarely do it casually."
        )
        if sells:
            notes.append(
                f"(Also {distinct_sellers} insider sale(s) worth ${sell_value:,.0f} in the same "
                f"window — unremarkable alongside a purchase.)"
            )
    elif distinct_sellers >= 3:
        score = 42
        notes.append(
            f"{distinct_sellers} different insiders sold a combined ${sell_value:,.0f} recently with "
            f"no offsetting purchases — worth a second look, though routine diversification or "
            f"10b5-1 plans are still the likeliest explanation."
        )
    else:
        score = 50
        notes.append(
            "Recent insider activity is limited or routine (a sale or two, no clustering, no "
            "purchases) — not a real signal either way."
        )

    return {
        "insider_score": score,
        "insider_buys": len(buys),
        "insider_sells": len(sells),
        "insider_buy_value": round(buy_value, 2),
        "insider_sell_value": round(sell_value, 2),
        "notes": notes,
    }
