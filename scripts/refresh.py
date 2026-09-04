#!/usr/bin/env python3
"""
Unattended refresh entrypoint. Run by .github/workflows/refresh.yml on a
real cron schedule (GitHub Actions' own scheduler -- not a Claude session,
so it can't silently stop running the way the old scheduled-task approach
did; see RUNBOOK.md).

Fetches fresh data straight from FMP's REST API (scripts/fmp_client.py --
real HTTP, no WebFetch/LLM summarization step to garble numbers), runs it
through the same scoring pipeline (lib/) used interactively, saves results,
and regenerates the static dashboard page.

Usage: python3 scripts/refresh.py
Requires: FMP_API_KEY environment variable (see scripts/fmp_client.py).
"""
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from scripts import fmp_client as fmp
from lib.pipeline import score_ticker
from lib.composite import detect_noteworthy
from lib.save_results import save_run, load_previous
from lib.dashboard import generate_html
from lib.sentiment import score_headline
from lib.undervalued import prescore_value_candidate, score_undervalued, rank_value_picks
from lib import bot as trading_bot

NEWS_FEED_MAX = 15  # rolling per-ticker headline history kept across cycles


def tag_headlines(news_items):
    """Score each raw FMP news item individually (lib.sentiment.score_headline
    is the same per-headline scorer score_sentiment already uses internally)
    and keep the metadata the aggregate sentiment score normally throws away
    -- date/site/url -- so the dashboard can show an actual per-ticker news
    feed instead of just a single rolled-up score (added 2026-08-25,
    user-requested)."""
    tagged = []
    for n in news_items or []:
        title = n.get("title")
        if not title:
            continue
        s, mag = score_headline(title)
        tone = "positive" if s > 0.15 else "negative" if s < -0.15 else "neutral"
        tagged.append({
            "title": title,
            "date": n.get("publishedDate"),
            "site": n.get("site"),
            "url": n.get("url"),
            "tone": tone,
            "magnitude": bool(mag),
        })
    return tagged


def merge_headline_feed(previous_feed, fresh_tagged, max_items=NEWS_FEED_MAX):
    """Union this cycle's tagged headlines with whatever was already on file,
    deduped by title, newest first. This is what makes the news section
    'keep showing' a real catalyst for a few days instead of it vanishing
    the moment a later cycle's raw FMP fetch happens to come back thin."""
    seen = set()
    merged = []
    for h in list(fresh_tagged or []) + list(previous_feed or []):
        title = h.get("title")
        if not title or title in seen:
            continue
        seen.add(title)
        merged.append(h)
    merged.sort(key=lambda h: h.get("date") or "", reverse=True)
    return merged[:max_items]

WATCHLIST_PATH = os.path.join(BASE, "watchlist.json")
HEARTBEAT_PATH = os.path.join(BASE, "results", "heartbeat.json")
SITE_DIR = os.path.join(BASE, "site")

HISTORY_DAYS_BACK = 90  # calendar days -- comfortably covers the ~35+ trading
                         # days MACD needs plus the trailing-20-day avg volume window

# Leveraged/inverse ETFs and similar products that pass volume/market-cap
# filters but aren't real day-trade "setups" in the sense this tool means --
# skipped by symbol as a fast pre-filter; profile.isEtf/isFund below is the
# authoritative check for anything that slips past this list.
KNOWN_ETF_DENYLIST = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TSLL", "TSLQ", "BITO", "SPXL", "SPXS",
    "UVXY", "SVXY", "VXX", "UPRO", "SPXU", "TMF", "TMV", "YINN", "YANG",
    "LABU", "LABD", "FNGU", "FNGD", "NUGT", "DUST", "JNUG", "JDST",
}

# --- Screener redesign (2026-08-27, user-reported) ---------------------
# Both screeners used to be small, static, hand-picked ticker lists that
# leaned on the SAME composite scoring formula -- so "day trade" and
# "invest" picks kept converging on the same familiar large-cap names
# instead of actually screening for what makes a good short-term trade
# (volatile, liquid, room to move) vs. a good long-term hold (fundamentals,
# quality, valuation). Both now pull a genuinely large, dynamic candidate
# pool from FMP's company screener every cycle, cheaply pre-filter it down
# to a shortlist using only data the screener call already returned (no
# extra network calls), and only run the expensive full fundamentals+
# technicals+news fetch on that shortlist -- same shape as the existing
# day-trade discovery step already used, just applied to both now.
SCREENER_SHORTLIST_SIZE = 40  # user-chosen depth per screener per cycle
LOW_FLOAT_REFERENCE_SHARES = 50_000_000  # "low float" reference point for the day-trade ranking boost below


BACKTEST_DAYS = 730   # 2 years; also part of the backtest cache key below

FLOAT_PAGE_SIZE = 1000
# 25 pages (25k symbols) was NOT enough: the live range stopped at "PPZRX",
# so every candidate from Q to Z still had no float data. The listing is not
# just US common stock -- it carries funds, foreign lines and share classes
# (the first row is a Morningstar fund id), so the universe is far larger
# than the ~10k US tickers I sized this for. Raised, and paired with the
# early-exit below so a bigger cap costs nothing on a normal cycle.
FLOAT_MAX_PAGES = 60


def fetch_float_lookup(pages=FLOAT_MAX_PAGES, page_size=FLOAT_PAGE_SIZE, needed=None):
    """symbol -> float share count, from FMP's bulk shares-float-all endpoint
    (page_size symbols/page) rather than one network call per candidate.

    Pages until a short page says the listing is exhausted, capped at `pages`.
    That cap matters: the endpoint returns symbols in ALPHABETICAL order, so a
    partial pull isn't a random sample of the market -- it's the front of the
    alphabet and nothing else.

    That was the actual bug behind "0 with known float" (2026-08-27/28). This
    fetched only 3 pages = 3000 symbols, which alphabetically runs out around
    the B's, while day-trade candidates are names like WULF, ZETA, RIOT, NCLH
    -- all far past the cutoff, so essentially none of them could ever match.
    It looked like a symbol-format mismatch and wasn't; the formats were
    identical ('A', 'AA', 'AAL') and the coverage simply stopped early. The
    first live cycle after the diagnostic logging went in made it obvious:
    4/40 candidates matched, and the 4 were exactly the early-alphabet ones.
    Cost of the fix is ~10 extra calls per cycle against a ~1000-call budget.

    Missing/failed pages degrade gracefully -- fewer symbols get the low-float
    ranking boost, which is a worse ranking, not a broken run."""
    want = set(needed) if needed else None
    lookup = {}
    logged_sample = False
    pages_fetched = 0
    stopped_early = False
    for page in range(pages):
        try:
            rows = fmp.shares_float_all(page=page, limit=page_size)
        except fmp.FMPError as e:
            log(f"shares-float-all page {page} failed: {e}")
            break
        if not rows:
            break
        pages_fetched += 1
        for row in rows:
            sym = row.get("symbol")
            fs = (
                row.get("floatShares") or row.get("freeFloat")
                or row.get("floatingShares") or row.get("shares_float")
                or row.get("outstandingShares")
            )
            if sym and fs:
                lookup[sym] = fs
            elif not logged_sample:
                log(f"shares-float-all: unrecognized row shape, sample keys/values: {row}")
                logged_sample = True
        # Stop as soon as every candidate we actually care about has been
        # found. Most cycles this ends the scan long before the page cap,
        # so raising that cap costs nothing except on the rare cycle whose
        # candidates run to the end of the alphabet.
        if want and want.issubset(lookup.keys()):
            stopped_early = True
            break
        # A short page means that was the last one -- stop rather than burning
        # the remaining page budget on empty responses.
        if len(rows) < page_size:
            break
    if lookup:
        covered = f", {len(want & lookup.keys())}/{len(want)} candidates covered" if want else ""
        log(f"shares-float-all: {pages_fetched} pages fetched, {len(lookup)} symbols with float "
            f"(alphabetical range {min(lookup)}..{max(lookup)}){covered}"
            f"{' — stopped early, all candidates found' if stopped_early else ''}")
        if want and not want.issubset(lookup.keys()):
            missing = sorted(want - lookup.keys())
            log(f"shares-float-all: {len(missing)} candidate(s) still without float data "
                f"(page cap reached before their part of the alphabet): {missing[:10]}")
    else:
        log("shares-float-all: no float data resolved -- low-float boost inactive this cycle")
    return lookup


def rank_day_trade_candidates(candidates, float_lookup, limit=SCREENER_SHORTLIST_SIZE):
    """Cheap pre-filter using only fields already on hand (beta, volume from
    the screener/movers rows; float from fetch_float_lookup) -- favors
    volatile, liquid, LOW-float names, which is what actually makes a stock
    tradeable intraday. This replaces the old marketCapMoreThan=5B filter,
    which was screening FOR mega-caps -- the opposite of a day-trade
    screen's job, and the direct cause of it overlapping with the investing
    side."""
    scored = []
    for sym, row in candidates.items():
        beta = row.get("beta") or 1.0
        volume = row.get("volume") or 0
        float_shares = float_lookup.get(sym)
        float_boost = min(LOW_FLOAT_REFERENCE_SHARES / float_shares, 5.0) if float_shares else 1.0
        rank = max(beta, 0.1) * (volume / 1_000_000) * float_boost
        scored.append((rank, sym))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [sym for _, sym in scored[:limit]]


def pick_diverse_investing_candidates(rows, already_tracked, limit=SCREENER_SHORTLIST_SIZE):
    """Round-robins across sectors instead of taking the screener response's
    default order (roughly market-cap descending), which would just hand
    back the same handful of mega-caps every cycle -- the exact complaint
    that prompted this redesign. Each sector's own internal order is kept;
    sectors are just interleaved so the shortlist is genuinely diverse."""
    by_sector = {}
    for row in rows:
        sym = row.get("symbol")
        if not sym or sym in already_tracked:
            continue
        by_sector.setdefault(row.get("sector") or "Other", []).append(row)

    picked = []
    while len(picked) < limit and any(by_sector.values()):
        for bucket in by_sector.values():
            if bucket:
                picked.append(bucket.pop(0))
            if len(picked) >= limit:
                break
    return picked


# --- Deep-value ("hidden gems") screener sizing ------------------------
# Three stages instead of the other two screens' two, because "undervalued"
# can't be judged from the screener response at all -- that payload carries
# marketCap/beta/volume/sector but no valuation or profitability data. So a
# middle stage buys the missing facts with ONE /stable/ratios call per name
# (cheap) before committing the 10-call deep fetch to the few that survive.
# 140 x 1 + 24 x 10 = ~380 extra calls/cycle, comfortably inside the
# throttle in fmp_client.py at a 15-minute cadence.
VALUE_PRESCREEN_SIZE = 140   # names bought one ratios call each
VALUE_DEEP_SIZE = 24         # names promoted to the full fetch
VALUE_PICKS_SHOWN = 6        # survivors surfaced on the dashboard

# Deliberately excludes mega-caps. A $2T company is the most-analyzed asset
# on earth -- the odds it is quietly 50% mispriced are poor, and the whole
# premise here is finding what the market hasn't gotten to yet. The floor
# keeps out the sub-$300M tier where the "cheap" numbers are mostly noise
# and the spreads eat any edge.
VALUE_MIN_MARKET_CAP = 300_000_000
VALUE_MAX_MARKET_CAP = 50_000_000_000


def pick_value_prescreen_pool(rows, already_tracked, limit=VALUE_PRESCREEN_SIZE):
    """Sector round-robin over the raw screener rows, same reasoning as
    pick_diverse_investing_candidates: the screener returns roughly market-cap
    descending, so taking the head of the list would hand back the same large
    names every cycle and never reach the overlooked end of the market, which
    is the only place this screen is likely to find anything."""
    by_sector = {}
    for row in rows:
        sym = row.get("symbol")
        if not sym or sym in already_tracked:
            continue
        by_sector.setdefault(row.get("sector") or "Other", []).append(row)

    picked = []
    while len(picked) < limit and any(by_sector.values()):
        for bucket in by_sector.values():
            if bucket:
                picked.append(bucket.pop(0))
            if len(picked) >= limit:
                break
    return picked


MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)   # 9:30 AM ET
MARKET_CLOSE = (16, 0)  # 4:00 PM ET

# Mirrors app.py's scheduler interval. Duplicated rather than imported to
# avoid a circular import (app imports refresh); asserted below so the two
# can't silently drift apart.
REFRESH_INTERVAL_SECONDS = 15 * 60
SKIP_FLAG_PATH = os.path.join(BASE, "results", ".skip_this_run")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def within_market_hours(now_utc=None):
    """True if it's currently a weekday between 9:30 AM and 4:00 PM Eastern.
    Deliberately computed from the real America/New_York zone (not a fixed
    UTC offset) so this is correct across the DST transition automatically
    -- the old Claude-scheduled-task approach needed a manually-scheduled
    one-off reminder to hand-edit its cron expression every November/March;
    this doesn't. The GitHub Actions cron itself is intentionally wider than
    market hours (see .github/workflows/refresh.yml) specifically so this
    check is the actual source of truth for whether to do real work."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(MARKET_TZ)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_t = now_et.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_t = now_et.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_t <= now_et <= close_t


def write_heartbeat(status, started_at=None, error=None):
    os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
    payload = {
        "status": status,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    }
    if status == "completed":
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    if error:
        payload["error"] = str(error)[:2000]
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    return payload["started_at"]


def build_fund_input(symbol, q, r, growth, target, grades, prof):
    price = q.get("price")
    pe = r.get("priceToEarningsRatio")
    npm = r.get("netProfitMargin")
    div_per_share = r.get("dividendPerShare")
    dte = r.get("debtToEquityRatio")
    fcf_ps = r.get("freeCashFlowPerShare")
    rev_ps = r.get("revenuePerShare")
    rev_growth = growth.get("revenueGrowth")
    target_consensus = target.get("targetConsensus")
    analyst_upside_pct = None
    if target_consensus is not None and price:
        analyst_upside_pct = round((target_consensus - price) / price * 100, 2)
    return {
        "pe_ratio": pe,
        "revenue_growth_pct": round(rev_growth * 100, 2) if rev_growth is not None else None,
        "profit_margin_pct": round(npm * 100, 2) if npm is not None else None,
        "dividend_yield_pct": round(div_per_share / price * 100, 2) if (div_per_share and price) else None,
        "debt_to_equity": dte,
        "fcf_margin_pct": round(fcf_ps / rev_ps * 100, 2) if (fcf_ps and rev_ps) else None,
        "analyst_rating": grades.get("consensus"),
        "analyst_upside_pct": analyst_upside_pct,
        "market_cap_usd": q.get("marketCap") or prof.get("mktCap"),
        "sector": prof.get("sector"),
    }


def fetch_and_score(symbol, name_hint=None, full=True, source=None, source_label=None, prev_headline_feed=None):
    """full=True pulls ratios/growth/target/grades/profile/earnings/insider
    in addition to quote+history+news; full=False (used for day-trade-only
    discovery candidates) skips the fundamentals-only calls per RUNBOOK.md
    step 3b ("skip ratios/growth/target/rating/sector/insider, they don't
    feed day-trade scoring") to keep the scan fast.

    prev_headline_feed: the ticker's headline_feed from the previous cycle
    (watchlist tickers only -- see run()), so a real catalyst stays visible
    on the dashboard for a few days rather than disappearing the moment a
    later cycle's raw news fetch happens to come back thin."""
    q = fmp.quote(symbol)
    if not q or q.get("price") is None:
        raise fmp.FMPError(f"No usable quote for {symbol}")
    price = q["price"]

    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=HISTORY_DAYS_BACK)
    hist = fmp.historical_price_full(symbol, from_date.isoformat(), to_date.isoformat())
    if len(hist) < 15:
        raise fmp.FMPError(f"Not enough price history for {symbol} ({len(hist)} rows)")
    closes_oldest_first = [row["close"] for row in reversed(hist)]
    avg_volume = None
    if len(hist) > 21:
        avg_volume = sum(row.get("volume") or 0 for row in hist[1:21]) / 20

    news = fmp.news_stock(symbol, limit=12)
    headlines = [n.get("title") for n in news if n.get("title")]
    headline_feed = merge_headline_feed(prev_headline_feed, tag_headlines(news))

    r = growth = target = grades = prof = {}
    earnings_rows = []
    insider_filings = []
    if full:
        r = fmp.ratios(symbol)
        growth = fmp.financial_growth(symbol)
        target = fmp.price_target_consensus(symbol)
        grades = fmp.grades_consensus(symbol)
        prof = fmp.profile(symbol)
        earnings_rows = fmp.earnings(symbol)
        insider_filings = fmp.insider_trading_search(symbol, limit=15)

    fund_input = build_fund_input(symbol, q, r, growth, target, grades, prof) if full else {
        "market_cap_usd": q.get("marketCap"),
        "sector": prof.get("sector"),
    }

    next_earnings_date = None
    for row in earnings_rows:
        if row.get("epsActual") is None and row.get("date"):
            next_earnings_date = row["date"]
            break

    result = score_ticker(
        ticker=symbol,
        name=name_hint or prof.get("companyName") or symbol,
        price=price,
        closes_oldest_first=closes_oldest_first,
        fund_input=fund_input,
        price_52w_low=q.get("yearLow"),
        price_52w_high=q.get("yearHigh"),
        headlines=headlines,
        insider_filings=insider_filings,
        volume=q.get("volume"),
        avg_volume=avg_volume,
        next_earnings_date=next_earnings_date,
    )
    if source:
        result["source"] = source
    if source_label:
        result["source_label"] = source_label
    result.setdefault("sentiment", {})["headline_feed"] = headline_feed
    return result


def is_etf_or_fund(symbol, screener_row=None, prof=None):
    if symbol in KNOWN_ETF_DENYLIST:
        return True
    if screener_row and (screener_row.get("isEtf") or screener_row.get("isFund")):
        return True
    if prof and (prof.get("isEtf") or prof.get("isFund")):
        return True
    return False


def run():
    # Remove any stale skip-flag from a previous run before deciding this one.
    if os.path.exists(SKIP_FLAG_PATH):
        os.remove(SKIP_FLAG_PATH)

    if not within_market_hours():
        now_et = datetime.now(timezone.utc).astimezone(MARKET_TZ)
        log(f"Outside market hours ({now_et.strftime('%a %Y-%m-%d %H:%M %Z')}) -- skipping this run, no data touched.")
        os.makedirs(os.path.dirname(SKIP_FLAG_PATH), exist_ok=True)
        with open(SKIP_FLAG_PATH, "w") as f:
            f.write("skipped: outside market hours\n")

        # Still rebuild the page from the LAST saved results before returning.
        # No network, no new data -- purely re-running the template over
        # results/latest.json. Without this, the served HTML is only ever
        # regenerated at the end of a successful in-hours cycle, so any
        # dashboard change deployed outside market hours stays invisible
        # until the next open -- which is most of the time, and looks
        # exactly like a broken deploy from the outside (2026-08-28: the
        # new Hidden Gems tab shipped and didn't appear for precisely this
        # reason). Wrapped so a template bug can never turn a harmless skip
        # into a failed run.
        try:
            out_path = generate_html()
            os.makedirs(SITE_DIR, exist_ok=True)
            with open(out_path) as f:
                html = f.read()
            with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
                f.write(html)
            log("rebuilt dashboard HTML from last saved results (no new data fetched)")
        except Exception as e:
            log(f"dashboard rebuild during skipped run failed (non-fatal): {e}")
        return 0

    started_at = write_heartbeat("started")
    market_note = ""
    try:
        with open(WATCHLIST_PATH) as f:
            wl = json.load(f)
        watchlist = wl.get("watchlist", [])

        previous = load_previous() or {}
        prev_watchlist_by_ticker = {r["ticker"]: r for r in previous.get("watchlist_results", [])}

        # --- 1. Watchlist (full fetch, every run) ---
        watchlist_results = []
        noteworthy = []
        for symbol in watchlist:
            log(f"watchlist: {symbol}")
            try:
                prev_feed = (prev_watchlist_by_ticker.get(symbol) or {}).get("sentiment", {}).get("headline_feed")
                res = fetch_and_score(symbol, full=True, prev_headline_feed=prev_feed)
            except fmp.FMPError as e:
                log(f"  FAILED {symbol}: {e}")
                # Carry the previous result forward rather than dropping the
                # ticker from the dashboard entirely over one bad fetch.
                if symbol in prev_watchlist_by_ticker:
                    res = prev_watchlist_by_ticker[symbol]
                    res["fetch_error"] = str(e)
                else:
                    continue
            prev = prev_watchlist_by_ticker.get(symbol)
            is_nw, reasons = detect_noteworthy(prev, res) if prev else (False, [])
            res["noteworthy"] = is_nw
            res["noteworthy_reasons"] = reasons
            if is_nw:
                noteworthy.append((symbol, reasons))
            watchlist_results.append(res)

        # --- 2. Investing screener: broad dynamic pull -> screener_picks + investing discovery pool ---
        # Mid/large-cap, actively-traded, non-ETF/fund names -- a wide net on
        # purpose. isEtf/isFund are asked for at the API level AND checked
        # again locally (is_etf_or_fund) since the query-param filter isn't
        # fully trustworthy on its own.
        investing_rows = []
        try:
            investing_rows = fmp.company_screener(
                marketCapMoreThan=2_000_000_000, volumeMoreThan=200_000,
                isActivelyTrading="true", isEtf="false", isFund="false",
                exchange="NASDAQ,NYSE", limit=1000,
            )
        except fmp.FMPError as e:
            log(f"investing screener failed: {e}")

        investing_rows = [r for r in investing_rows if not is_etf_or_fund(r.get("symbol"), screener_row=r)]
        investing_shortlist = pick_diverse_investing_candidates(investing_rows, already_tracked=set(watchlist))
        log(f"investing screener: {len(investing_rows)} candidates pulled, {len(investing_shortlist)} shortlisted for scoring")

        screener_results = []
        for row in investing_shortlist:
            symbol = row.get("symbol")
            log(f"investing screen: {symbol}")
            try:
                res = fetch_and_score(symbol, name_hint=row.get("companyName"), full=True)
            except fmp.FMPError as e:
                log(f"  FAILED {symbol}: {e}")
                continue
            screener_results.append(res)

        screener_picks = [r for r in screener_results if (r.get("composite_score") or 0) >= 68]

        investing_pool = sorted(
            screener_results, key=lambda r: r.get("composite_score") or 0, reverse=True
        )[:4]
        for r in investing_pool:
            r["source"] = "screened"
            r["source_label"] = "Growth screen"

        # --- 2b. Deep-value "hidden gems" screener (3-stage) ---
        # Cheap + genuinely good + hasn't moved yet. See lib/undervalued.py
        # for what this can and can't claim -- in short, it enforces the
        # preconditions for an early entry, it does not predict one.
        value_rows = []
        try:
            value_rows = fmp.company_screener(
                marketCapMoreThan=VALUE_MIN_MARKET_CAP,
                marketCapLowerThan=VALUE_MAX_MARKET_CAP,
                priceMoreThan=3, volumeMoreThan=150_000,
                isActivelyTrading="true", isEtf="false", isFund="false",
                exchange="NASDAQ,NYSE", limit=1000,
            )
        except fmp.FMPError as e:
            log(f"value screener failed: {e}")

        value_rows = [r for r in value_rows if not is_etf_or_fund(r.get("symbol"), screener_row=r)]
        value_prescreen = pick_value_prescreen_pool(
            value_rows, already_tracked=set(watchlist) | {r["ticker"] for r in screener_results}
        )
        log(f"value screener: {len(value_rows)} candidates pulled, "
            f"{len(value_prescreen)} going to ratios prescreen")

        # Stage 2: one ratios call each, rank on cheapness x quality.
        prescored = []
        prescreen_rejects = {}
        for row in value_prescreen:
            symbol = row.get("symbol")
            try:
                ratios_row = fmp.ratios(symbol)
            except fmp.FMPError as e:
                prescreen_rejects[symbol] = f"ratios fetch failed: {e}"
                continue
            score, reject = prescore_value_candidate(ratios_row)
            if reject:
                prescreen_rejects[symbol] = reject
                continue
            prescored.append((score, symbol, row))

        prescored.sort(key=lambda x: x[0], reverse=True)
        value_deep = prescored[:VALUE_DEEP_SIZE]
        log(f"value prescreen: {len(prescored)} passed, {len(prescreen_rejects)} rejected "
            f"(cheap+quality gate), top {len(value_deep)} promoted to deep fetch")

        # Stage 3: full fetch + deep scoring + vetoes on the survivors only.
        value_results = []
        for score, symbol, row in value_deep:
            log(f"value screen: {symbol} (prescore {score})")
            try:
                res = fetch_and_score(symbol, name_hint=row.get("companyName"), full=True,
                                      source="screened", source_label="Deep value")
            except fmp.FMPError as e:
                log(f"  FAILED {symbol}: {e}")
                continue
            res["undervalued"] = score_undervalued(res)
            res["undervalued"]["prescore"] = score
            value_results.append(res)

        value_picks = rank_value_picks(value_results, limit=VALUE_PICKS_SHOWN)
        value_rejected = [
            {
                "ticker": r["ticker"],
                "name": r.get("name"),
                "score": (r.get("undervalued") or {}).get("undervalued_score"),
                "reasons": (r.get("undervalued") or {}).get("veto_reasons") or [],
            }
            for r in value_results
            if (r.get("undervalued") or {}).get("disqualified")
        ]
        value_note = (
            f"Deep-value scan: {len(value_rows)} screened, {len(value_prescreen)} prescreened, "
            f"{len(value_results)} deep-scored, {len(value_rejected)} disqualified, "
            f"{len(value_picks)} kept."
        )
        log(value_note)
        if not value_picks:
            log("  No name cleared every value gate this cycle -- showing none rather than "
                "relaxing the filters to fill the tab.")

        # --- 3. Day-trade discovery: broad dynamic pull (volatile/liquid/low-float) + movers lists ---
        candidates = {}
        try:
            for row in fmp.company_screener(
                marketCapMoreThan=50_000_000, marketCapLowerThan=10_000_000_000,
                priceMoreThan=2, volumeMoreThan=1_000_000, betaMoreThan=1.3,
                isActivelyTrading="true", isEtf="false", isFund="false",
                exchange="NASDAQ,NYSE,AMEX", limit=1000,
            ):
                sym = row.get("symbol")
                if sym and not is_etf_or_fund(sym, screener_row=row):
                    candidates[sym] = row
        except fmp.FMPError as e:
            log(f"company-screener failed: {e}")

        for fetcher, label in ((fmp.most_actives, "most-actives"),
                                (fmp.biggest_gainers, "gainers"),
                                (fmp.biggest_losers, "losers")):
            try:
                for row in fetcher():
                    sym = row.get("symbol")
                    if sym and not is_etf_or_fund(sym, screener_row=row):
                        candidates.setdefault(sym, row)
            except fmp.FMPError as e:
                log(f"{label} failed: {e}")

        already_tracked = set(watchlist) | {r["ticker"] for r in screener_results}
        raw_pool = {s: row for s, row in candidates.items() if s not in already_tracked}
        log(f"day-trade raw_pool sample symbols: {list(raw_pool.keys())[:10]}")
        float_lookup = fetch_float_lookup(needed=raw_pool.keys())
        scan_pool = rank_day_trade_candidates(raw_pool, float_lookup)
        log(f"day-trade scan pool: {len(raw_pool)} candidates pulled, {len(scan_pool)} shortlisted "
            f"({sum(1 for s in scan_pool if s in float_lookup)} with known float)")

        day_trade_scored = []
        dropped_short = []
        scan_failures = []
        for symbol in scan_pool:
            try:
                res = fetch_and_score(symbol, full=False, source="screened", source_label="Today's mover")
            except fmp.FMPError as e:
                scan_failures.append(symbol)
                continue
            if res.get("day_trade_direction") == "long":
                day_trade_scored.append(res)
            else:
                dropped_short.append(symbol)

        day_trade_scored.sort(key=lambda r: r.get("day_trade_score") or 0, reverse=True)
        day_trade_pool = day_trade_scored[:8]

        scan_note = (
            f"Day-trade scan: {len(scan_pool)} candidates checked, "
            f"{len(dropped_short)} dropped (short direction, long-only preference), "
            f"{len(scan_failures)} failed to fetch, {len(day_trade_pool)} kept."
        )
        log(scan_note)
        if len(day_trade_pool) < 8:
            market_note = (
                f"Only found {len(day_trade_pool)} genuine long day-trade setups this run "
                f"(fewer than the usual 8) -- shown honestly rather than padded with weak picks."
            )

        discovered_candidates = day_trade_pool + investing_pool

        payload = {
            "watchlist_results": watchlist_results,
            "screener_picks": screener_picks,
            "discovered_candidates": discovered_candidates,
            "value_picks": value_picks,
            "value_rejected": value_rejected,
            "value_scan_note": value_note,
            "pending_tickers": [],
            "market_note": market_note,
            "day_trade_scan_note": scan_note,
            # Countdown data for the dashboard. Approximate by nature: the
            # scheduler in app.py sleeps REFRESH_INTERVAL_SECONDS *after* a
            # cycle finishes, so the next start is roughly now + interval.
            # The page treats it as an estimate and recomputes from whatever
            # generated_at it actually receives, so drift self-corrects.
            "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
            "next_refresh_at": (datetime.now(timezone.utc)
                                + timedelta(seconds=REFRESH_INTERVAL_SECONDS)).isoformat(),
        }

        # --- Paper-trading bot ---
        # Runs on this cycle's freshly-scored data and persists its own
        # portfolio to results/bot_state.json. Wrapped so a bug in the bot
        # can never cost us the actual data refresh -- the dashboard's core
        # job is the scoring, and the bot is a passenger on that cycle.
        try:
            bot_state = trading_bot.run_cycle(payload)
            trading_bot.save_state(bot_state)
            payload["bot"] = trading_bot.public_snapshot(bot_state)
            bs = payload["bot"]["summary"]
            log(f"bot: equity ${bs['equity']:,.2f} ({bs['total_return_pct']:+.2f}%), "
                f"{bs['open_count']} open, {bs['closed_count']} closed, "
                f"cash ${bs['cash']:,.2f}")
            for a in bot_state.get("actions", [])[-6:]:
                if a.get("ts") == bot_state.get("last_run"):
                    log(f"  bot {a['kind'].upper()} {a['ticker']} x{a.get('shares')} "
                        f"@ ${a.get('price')} — {a.get('detail')}")
        except Exception as e:
            log(f"bot cycle failed (non-fatal, data refresh unaffected): {e}")
            traceback.print_exc()

        # --- One-off backtest of the risk engine ---
        # Runs once and caches to results/backtest.json (persistent disk), then
        # refreshes weekly. Guarded and non-fatal: it is a diagnostic, and must
        # never be able to cost us the live data refresh. Its history download
        # is shared across every sensitivity variant, so the whole thing costs
        # ~45 API calls, not ~300.
        try:
            from scripts import backtest as bt
            stale = True
            reason = "no cached result"
            if os.path.exists(bt.RESULT_PATH):
                age_days = (datetime.now(timezone.utc)
                            - datetime.fromtimestamp(os.path.getmtime(bt.RESULT_PATH),
                                                     tz=timezone.utc)).days
                stale = age_days >= 7
                reason = f"cached result is {age_days}d old"
                # Age alone is not enough: changing an exit rule makes a
                # cached result describe a strategy that no longer exists,
                # and comparing the new bot against the old backtest would
                # be quietly wrong. Re-run whenever the rules differ.
                if not stale:
                    try:
                        with open(bt.RESULT_PATH) as f:
                            cached_cfg = (json.load(f) or {}).get("config") or {}
                        live_cfg = {
                            "ratchet_steps": [list(x) for x in bt.RATCHET_STEPS],
                            "thesis_exit_max_gain_pct": bt.THESIS_EXIT_MAX_GAIN_PCT,
                            "stop_pct": bt.INVEST_STOP_PCT,
                            "target_pct": bt.INVEST_TARGET_PCT,
                            "cost_per_side_pct": bt.COST_PER_SIDE_PCT,
                            "risk_per_trade_pct": bt.RISK_PER_TRADE_PCT,
                            # Window length must be part of this too: changing
                            # 1yr -> 2yr changes what the result MEANS while
                            # leaving every exit rule identical, so without it
                            # the cache silently serves the old window (which
                            # is exactly what happened on 2026-09-02).
                            "days": BACKTEST_DAYS,
                            # Adding a sweep variant changes the result without
                            # touching any exit rule, so it must be part of the
                            # key too -- otherwise the new variants silently
                            # never run (same trap as `days` on 2026-09-02).
                            "sweep_variants": [list(v) for v in bt.SWEEP_VARIANTS],
                            # Result-shape version. Adding a field to the
                            # result does not change any exit rule, so without
                            # this the cache keeps serving a result that simply
                            # lacks the new field.
                            "result_schema": 8,
                            "vol_stop_mult": trading_bot.VOL_STOP_MULT,
                            "ratchet_fractions": [list(x) for x in trading_bot.RATCHET_FRACTIONS],
                        }
                        for k, v in live_cfg.items():
                            cv = cached_cfg.get(k)
                            if k == "ratchet_steps" and cv is not None:
                                cv = [list(x) for x in cv]
                            if cv != v:
                                stale = True
                                reason = f"exit rules changed ({k}: {cv} -> {v})"
                                break
                    except Exception:
                        stale, reason = True, "cached result unreadable"
            if stale:
                log(f"backtest: re-running -- {reason}")
                log("backtest: running 2-year risk-engine backtest (once weekly)...")
                res = bt.run_and_save(days=BACKTEST_DAYS)
                log(f"backtest: {res['total_return_pct']:+.2f}% vs buy-hold "
                    f"{res['benchmark_buy_hold_pct']:+.2f}% "
                    f"(excess {res['excess_vs_benchmark_pct']:+.2f}), "
                    f"{res['closed_trades']} trades, hit {res['hit_rate_pct']}%, "
                    f"R:R {res['realized_rr']}, maxDD {res['max_drawdown_pct']}%")
                dep = res.get("deployment") or {}
                if dep:
                    log(f"  deployment: avg {dep['avg_invested_pct']}% invested "
                        f"(median {dep['median_invested_pct']}%), "
                        f"avg {dep['avg_open_positions']}/{dep['max_positions_cap']} slots, "
                        f"at cap {dep['pct_sessions_at_cap']}% of sessions, "
                        f"under half {dep['pct_sessions_under_half']}%")
                    b = dep.get("blocked") or {}
                    log(f"  entry blockers: entered {b.get('entered')}, "
                        f"slots-full {b.get('slots_full')}, "
                        f"sector-cap {b.get('sector_cap')}, "
                        f"size-reject {b.get('size_reject')}, "
                        f"no-candidate sessions {b.get('no_candidate_day')}, "
                        f"avg candidates/session {dep.get('avg_candidates_per_session')}")
                    if dep.get("size_reject_reasons"):
                        log(f"  size-reject reasons: {dep['size_reject_reasons']}")
                gb = res.get("giveback") or {}
                if gb.get("trades_measured"):
                    log(f"  giveback: avg {gb['avg_giveback_pct']}%/trade "
                        f"(winners {gb['avg_giveback_winners_pct']}%, "
                        f"losers {gb['avg_giveback_losers_pct']}%), "
                        f"${gb['total_giveback_dollars']:,.0f} total")
                    for g in gb.get("green_then_red", []):
                        log(f"    green->red, was up >={g['threshold_pct']:.0f}%: "
                            f"{g['trades']} trades ({g['pct_of_all_losers']}% of losers), "
                            f"avg peak {g['avg_peak_pct']}%, ${g['giveback_dollars']:,.0f}")
                wf = res.get("walk_forward") or {}
                if wf.get("error"):
                    log(f"  walk-forward: FAILED -- {wf['error']}")
                elif wf:
                    log(f"  walk-forward: train {wf['train_window']['from']}..{wf['train_window']['to']}"
                        f" / test {wf['test_window']['from']}..{wf['test_window']['to']}")
                    log(f"    picked on train: {wf['picked_on_train']} "
                        f"({wf['picked_train_return_pct']:+.2f}% train) "
                        f"-> {wf['picked_test_return_pct']:+.2f}% on TEST, "
                        f"rank {wf['picked_rank_on_test']}/{wf['candidates']} out-of-sample")
                    log(f"    best on test was {wf['best_on_test']}; "
                        f"avg candidate {wf['avg_test_return_pct']:+.2f}%; "
                        f"edge from tuning {wf['edge_vs_random_pick_pct']:+.2f}%; "
                        f"rank correlation {wf['rank_correlation']}")
                    for e in wf.get("entry_experiments", []):
                        if "error" in e:
                            log(f"    entry {e.get('label')}: ERROR {e['error']}")
                            continue
                        log(f"    entry {e['label']:<26} train {str(e.get('train_return_pct')):>7} "
                            f"test {str(e.get('test_return_pct')):>7} "
                            f"pctile {str(e.get('test_percentile')):>6} "
                            f"trades {e.get('test_trades')} invested {e.get('test_invested_pct')}%")
                    for phase, rb in (wf.get("random_benchmark") or {}).items():
                        if rb.get("error"):
                            log(f"    random-{phase}: {rb['error']}")
                        elif rb.get("bot_percentile") is not None:
                            log(f"    random {phase}: bot {rb['bot_return_pct']:+.2f}% vs "
                                f"{rb['trials']} random {rb['names_per_portfolio']}-name portfolios "
                                f"(median {rb['median_pct']:+.2f}%, "
                                f"p25 {rb['p25_pct']:+.2f}%, p75 {rb['p75_pct']:+.2f}%) "
                                f"-> {rb['bot_percentile']}th percentile")
                    for r in wf.get("rows", []):
                        log(f"      {r['label']:<24} train {str(r.get('train_return_pct')):>7} "
                            f"test {str(r.get('test_return_pct')):>7}")
                for l in res.get("ratchet_sweep", []):
                    if "error" in l:
                        log(f"  ladder {l['label']}: ERROR {l['error']}")
                        continue
                    log(f"  ladder {l['label']}: {l['return_pct']:+.2f}%, "
                        f"maxDD {l['max_dd_pct']:.1f}%, {l['trades']} trades, "
                        f"hit {l['hit_rate_pct']}%, R:R {l['realized_rr']}, "
                        f"green->red@5 {l['green_then_red_5']}")
                for v in res.get("sensitivity", []):
                    if "error" not in v:
                        log(f"  backtest variant {v['stop_pct']}/{v['target_pct']} "
                            f"({v['rr']}:1): {v['return_pct']:+.2f}%, "
                            f"maxDD {v['max_dd_pct']:.1f}%, {v['trades']} trades, "
                            f"hit {v['hit_rate_pct']}%")
                        for y in v.get("yearly", []):
                            log(f"      {y['year']}: {y['return_pct']:+.2f}% "
                                f"({y['trades']} trades, hit {y['hit_rate_pct']}%)")
        except Exception as e:
            log(f"backtest failed (non-fatal): {e}")
            traceback.print_exc()

        try:
            if os.path.exists(os.path.join(BASE, "results", "backtest.json")):
                with open(os.path.join(BASE, "results", "backtest.json")) as f:
                    payload["backtest"] = json.load(f)
        except Exception as e:
            log(f"could not attach backtest results: {e}")

        save_run(payload)
        log("saved results/latest.json + history snapshot")

        out_path = generate_html()
        log(f"generated {out_path}")
        # Static hosts (Render Static Site, GitHub Pages) serve index.html at
        # the site root -- keep a copy under that name alongside the original.
        os.makedirs(SITE_DIR, exist_ok=True)
        with open(out_path) as f:
            html = f.read()
        with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
            f.write(html)

        if noteworthy:
            log("Noteworthy changes:")
            for sym, reasons in noteworthy:
                log(f"  {sym}: {reasons}")
        else:
            log("No noteworthy changes this run.")

        write_heartbeat("completed", started_at=started_at)
        return 0
    except Exception as e:
        log(f"RUN FAILED: {e}")
        traceback.print_exc()
        write_heartbeat("failed", started_at=started_at, error=e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
