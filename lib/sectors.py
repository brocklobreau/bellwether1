"""
Sector classification for the concentration check. Prefers live data (FMP's
company-profile `sector` field, passed through fund_input/score_fundamentals
as `sector`) when the pipeline has fetched it; falls back to a small static
map of common large-caps for tickers that haven't had a profile pull yet, so
the feature has something to show from day one instead of "Unknown" for
everything. Real classifications win the moment real data shows up.
"""

# GICS-ish sectors for names likely to show up in the watchlist, screener
# universe, or as discovered candidates. Not exhaustive -- anything missing
# falls through to "Other / Unclassified" rather than guessing.
SECTOR_FALLBACK = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD": "Technology", "ADBE": "Technology", "CRM": "Technology",
    "AVGO": "Technology", "TXN": "Technology", "ORCL": "Technology",
    "GOOGL": "Communication Services", "META": "Communication Services",
    "NFLX": "Communication Services", "DIS": "Communication Services",
    "AMZN": "Consumer Discretionary", "HD": "Consumer Discretionary",
    "MCD": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BRK.B": "Financials", "COIN": "Financials", "MARA": "Financials",
    "JNJ": "Health Care", "UNH": "Health Care", "ABBV": "Health Care",
    "MRK": "Health Care", "MRNA": "Health Care",
    "PG": "Consumer Staples", "KO": "Consumer Staples", "COST": "Consumer Staples",
    "PEP": "Consumer Staples", "WMT": "Consumer Staples",
    "XOM": "Energy",
    "LIN": "Materials",
    "CAT": "Industrials", "GE": "Industrials", "LMT": "Industrials",
}

UNCLASSIFIED = "Other / Unclassified"


def get_sector(ticker, fund_input=None):
    live = (fund_input or {}).get("sector")
    if live:
        return live
    return SECTOR_FALLBACK.get((ticker or "").upper(), UNCLASSIFIED)


def sector_breakdown(items):
    """items: list of (ticker, weight) tuples -- weight is share count for a
    plain watchlist tally, or dollar market value for a portfolio. Returns
    a list of {sector, value, pct} sorted by value descending, plus a
    plain-language concentration rating based on the largest slice."""
    totals = {}
    grand_total = 0.0
    for ticker, weight, sector in items:
        if weight is None or weight <= 0:
            continue
        totals[sector] = totals.get(sector, 0.0) + weight
        grand_total += weight

    if grand_total <= 0:
        return {"breakdown": [], "rating": None, "top_sector": None, "top_pct": None}

    breakdown = sorted(
        ({"sector": s, "value": v, "pct": round(v / grand_total * 100, 1)} for s, v in totals.items()),
        key=lambda x: -x["value"],
    )
    top = breakdown[0]
    if top["pct"] >= 45:
        rating = "Concentrated"
    elif top["pct"] >= 30:
        rating = "Leaning heavy"
    else:
        rating = "Well diversified"

    return {"breakdown": breakdown, "rating": rating, "top_sector": top["sector"], "top_pct": top["pct"]}
