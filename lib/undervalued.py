"""
Deep-value / "hidden gem" scoring -- the third screen, added 2026-08-28 at
user request: "find super undervalued stocks that are doing great things"
and "actually find the stocks before they pop".

WHAT THIS CAN AND CANNOT DO -- read this before trusting the output.

Nothing here predicts that a stock is about to rise. No screener can; if a
public formula could reliably time a re-rating, the re-rating would already
have happened. What this DOES do is enforce, mechanically and every cycle,
the three conditions that have to be true for a "before it pops" candidate
to even be possible:

  1. It is genuinely cheap on its own numbers (not merely cheaper than a
     peer, not cheap because someone called it cheap).
  2. The business underneath is actually good -- profitable, cash-
     generative, not drowning in debt, not shrinking. This is the half
     that separates a bargain from a value trap, and it is the half most
     "cheap stock" lists skip entirely.
  3. It has NOT already run. This is the literal, checkable version of
     "before it pops": a stock sitting near its 52-week high, or one that
     just jumped 40% in two weeks, has already had the move -- whatever
     the thesis was, the market has priced it. Those are vetoed outright
     here rather than scored down, because a high score on an already-
     extended name is exactly the failure mode that makes a screen like
     this useless (the same lesson already learned on the day-trade side;
     see day_trade_score._extension_score, added 2026-08-21).

So: this narrows thousands of stocks to a handful that are cheap, sound,
and still early. Some of them will re-rate. Some will stay cheap for years
-- that is what "the market disagrees with you" looks like, and it is the
normal outcome, not a bug. Position sizes should reflect that.

The upside number this produces is a RE-RATING ESTIMATE with its arithmetic
shown (see estimate_fair_value) -- what the stock would be worth if it
traded at a multiple appropriate to its own growth and margins. It is not a
forecast, has no time attached to it, and is deliberately capped.

No network calls anywhere in this module -- everything operates on dicts
that scripts/refresh.py has already fetched.
"""

# --- Hard vetoes -------------------------------------------------------
# Deliberately vetoes rather than score penalties: a name failing any of
# these is not a "lower-ranked gem", it is a different thing entirely and
# should not appear on this screen at any score.

MAX_RANGE_POSITION = 65.0      # % of 52w range; above this the move already happened
MAX_RECENT_RUN_PCT = 35.0      # 10-session gain above which this is a chase, not an entry
MAX_DEBT_TO_EQUITY = 5.0       # beyond this, the equity is an option on the creditors' mercy
TRAP_REVENUE_DECLINE = -15.0   # shrinking this fast AND burning cash = classic value trap
MAX_PE_FOR_VALUE_SCREEN = 45.0 # above this it is not a value candidate by any definition
MIN_RERATING_UPSIDE = 15.0     # below this there is no discount worth calling undervalued
MIN_SCORE_TO_SHOW = 58.0       # a weak "gem" is not a gem; show nothing rather than filler

# --- Prescreen (cheap stage) -------------------------------------------
# Runs on ONE /stable/ratios call per candidate instead of the full 10-call
# deep fetch, so a few hundred names can be triaged for the cost of a couple
# dozen deep ones. Same cheap-prefilter-then-expensive-score shape already
# used by the day-trade and investing screens.


MIN_NET_MARGIN_FOR_QUALITY = 0.02  # 2% -- below this "profitable" is a rounding error


def _band(value, bands, default):
    """LOWER IS BETTER. bands: list of (upper_bound_exclusive, score),
    ascending by bound. Returns the first score whose bound the value is
    under, else default."""
    if value is None:
        return None
    for bound, score in bands:
        if value < bound:
            return score
    return default


def _band_high(value, bands, default):
    """HIGHER IS BETTER. bands: list of (lower_bound_inclusive, score),
    descending by bound. Returns the first score whose bound the value
    reaches, else default. Exists so growth/margin/ROE scoring reads the way
    it actually works instead of being negated into _band."""
    if value is None:
        return None
    for bound, score in bands:
        if value >= bound:
            return score
    return default


def prescore_value_candidate(ratios_row):
    """Cheap first-pass triage from a single /stable/ratios response.

    Returns (score_0_100, reject_reason). A non-None reject_reason means the
    name should be dropped before spending a deep fetch on it -- these are
    the disqualifications that are already certain from the ratios alone, so
    there is no point paying ten more API calls to confirm them.

    Field names match what build_fund_input already pulls off this same
    endpoint in scripts/refresh.py, so this adds no new API surface."""
    if not ratios_row:
        return None, "no ratios data"

    pe = ratios_row.get("priceToEarningsRatio")
    npm = ratios_row.get("netProfitMargin")
    dte = ratios_row.get("debtToEquityRatio")
    fcf_ps = ratios_row.get("freeCashFlowPerShare")
    rev_ps = ratios_row.get("revenuePerShare")
    pb = ratios_row.get("priceToBookRatio")
    pfcf = ratios_row.get("priceToFreeCashFlowRatio")
    roe = ratios_row.get("returnOnEquity")

    # Profitability is a precondition, not a scoring factor. An unprofitable
    # company has no meaningful P/E, so "undervalued" can't be established on
    # the terms this screen uses -- it would just be a cheap-looking share
    # price, which is not the same thing at all.
    if pe is None:
        return None, "no P/E available"
    if pe <= 0:
        return None, "unprofitable (negative/undefined P/E)"
    if pe > MAX_PE_FOR_VALUE_SCREEN:
        return None, f"P/E {pe:.1f} — not a value candidate"
    if npm is not None and npm < MIN_NET_MARGIN_FOR_QUALITY:
        return None, f"net margin {npm * 100:.1f}% — too thin to call this a quality business"
    if dte is not None and dte > MAX_DEBT_TO_EQUITY:
        return None, f"debt/equity {dte:.1f} — over-levered"

    fcf_margin = None
    if fcf_ps is not None and rev_ps:
        fcf_margin = fcf_ps / rev_ps * 100

    cheap_parts = []
    pe_s = _band(pe, [(8, 100), (12, 92), (16, 80), (20, 68), (25, 52), (30, 38)], 22)
    cheap_parts.append((0.5, pe_s))
    pb_s = _band(pb, [(1.0, 100), (1.5, 88), (2.5, 72), (4.0, 55), (6.0, 38)], 22)
    if pb_s is not None:
        cheap_parts.append((0.25, pb_s))
    pfcf_s = _band(pfcf, [(8, 100), (12, 88), (18, 74), (25, 58), (40, 38)], 22)
    if pfcf_s is not None:
        cheap_parts.append((0.25, pfcf_s))

    qual_parts = []
    if npm is not None:
        qual_parts.append((0.30, _band_high(npm * 100, [(20, 100), (12, 86), (7, 70), (3, 55)], 38)))
    if roe is not None:
        qual_parts.append((0.25, _band_high(roe, [(0.20, 100), (0.12, 84), (0.07, 66), (0.03, 48)], 25)))
    if fcf_margin is not None:
        qual_parts.append((0.25, _band_high(fcf_margin, [(15, 100), (8, 86), (3, 68), (0, 52)], 25)))
    if dte is not None:
        qual_parts.append((0.20, _band(dte, [(0.3, 100), (0.8, 86), (1.5, 68), (2.5, 50), (4.0, 32)], 18)))

    cheap = sum(w * s for w, s in cheap_parts) / sum(w for w, _ in cheap_parts)
    if qual_parts:
        qual = sum(w * s for w, s in qual_parts) / sum(w for w, _ in qual_parts)
    else:
        # No quality data at all -- treat as mediocre, never as fine. An
        # unverified-quality cheap stock is exactly the value-trap shape.
        qual = 45.0

    # MULTIPLICATIVE, not a weighted sum. A weighted sum lets an extreme
    # discount (P/E 6, P/B 0.5) drag a genuinely bad business up to a
    # respectable score -- which is precisely backwards, because a stock is
    # usually that cheap FOR a reason. Here quality acts as a throttle on
    # cheapness: excellent quality lets the full discount count, poor
    # quality mostly cancels it out however cheap the name looks.
    score = cheap * (0.35 + 0.65 * (qual / 100.0))

    return round(score, 1), None


# --- Deep scoring ------------------------------------------------------


def estimate_fair_value(pe, revenue_growth_pct, profit_margin_pct,
                        analyst_upside_pct=None, price=None):
    """What the stock is worth versus what it costs -- the whole point of the
    screen, expressed in dollars rather than only as a percentage.

    The fair multiple is anchored at 12x (roughly where a no-growth,
    average-margin business belongs), adjusted up for real growth and real
    margins, and hard-capped at 26x so a fast grower can't be handed an
    absurd target. Blended 60/40 with analyst consensus upside when that
    exists, as a tether to what people actually modelling the company think.

    fair_value_price is derived from the SAME blended number shown on the
    card, not from the raw multiple -- otherwise the dollar figure and the
    percentage would disagree with each other on screen, which is worse than
    showing neither.

    This is a valuation estimate, not a target and not a forecast: it says
    what the multiple implies today, with no view on whether or when the
    market will agree. Returns a dict (all keys None-safe)."""
    empty = {"upside_pct": None, "fair_pe": None, "current_pe": pe,
             "current_price": price, "fair_value_price": None, "note": None}
    if not pe or pe <= 0:
        return empty

    fair_pe = 12.0
    if revenue_growth_pct is not None:
        # +0.35x of multiple per point of growth, capped -- growth deserves a
        # premium but not an unbounded one.
        fair_pe += max(-4.0, min(9.0, revenue_growth_pct * 0.35))
    if profit_margin_pct is not None:
        fair_pe += max(-3.0, min(5.0, (profit_margin_pct - 8.0) * 0.20))
    fair_pe = max(9.0, min(26.0, fair_pe))

    implied = max(-60.0, min(150.0, (fair_pe / pe - 1) * 100))

    if analyst_upside_pct is not None:
        blended = 0.6 * implied + 0.4 * analyst_upside_pct
        note = (f"Trades at {pe:.1f}x earnings; its growth and margins justify roughly "
                f"{fair_pe:.1f}x, implying {implied:+.0f}% on a re-rating alone. "
                f"Analyst targets imply {analyst_upside_pct:+.0f}%. Blended: {blended:+.0f}%.")
    else:
        blended = implied
        note = (f"Trades at {pe:.1f}x earnings; its growth and margins justify roughly "
                f"{fair_pe:.1f}x, implying {implied:+.0f}% if it re-rates. No analyst "
                f"target available to cross-check that against.")

    blended = round(max(-60.0, min(150.0, blended)), 1)
    fair_price = round(price * (1 + blended / 100.0), 2) if price else None
    if fair_price is not None:
        note += f" That puts fair value near ${fair_price:,.2f} against ${price:,.2f} today."

    return {"upside_pct": blended, "fair_pe": round(fair_pe, 1), "current_pe": pe,
            "current_price": price, "fair_value_price": fair_price, "note": note}


def check_vetoes(fund, tech):
    """The disqualifications. Returns a list of (code, plain_english) -- empty
    means the name survives. Kept separate from scoring and reported on the
    dashboard so a rejected name's reason is visible, not silently swallowed."""
    vetoes = []

    pe = fund.get("pe_ratio")
    if pe is None:
        vetoes.append(("no_pe", "No usable P/E — can't establish it's cheap on earnings."))
    elif pe <= 0:
        vetoes.append(("unprofitable", "Unprofitable — a low price isn't the same as undervalued."))
    elif pe > MAX_PE_FOR_VALUE_SCREEN:
        vetoes.append(("not_cheap", f"P/E {pe:.1f} — not undervalued by any reading."))

    rp = tech.get("range_position_pct")
    if rp is not None and rp > MAX_RANGE_POSITION:
        vetoes.append(("already_popped",
                       f"Already at {rp:.0f}% of its 52-week range — the re-rating has largely "
                       f"happened. This screen is for names that haven't moved yet."))

    mom = tech.get("momentum_10d_pct")
    if mom is not None and mom > MAX_RECENT_RUN_PCT:
        vetoes.append(("already_ran",
                       f"Up {mom:+.0f}% in the last 10 sessions — whatever the catalyst was, "
                       f"it already fired. Buying here is chasing it."))

    dte = fund.get("debt_to_equity")
    if dte is not None and dte > MAX_DEBT_TO_EQUITY:
        vetoes.append(("over_levered",
                       f"Debt/equity {dte:.1f} — the balance sheet, not the valuation, is the "
                       f"story here."))

    growth = fund.get("revenue_growth_pct")
    fcf = fund.get("fcf_margin_pct")
    if growth is not None and fcf is not None and growth < TRAP_REVENUE_DECLINE and fcf < 0:
        vetoes.append(("value_trap",
                       f"Revenue {growth:+.0f}% and free cash flow negative — shrinking and "
                       f"burning cash at once is the classic value trap, not a bargain."))

    return vetoes


def score_undervalued(result):
    """Deep scoring for one already-fetched ticker (a scripts/refresh.py
    score_ticker() result). Returns a dict merged onto that result under
    'undervalued'.

    Weighting reflects what the request actually asked for: cheap (30) and
    good (30) in equal measure, a heavy 25 on not-having-moved-yet because
    that is the whole point of "before it pops", and 15 on catalyst -- the
    smallest weight deliberately, since a catalyst is the least reliable and
    most easily-imagined of the four."""
    fund = result.get("fundamental") or {}
    tech = result.get("technical") or {}
    insider = result.get("insider") or {}
    sent = result.get("sentiment") or {}

    vetoes = check_vetoes(fund, tech)

    pe = fund.get("pe_ratio")
    growth = fund.get("revenue_growth_pct")
    margin = fund.get("profit_margin_pct")
    fcf = fund.get("fcf_margin_pct")
    dte = fund.get("debt_to_equity")
    upside_analyst = fund.get("analyst_upside_pct")
    rp = tech.get("range_position_pct")
    mom = tech.get("momentum_10d_pct")

    notes = []
    parts = []  # (weight, score)

    # 1. Cheapness ------------------------------------------------------
    if pe is not None and pe > 0:
        pe_s = _band(pe, [(8, 100), (12, 92), (16, 80), (20, 66), (25, 50), (30, 36)], 20)
        parts.append((0.30, pe_s))
        if pe < 12:
            notes.append(f"P/E {pe:.1f} — genuinely cheap, not just cheaper than peers.")
        elif pe < 20:
            notes.append(f"P/E {pe:.1f} — modestly valued.")
        else:
            notes.append(f"P/E {pe:.1f} — only mildly cheap; the discount here is thin.")

    # 2. Quality --------------------------------------------------------
    qual = []
    if growth is not None:
        qual.append(_band_high(growth, [(25, 100), (12, 86), (5, 70), (0, 52)], 28))
        notes.append(f"Revenue {growth:+.1f}% YoY.")
    if margin is not None:
        qual.append(_band_high(margin, [(20, 100), (12, 86), (7, 70), (3, 55)], 38))
        notes.append(f"Net margin {margin:.1f}%.")
    if fcf is not None:
        qual.append(_band_high(fcf, [(15, 100), (8, 86), (3, 68), (0, 52)], 25))
        notes.append(f"Free cash flow margin {fcf:.1f}% — "
                     + ("real cash generation behind the earnings." if fcf > 3
                        else "thin cash conversion; earnings quality is the thing to check."))
    if dte is not None:
        qual.append(_band(dte, [(0.3, 100), (0.8, 86), (1.5, 68), (2.5, 50), (4.0, 32)], 18))
        notes.append(f"Debt/equity {dte:.2f}.")
    if qual:
        parts.append((0.30, sum(qual) / len(qual)))

    # 3. Hasn't moved yet ----------------------------------------------
    timing = []
    if rp is not None:
        # Lower in the 52-week range is better here -- the opposite of a
        # momentum screen, and intentionally so.
        timing.append(max(0.0, min(100.0, 100.0 - rp * 1.15)))
        notes.append(f"Sitting at {rp:.0f}% of its 52-week range — "
                     + ("near the lows, the market hasn't re-rated it." if rp < 35
                        else "mid-range; partially re-rated already."))
    if mom is not None:
        timing.append(_band(abs(mom), [(5, 100), (12, 88), (20, 70), (30, 48)], 28) or 28)
        if abs(mom) < 12:
            notes.append(f"{mom:+.1f}% over 10 sessions — still quiet, no crowd yet.")
        else:
            notes.append(f"{mom:+.1f}% over 10 sessions — starting to move.")
    if timing:
        parts.append((0.25, sum(timing) / len(timing)))

    # 4. Catalyst -------------------------------------------------------
    cat = []
    buys = insider.get("insider_buys") or 0
    if buys:
        cat.append(90 if buys >= 2 else 78)
        notes.append(f"{buys} open-market insider purchase(s) worth "
                     f"${insider.get('insider_buy_value', 0):,.0f} — people who know the business "
                     f"are buying it at this price.")
    elif insider.get("insider_score") is not None:
        cat.append(45)
    if upside_analyst is not None:
        cat.append(max(0.0, min(100.0, 50 + upside_analyst * 1.2)))
    if sent.get("sentiment_score") is not None:
        cat.append(sent["sentiment_score"])
    if cat:
        parts.append((0.15, sum(cat) / len(cat)))

    if not parts:
        return {
            "undervalued_score": None,
            "undervalued_rating": None,
            "disqualified": True,
            "veto_reasons": ["Not enough data to judge."],
            "rerating_upside_pct": None,
            "notes": ["Not enough fundamental data to score this as a value candidate."],
        }

    total_w = sum(w for w, _ in parts)
    score = round(sum(w * s for w, s in parts) / total_w, 1)

    valuation = estimate_fair_value(pe, growth, margin, upside_analyst, price=result.get("price"))
    upside = valuation["upside_pct"]
    if valuation["note"]:
        notes.append(valuation["note"])

    # A name can score respectably on quality and quiet-price alone while its
    # own arithmetic says there is no discount left. That is a fine company,
    # not an undervalued one, and this screen is only about the second thing.
    if upside is not None and upside < MIN_RERATING_UPSIDE:
        vetoes.append(("no_discount",
                       f"Its own numbers imply only {upside:+.0f}% from a re-rating — priced "
                       f"about right, so there's no discount here to collect."))

    if score >= 75:
        rating = "Deep value + quality"
    elif score >= 62:
        rating = "Undervalued, worth the work"
    elif score >= 50:
        rating = "Mildly cheap"
    else:
        rating = "Not compelling"

    return {
        "undervalued_score": score,
        "undervalued_rating": rating,
        "disqualified": bool(vetoes),
        "veto_reasons": [msg for _code, msg in vetoes],
        "veto_codes": [code for code, _msg in vetoes],
        "rerating_upside_pct": upside,
        "current_price": valuation["current_price"],
        "fair_value_price": valuation["fair_value_price"],
        "current_pe": valuation["current_pe"],
        "fair_pe": valuation["fair_pe"],
        "notes": notes,
    }


def rank_value_picks(results, limit=6):
    """Survivors only, best first. Disqualified names are dropped outright --
    they are reported separately (with their reason) rather than ranked, so
    the list the user reads is only names that passed every veto."""
    alive = [r for r in results
             if (r.get("undervalued") or {}).get("undervalued_score") is not None
             and not (r.get("undervalued") or {}).get("disqualified")
             and r["undervalued"]["undervalued_score"] >= MIN_SCORE_TO_SHOW]
    alive.sort(key=lambda r: r["undervalued"]["undervalued_score"], reverse=True)
    return alive[:limit]
