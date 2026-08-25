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


MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)   # 9:30 AM ET
MARKET_CLOSE = (16, 0)  # 4:00 PM ET
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


def fetch_and_score(symbol, name_hint=None, full=True, source=None, source_label=None):
    """full=True pulls ratios/growth/target/grades/profile/earnings/insider
    in addition to quote+history+news; full=False (used for day-trade-only
    discovery candidates) skips the fundamentals-only calls per RUNBOOK.md
    step 3b ("skip ratios/growth/target/rating/sector/insider, they don't
    feed day-trade scoring") to keep the scan fast."""
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

    news = fmp.news_stock(symbol, limit=8)
    headlines = [n.get("title") for n in news if n.get("title")]

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
        return 0

    started_at = write_heartbeat("started")
    market_note = ""
    try:
        with open(WATCHLIST_PATH) as f:
            wl = json.load(f)
        watchlist = wl.get("watchlist", [])
        screener_universe = wl.get("screener_universe", [])

        previous = load_previous() or {}
        prev_watchlist_by_ticker = {r["ticker"]: r for r in previous.get("watchlist_results", [])}

        # --- 1. Watchlist (full fetch, every run) ---
        watchlist_results = []
        noteworthy = []
        for symbol in watchlist:
            log(f"watchlist: {symbol}")
            try:
                res = fetch_and_score(symbol, full=True)
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

        # --- 2. Screener universe (full fetch) -> screener_picks + investing discovery pool ---
        screener_results = []
        for symbol in screener_universe:
            log(f"screener: {symbol}")
            try:
                res = fetch_and_score(symbol, full=True)
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

        # --- 3. Day-trade discovery: company screener + movers lists ---
        candidates = {}
        try:
            for row in fmp.company_screener(
                marketCapMoreThan=5_000_000_000, volumeMoreThan=3_000_000,
                betaMoreThan=1.1, isActivelyTrading="true",
                exchange="NASDAQ,NYSE", limit=30,
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

        already_tracked = set(watchlist) | set(screener_universe)
        scan_pool = [s for s in candidates if s not in already_tracked]
        log(f"day-trade scan pool: {len(scan_pool)} candidates")

        day_trade_scored = []
        dropped_short = []
        scan_failures = []
        for symbol in scan_pool[:30]:  # hard cap so one run can't run away
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
            f"Day-trade scan: {len(scan_pool[:30])} candidates checked, "
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
            "pending_tickers": [],
            "market_note": market_note,
            "day_trade_scan_note": scan_note,
        }
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
