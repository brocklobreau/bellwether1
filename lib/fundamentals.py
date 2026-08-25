"""
Fundamental scoring. Takes a plain dict of metrics (already fetched/parsed
via WebFetch from a finance data page) and produces a 0-100 score + notes.

Expected input keys (all optional -- missing ones are just skipped):
  pe_ratio, forward_pe, revenue_growth_pct, profit_margin_pct,
  dividend_yield_pct, analyst_rating (str), analyst_upside_pct,
  debt_to_equity, fcf_margin_pct, market_cap_usd, sector

debt_to_equity and fcf_margin_pct (added 2026-08-21) both come from the same
/stable/ratios fetch already used for pe_ratio/profit_margin_pct/dividend --
debtToEquityRatio directly, fcf_margin_pct derived as
freeCashFlowPerShare / revenuePerShare * 100. No new API calls needed. A
company can carry a healthy net margin while over-levered or burning cash
despite it -- these two catch what margin alone misses, which matters more
over a months-long hold than a quick trade.
"""


def score_fundamentals(m: dict):
    notes = []
    points = []  # (weight, score_0_100, label)

    pe = m.get("pe_ratio")
    fwd_pe = m.get("forward_pe")
    if pe is not None:
        # Rough heuristic: <15 cheap, 15-25 fair, 25-40 growth-priced, >40 expensive.
        # Blue chips often sit 20-35, so center the "fair" zone there.
        if pe <= 0:
            pe_score = 30
            notes.append("Negative/undefined P/E (unprofitable or distorted earnings).")
        elif pe < 15:
            pe_score = 85
            notes.append(f"P/E {pe} — cheap relative to typical large-cap norms.")
        elif pe < 25:
            pe_score = 70
            notes.append(f"P/E {pe} — reasonably valued.")
        elif pe < 35:
            pe_score = 55
            notes.append(f"P/E {pe} — priced for continued growth.")
        elif pe < 50:
            pe_score = 35
            notes.append(f"P/E {pe} — expensive, priced for a lot to go right.")
        else:
            pe_score = 20
            notes.append(f"P/E {pe} — very expensive by historical norms.")
        if fwd_pe and pe and fwd_pe < pe:
            pe_score += 5
            notes.append(f"Forward P/E ({fwd_pe}) is lower than trailing — earnings expected to grow into the price.")
        points.append((0.30, max(0, min(100, pe_score)), "Valuation (P/E)"))

    growth = m.get("revenue_growth_pct")
    if growth is not None:
        growth_score = 50 + max(-40, min(40, growth * 2.5))
        notes.append(f"Revenue growth: {growth:+.1f}% YoY.")
        points.append((0.25, max(0, min(100, growth_score)), "Growth"))

    margin = m.get("profit_margin_pct")
    if margin is not None:
        if margin < 0:
            margin_score = 15
            notes.append(f"Profit margin negative ({margin:.1f}%) — unprofitable.")
        else:
            margin_score = min(100, 30 + margin * 2.2)
            notes.append(f"Profit margin {margin:.1f}%.")
        points.append((0.20, margin_score, "Profitability"))

    div = m.get("dividend_yield_pct")
    if div is not None:
        # Mild bonus for a healthy (not distress-level) yield; not a big driver.
        if div == 0:
            div_score = 45
        elif div < 4:
            div_score = 55 + div * 3
        else:
            div_score = 60  # very high yield can signal distress, don't over-reward
            notes.append(f"Dividend yield {div:.2f}% — high; worth checking payout sustainability.")
        if div and div < 4:
            notes.append(f"Dividend yield {div:.2f}%.")
        points.append((0.10, max(0, min(100, div_score)), "Dividend"))

    dte = m.get("debt_to_equity")
    if dte is not None:
        if dte < 0:
            dte_score = 40
            notes.append(f"Debt/equity {dte:.2f} — negative equity, treat with caution.")
        elif dte < 0.5:
            dte_score = 90
            notes.append(f"Debt/equity {dte:.2f} — low leverage, strong balance sheet.")
        elif dte < 1.0:
            dte_score = 75
            notes.append(f"Debt/equity {dte:.2f} — healthy leverage.")
        elif dte < 2.0:
            dte_score = 55
            notes.append(f"Debt/equity {dte:.2f} — moderate leverage.")
        elif dte < 4.0:
            dte_score = 35
            notes.append(f"Debt/equity {dte:.2f} — elevated leverage (some of this is normal for financials/utilities — check sector norms before reading too much into it alone).")
        else:
            dte_score = 20
            notes.append(f"Debt/equity {dte:.2f} — heavy leverage.")
        points.append((0.10, dte_score, "Balance sheet"))

    fcf_margin = m.get("fcf_margin_pct")
    if fcf_margin is not None:
        if fcf_margin < 0:
            fcf_score = 15
            notes.append(f"Free cash flow margin {fcf_margin:.1f}% — burning cash.")
        else:
            fcf_score = max(0, min(100, 40 + fcf_margin * 2.5))
            tone = "strong" if fcf_margin >= 15 else "positive"
            notes.append(f"Free cash flow margin {fcf_margin:.1f}% — {tone} cash generation.")
        points.append((0.10, fcf_score, "Cash flow"))

    rating = (m.get("analyst_rating") or "").lower()
    upside = m.get("analyst_upside_pct")
    if rating or upside is not None:
        rating_score = 50
        if "strong buy" in rating:
            rating_score = 85
        elif "buy" in rating:
            rating_score = 70
        elif "hold" in rating or "neutral" in rating:
            rating_score = 50
        elif "sell" in rating:
            rating_score = 25
        if upside is not None:
            rating_score += max(-20, min(20, upside))
            notes.append(f"Analyst consensus: {m.get('analyst_rating', 'n/a')}, price target implies {upside:+.1f}% from here.")
        elif rating:
            notes.append(f"Analyst consensus: {m.get('analyst_rating')}.")
        points.append((0.15, max(0, min(100, rating_score)), "Analyst consensus"))

    if not points:
        return {
            "fundamental_score": None, "notes": ["No fundamental data available."],
            "market_cap_usd": m.get("market_cap_usd"), "sector": m.get("sector"),
        }

    total_weight = sum(w for w, _, _ in points)
    fundamental_score = round(sum(w * s for w, s, _ in points) / total_weight, 1)

    return {
        "fundamental_score": fundamental_score,
        "pe_ratio": pe,
        "forward_pe": fwd_pe,
        "revenue_growth_pct": growth,
        "profit_margin_pct": margin,
        "dividend_yield_pct": div,
        "debt_to_equity": dte,
        "fcf_margin_pct": fcf_margin,
        "analyst_rating": m.get("analyst_rating"),
        "analyst_upside_pct": upside,
        "market_cap_usd": m.get("market_cap_usd"),
        "sector": m.get("sector"),
        "notes": notes,
    }
