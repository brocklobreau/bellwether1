# Stock signal pipeline — runbook

Follow this exactly on every run (scheduled or manual). Data comes from the
Financial Modeling Prep (FMP) API (paid key on file — see step 1), fetched
via WebFetch since this sandbox can't make raw HTTP calls from code. The
math is pure Python in `lib/`. Project root: `/root/stock-algo`.

## 0. Setup
- `cd /root/stock-algo`
- **Heartbeat (added 2026-08-24):** immediately write/overwrite
  `results/heartbeat.json` with `{"status": "started", "started_at": <UTC ISO
  now>}`. This is the FIRST thing every run does, before any WebFetch calls.
  Reason: on 2026-08-21 through 2026-08-24, the scheduled "Bellwether hourly
  refresh" task fired on schedule every weekday market hour (confirmed via
  `list_triggers` — `last_fired_at` kept advancing) but produced zero file
  changes for the entire weekend — no updated `results/latest.json`, no new
  `results/history/*` snapshot, no dashboard republish. Because each
  scheduled firing runs as its own separate, unobservable session, there was
  no way to tell from outside whether the run never started, started and
  crashed immediately, or completed and then failed to write — this
  heartbeat closes that gap going forward. At the very end of step 5 (after
  the dashboard has been regenerated and republished), overwrite the same
  file with `{"status": "completed", "started_at": <same value>,
  "completed_at": <UTC ISO now>}`. If a run ever finds `heartbeat.json`
  already `status: "started"` from a previous run with no matching
  `completed_at` (i.e. the last run never finished), say so plainly in that
  run's final message — that's the signature of the kind of silent failure
  seen this week.
- Read `watchlist.json` for the ticker lists.
- Read `results/latest.json` if it exists (via `lib/save_results.load_previous()`) — this is "previous" for noteworthy-change detection. If it doesn't exist yet, this is the first run; skip change detection.
- Determine if this is the **first run of the trading day** (roughly the first run between 13:00-15:00 UTC on a weekday). If so, also run the screener pass (step 3) and the discovery pass (step 3b). Otherwise skip both to keep runs fast, unless the user explicitly asked for a fresh run of either.
- Read the API key: `open("config/fmp_api_key.txt").read().strip()`. Never print
  it in full in chat or write it into any other file — build request URLs
  with it directly. If any FMP call returns 401/403, the key may have
  expired or hit a plan/rate limit — tell the user plainly what failed
  rather than silently falling back to guessed data.
- `portfolio.json` (repo root) is the user's chat-reported trade log —
  read/write it with `lib/portfolio.load_portfolio()` / `save_portfolio()`.
  If the user tells you in chat that they bought or sold something ("I
  bought 10 AAPL at $228 on 8/15"), add a trade dict (`{id, ticker, side:
  "buy"|"sell", shares, price, date: "YYYY-MM-DD", note}`) and save it. This
  is embedded into the page every regeneration and shows up on every device.
  There's also an "Add trade" form directly on the dashboard's Portfolio tab
  — trades added there are saved in the viewer's OWN browser via
  localStorage, which survives every hourly republish untouched (republishing
  only rewrites the page's markup, never a viewer's local storage) but is
  per-browser/per-device, not synced anywhere else. The two logs are simply
  combined on the page at render/load time — don't try to merge them into
  one file; if the user reports the same trade both ways it'll double-count,
  so mention that if it comes up.

## 1. Per-ticker data collection (watchlist tickers, every run)

All calls are `WebFetch` against FMP's `stable` API (the legacy `/api/v3/`
paths are deprecated and return 403 — always use `/stable/`). Ask WebFetch
to "return the raw JSON verbatim" in the prompt every time; these are clean
API responses, not pages to summarize. For each ticker in `watchlist.json` →
`watchlist`:

**a) Quote** (price, market cap, 52-week range, 50/200-day averages, today's volume) —
`https://financialmodelingprep.com/stable/quote?symbol=<TICKER>&apikey=<KEY>`
→ fields: `price`, `marketCap`, `yearHigh`, `yearLow`, `priceAvg50`, `priceAvg200`, `volume`.
This plan's `/stable/quote` does NOT return an `averageVolume` field (confirmed
2026-08-21 — it's simply absent from the response, not a WebFetch summarization
gap) — get the trailing average from step f's price history instead (see the
note there). Don't try comma-separating multiple symbols in one call either;
`quote?symbol=A,B,C` silently returns `[]` on this plan — one ticker per call.

**b) Ratios** (valuation, margins, dividend, leverage, cash flow) —
`https://financialmodelingprep.com/stable/ratios?symbol=<TICKER>&apikey=<KEY>`
→ take the most recent (first) entry: `priceToEarningsRatio` (pe_ratio),
`netProfitMargin` (×100 for percent), `dividendPerShare` (divide by `price`
from step a, ×100, for `dividend_yield_pct`). This same response also carries
`debtToEquityRatio` (→ `debt_to_equity` directly) and
`freeCashFlowPerShare`/`revenuePerShare` (→ `fcf_margin_pct` =
`freeCashFlowPerShare / revenuePerShare * 100`) — added 2026-08-21 to feed
`lib/fundamentals.py`'s balance-sheet and cash-flow scoring components. No
separate call needed; this one response covers both the original fields and
these two.

**c) Growth** —
`https://financialmodelingprep.com/stable/financial-growth?symbol=<TICKER>&apikey=<KEY>`
→ most recent entry's `revenueGrowth` (×100 for percent → `revenue_growth_pct`).

**d) Analyst price target** —
`https://financialmodelingprep.com/stable/price-target-consensus?symbol=<TICKER>&apikey=<KEY>`
→ `targetConsensus`. Compute `analyst_upside_pct = (targetConsensus - price) / price * 100`.

**e) Analyst rating consensus** —
`https://financialmodelingprep.com/stable/grades-consensus?symbol=<TICKER>&apikey=<KEY>`
→ `consensus` field (e.g. `"Buy"`, `"Strong Buy"`) → `analyst_rating`.

**f) Price history** (oldest → newest closes for the indicators) —
`https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=<TICKER>&from=<YYYY-MM-DD ~70 calendar days back>&to=<today>&apikey=<KEY>`
→ list of `{date, close, volume, ...}` objects, newest-first — reverse to
oldest→newest before passing to `score_ticker`. Need ~35+ trading days for
MACD; 70 calendar days comfortably covers that. This same response is also
the source for `avg_volume`: take the `volume` field from the ~20 trading
days *before* today (i.e. skip index 0, the newest/today's row, then average
the next 20) and pass that as `avg_volume` to `score_ticker` alongside
today's `volume` from step a. Don't reuse today's own volume in the average —
that would flatten relative-volume readings toward 1.0x on exactly the days
it matters most.

**g) News headlines** —
`https://financialmodelingprep.com/stable/news/stock?symbols=<TICKER>&apikey=<KEY>`
→ take the `title` field from the first 5-10 items as the headline list for
`lib/sentiment.py`.

**h) Company profile (sector)** —
`https://financialmodelingprep.com/stable/profile?symbol=<TICKER>&apikey=<KEY>`
→ `sector` field (e.g. `"Technology"`). Add it to `fund_input` as `sector`.
Feeds the Sector/concentration check on the Home page and Portfolio tab
(`lib/sectors.py`) — until this has been fetched for a ticker, that module
falls back to a small static sector map so the feature isn't empty, but the
live value always wins once it's there. Cheap to skip on screener/discovery
passes if you're moving fast — the fallback map covers the common names.

**i.5) Next earnings date** (added 2026-08-21, once per day) —
`https://financialmodelingprep.com/stable/earnings?symbol=<TICKER>&apikey=<KEY>`
→ the response is sorted newest-first by `date` and the *upcoming* report is
simply whichever entry has the largest `date` and `epsActual: null` — in
practice that's always index 0, since a future date sorts before all past
ones, but confirm `epsActual` is null before trusting it (a stale/uncached
response could in principle lead with a past report). Only worth fetching
on the first run of the day (same cadence as step 3b) — an earnings date
doesn't change hour to hour. Carry the previous run's `earnings_risk` value
forward for a ticker on non-first runs of the day, same pattern as
`discovered_candidates`. Feeds `next_earnings_date` into `score_ticker` (see
step 2) → `lib/earnings_risk.py`. Originally day-trade only; as of the
2026-08-21 investing round the resulting `earnings_risk` badge also renders
on the Investing tab (`lib/dashboard.py`'s `earnings_badge()` — no longer
gated to `score_key == "day_trade_score"`), since a long hold can gap on
earnings just as easily as a day trade can.

**i) Insider trading** —
`https://financialmodelingprep.com/stable/insider-trading/search?symbol=<TICKER>&limit=15&apikey=<KEY>`
→ pass the raw list of filing objects straight through as `insider_filings`
to `score_ticker` — `lib/insider_signal.py` does its own filtering (only
`P-Purchase` / `S-Sale` transaction types carry a signal; everything else in
the feed is option/RSU mechanics, not a market opinion). Note: FMP's
institutional-ownership (13F) endpoint returned 402 (payment required) on
this plan — it's not available, don't try to substitute something else for
it, just leave institutional data out.

Assemble into the same shapes `score_ticker` already expects:
```python
fund_input = {
    "pe_ratio": ..., "revenue_growth_pct": ..., "profit_margin_pct": ...,
    "dividend_yield_pct": ..., "analyst_rating": ..., "analyst_upside_pct": ...,
    "debt_to_equity": ...,   # debtToEquityRatio, from step b
    "fcf_margin_pct": ...,   # freeCashFlowPerShare / revenuePerShare * 100, from step b
    "market_cap_usd": ...,   # marketCap is already a plain dollar number from FMP, no parsing needed
    "sector": ...,           # from step h
}
price = ...            # from step a
low_52w, high_52w = ...  # yearLow, yearHigh from step a
closes = [...]          # oldest -> newest, from step f
headlines = [...]       # from step g
insider_filings = [...] # raw list from step i, unfiltered
```
Missing fields are fine — omit the key, the scoring functions handle partial
data. `forward_pe` isn't in this field set (FMP's estimate endpoints aren't
reliable on the Starter plan) — leave it out, it's optional in scoring.

Nine calls per ticker sounds like a lot, but they're small, fast, clean
JSON — a world away from the old 3-call HTML-scrape-and-summarize approach,
and 300 req/min means the full watchlist (a-i × ~15 tickers ≈ 135 calls)
finishes in well under a minute of rate-limit budget.

## 2. Score each ticker

```python
import sys; sys.path.insert(0, "/root/stock-algo")
from lib.pipeline import score_ticker
from lib.composite import detect_noteworthy

result = score_ticker(
    ticker, company_name, price, closes,          # closes = oldest -> newest
    fund_input, price_52w_low=low_52w, price_52w_high=high_52w,
    headlines=headlines, insider_filings=insider_filings,
    volume=volume, avg_volume=avg_volume,          # from steps a and f — feeds the day-trade score's relative-volume component
    next_earnings_date=next_earnings_date,         # from step 1i.5 — None is fine, just means no earnings-risk flag
)
```

`score_ticker` runs technicals, fundamentals, sentiment, insider activity
(`lib/insider_signal.py` — landing in `result["insider"]`, folded into the
composite score at a modest 15% weight per `lib/composite.DEFAULT_WEIGHTS`),
the composite signal, the day-trade setup score (`lib/day_trade_score` — see
step 3b for the weighting), AND two separate entry/exit price-zone
calculations from `lib/price_levels`:
  - `compute_investing_levels` — long-horizon zone from moving averages, the
    52-week range, and the analyst price target. Lands in
    `result["price_levels"]` (used by the Overview tab) and
    `result["investing_levels"]` (same data, used by the Investing tab).
    Always long/buy-side — investing levels don't support shorts.
  - `compute_day_trade_levels` — short-horizon zone from the last 5-10
    sessions' swing highs/lows and the stock's own recent volatility, mirrored
    for long or short depending on `day_trade_setup["direction"]` (see step
    3b — `score_ticker` computes the day-trade setup first specifically so it
    can pass that direction in). Lands in `result["day_trade_levels"]` (used
    by the Day Trade tab), and also carries `direction` and
    `risk_reward_ratio` (reward-to-risk using the near-price edge of each
    zone; `None` if risk is zero).
Both return the same base shape: `entry_zone`/`exit_zone` (each a
`(low, high)` tuple), `stop`, and `notes`. Both also cap how far a support/
resistance candidate can sit from the current price before it's rejected as
stale rather than proposed as a real zone (±20% for `compute_day_trade_levels`,
±35% below price for `compute_investing_levels`'s entry side) — added
2026-08-21 after a real case where a stock's 5/10-session window straddled a
huge recent gap (a move that had already happened) and produced an entry
zone 55%+ below current price: technically "a recent low," but not a level
the trade could ever reach without a full round-trip. When a candidate gets
rejected this way, the zone falls back to being sized off volatility around
current price instead, with a note saying so. It ALSO runs `lib/checklist` (a
day-trade checklist and an investing checklist — plain pass/fail rules, not
another opaque score), landing in `result["day_trade_checklist"]` and
`result["investing_checklist"]`. Do not compute any of this separately —
the dashboard reads it all directly off `result`, including the top-level
convenience copies `result["day_trade_score"]`, `result["day_trade_rating"]`,
`result["day_trade_direction"]`, `result["day_trade_setup_notes"]`, and
`result["risk_reward_ratio"]`.

Compare against `previous["watchlist_results"]` (match by ticker) with
`detect_noteworthy(prev_result, result)` to get `(is_noteworthy, reasons)`.
Attach `"noteworthy": is_noteworthy, "noteworthy_reasons": reasons` to `result`.

## 3. Screener pass (only on the first run of the day, or if asked)

For each ticker in `watchlist.json` → `screener_universe`, repeat steps 1a-1g
(1g/news optional — skip it for screener tickers to save time and just use
technical + fundamental score if you're trying to move fast). Keep only
tickers with `composite_score >= 68` (i.e., a real BUY conviction, not
borderline) as "screener_picks" — these are the "some others if they're good"
candidates.

## 3b. Discover candidates beyond the watchlist (also only on the first run
of the day, or if asked) — this is what powers the Day Trade and Investing
tabs' "screened" picks, distinct from the fixed `screener_universe` above:

- **Day-trade candidates**: don't just chase today's biggest % movers — scan
  for genuinely good day-trade *setups*. Combine two sources:
  1. `https://financialmodelingprep.com/stable/company-screener?marketCapMoreThan=5000000000&volumeMoreThan=3000000&betaMoreThan=1.1&isActivelyTrading=true&exchange=NASDAQ,NYSE&limit=30&apikey=<KEY>`
     — a broad, *quality-filtered* universe (liquid, volatile, actively
     traded) that isn't dependent on what happened to move today. This is
     the main source; it consistently surfaces better candidates than the
     movers lists alone, which skew toward illiquid penny stocks.
  2. `https://financialmodelingprep.com/stable/most-actives?apikey=<KEY>`,
     `biggest-gainers`, and `biggest-losers` (a big drop is just as
     tradeable as a big pop) — still worth pulling, since a real mover with
     a genuine catalyst is a legitimate setup even if it wouldn't show up in
     the screener. Expect most of these three lists to be illiquid/penny
     names; only keep ones that also clear a reasonable market cap (skip
     ETFs/leveraged products like QQQ, TSLL, BITO).
  Pick ~8-12 candidates across both sources (excluding anything already on
  the watchlist), run each through steps 1a/1f/1g (quote, price history,
  news — skip ratios/growth/target/rating/sector/insider, they don't feed
  day-trade scoring) or the full 1a-1g if you're also curious about their
  fundamentals. For each, compute `lib.indicators.score_technical`,
  `lib.sentiment.score_sentiment`, and then
  `lib.day_trade_score.score_day_trade_setup(technical, sentiment,
  market_cap_usd, volume=volume, avg_volume=avg_volume)` — this returns a
  single 0-100 `day_trade_score` (not just a pass/fail checklist) weighting
  liquidity (10%), volatility (15%), RSI-based momentum (15%), MACD/momentum
  trend agreement (10%), a real news catalyst (10%), proximity to a 20-day
  breakout/breakdown level (10%), relative volume — today's volume vs.
  the trailing ~20-day average (15%; see step 1f for where `avg_volume`
  comes from) — and **extension** (15%, added 2026-08-21, user-reported):
  how much of the move has *already happened*, from `momentum_10d_pct`.
  A stock already up (or down) 30-60%+ over the last 10 sessions scores
  LOW here even if every other component loves it — the point of this tool
  is to find setups before they move, not to chase a move that already
  finished. Without this, a name that had already spiked 100%+ days earlier
  could still rank as a "Prime setup" purely off residual volatility/RSI/
  volume, which is backwards. See `lib.day_trade_score._extension_score`.
  It also returns `direction` ("long" or "short"),
  from `lib.day_trade_score.determine_direction` — MACD histogram and 10-day
  momentum agreeing is the primary signal, RSI vs. 50 is the fallback. Pass
  that `direction` straight into `compute_day_trade_levels` (step 2 already
  does this) so short setups get mirrored levels — short entry near
  resistance, cover/target near support, stop above entry — instead of
  long-only levels pasted onto a bearish setup. This system has no
  execution capability either way; "short" here just means the levels
  describe a bearish trade idea (borrow-and-sell-first), not that anything
  gets shorted automatically.

  **Long-only filter (user preference, set 2026-08-21):** the user does not
  want short-side day-trade ideas surfaced. After scoring, drop every
  candidate whose `direction` is `"short"` before ranking — still compute
  and log their scores (so scanned-vs-dropped reporting stays honest about
  why they were cut), just exclude them from the kept list. Rank the
  remaining long-direction candidates by `day_trade_score` descending and
  keep the top 8. If the initial 8-12 scanned don't yield 8 longs (this mix
  skews short some days), pull more candidates before giving up — a wider
  screener page or the `biggest-gainers` list specifically tend toward
  long-direction setups, since a stock already up on the day is more likely
  to score long. If it still comes up short of 8 genuine long setups, say so
  plainly in the report rather than padding the list with weak candidates.
  Log what was scanned vs. dropped (including why — short-direction vs.
  just a low score) so it's clear this isn't silently truncating good
  setups. This is still
  `lib.checklist.day_trade_checklist`'s job to also compute (dashboard shows
  both the score and the plain-language pass/fail chips), just don't use
  checklist pass-count as the ranking signal anymore — use `day_trade_score`.
  This produces trade *ideas* (ticker, direction, entry/target/stop zone,
  risk/reward, why) — it never places an order; the user places their own
  trades based on what it shows.
- **Investing candidates**: WebSearch something like `"best growth stocks
  to buy now"` or `"top rated stocks by analysts"`, or just pull a few names
  from `watchlist.json` → `screener_universe` that haven't been scored
  recently. Pick 4-6 with real substance (not penny stocks).
- Run every discovered ticker through `score_ticker` exactly like a watchlist
  ticker (steps 1-2), then tag the result before saving:
  `result["source"] = "screened"` and `result["source_label"] = "Today's
  mover"` (day-trade finds) or `"Growth screen"` (investing finds) — the
  dashboard displays this tag under the ticker so it's clear these aren't
  the core watchlist. Collect them into a list and save as
  `payload["discovered_candidates"]`.
- Don't just chase whatever passes — score honestly. A big mover might still
  fail most day-trade criteria (e.g. no real liquidity), and that's fine to
  show; the point is a real screen, not a curated highlight reel.

## 4. Save + notify

```python
from lib.save_results import save_run
payload = {
    "watchlist_results": [...],       # list of result dicts from step 2
    "screener_picks": [...],          # only present on days the screener ran
    "discovered_candidates": [...],   # only present on days step 3b ran (else carry forward the previous run's list so the tabs don't go empty)
}
save_run(payload)
```

If step 3b didn't run this time (not the first run of the day), carry forward
`previous.get("discovered_candidates", [])` into the new payload rather than
dropping it — otherwise the Day Trade / Investing tabs lose their screened
picks every non-first run of the day.

Collect every ticker where `noteworthy=True`. If any exist, generate the
dashboard (step 5) and end your final message with a clear, short summary of
what changed and why — this is what triggers the push notification, so lead
with the ticker and the reason (e.g. "NVDA flipped to SELL — RSI 78,
overbought, and a guidance-cut headline just hit."). If nothing is
noteworthy, still refresh the dashboard silently but keep the final message
to one plain line like "Hourly check complete — no notable changes." so it
doesn't read as a false alarm.

## 5. Regenerate the dashboard

Run `lib/dashboard.generate_html()` against `results/latest.json` (it also
reads `portfolio.json` itself for the Portfolio tab's server-rendered
snapshot) to produce the page, write it to `/root/stock-algo/site/dashboard.html`,
strip the wrapper tags into `site/dashboard_artifact.html` as usual, then
publish it with the Artifact tool using **the same file path and URL** (from
`results/artifact_url.txt`) so it updates in place. No special `capabilities`
flag is needed for the Portfolio tab's in-page form — it persists via the
viewer's own browser localStorage, not a platform capability.

After a successful publish, write the "completed" heartbeat described in
step 0.

## Notes / known limitations to keep in mind
- As of 2026-08-20, data comes from the Financial Modeling Prep API (real
  clean JSON, not scraped HTML), fetched via WebFetch since this sandbox
  can't make raw HTTP calls from code. Quotes are end-of-day/delayed, not
  tick-by-tick real-time — still "recent, not tick-accurate," just far more
  reliable and precise than the earlier stockanalysis.com-scraping approach.
  If FMP calls start failing (expired key, plan limit), say so plainly
  rather than silently reverting to guessed numbers.
- **Silent scheduled-run failure, 2026-08-21 to 2026-08-24 (fixed via
  heartbeat, see step 0):** the hourly scheduled task fired reliably on its
  cron schedule the entire weekend but never actually updated any file — the
  dashboard sat 3 days stale while a user was checking it. Root cause was
  never identified (each scheduled firing is its own opaque session with no
  shared logs back to this one), so treat "fires but doesn't write" as a
  failure mode that can recur, not something fully resolved — the heartbeat
  file is a detection mechanism, not a fix for whatever causes it.
- **WebFetch data reliability, discovered 2026-08-24:** two separate issues
  showed up refreshing watchlist/discovered-candidate prices mid-session,
  both against FMP's `/stable/` endpoints:
  1. `/stable/quote` sometimes serves a stale cached value (a prior day's
     close) even during live market hours, and adding a cache-busting query
     param (`&_cb=...`) only fixes it for some tickers, not others,
     inconsistently. When `/stable/quote`'s price looks suspiciously
     identical to what was already on file, don't trust it at face value.
  2. A single WebFetch asking for a large bulk extraction (e.g. "return the
     most recent 49 daily [close, volume] records as JSON") from
     `/stable/historical-price-eod/full` can come back with wrong/garbled
     values for some tickers — the underlying summarization step
     mis-transcribes the raw data, and it isn't obvious from the output
     alone (it looks like plausible, well-formed JSON). Confirmed wrong this
     way for JNJ, AMZN, V, MU, and AVGO on first attempts.
  The reliable workaround for a single price point: a narrow, single-value
  prompt ("return only the single most recent record's date, close, and
  volume, exactly as given") plus a cache-busting param, repeated 2-3 times
  to confirm the same value comes back each time. For a full historical
  array, sanity-check the newest row of the bulk response against a
  narrow-prompt fetch of just that row before trusting the rest of the
  array; if the newest row doesn't independently check out, discard the
  whole bulk response and re-fetch (a different cache-bust value sometimes
  self-corrects it, as it did for AVGO/MU on retry). Also worth knowing: for
  some tickers FMP genuinely hasn't posted a new day's close yet even hours
  into a live session (confirmed by both `/quote` and
  `/historical-price-eod/full` agreeing on the same stale date) — that's a
  real data-provider lag, not a fetch error, and the honest move is to use
  the latest confirmed close rather than inventing a fresher one.
- This produces research signals, not financial advice. Never state a signal
  with unwarranted certainty in the dashboard or notifications.
- Entry/exit price zones are read off real support/resistance, moving
  averages, recent swing points, and the analyst target — they are not price
  predictions. Present them as zones/ranges, never as a single guaranteed
  number. The day-trade zone is built from DAILY closes (5-10 session swing
  points), not live intraday ticks — call it a short-horizon swing level,
  not a scalping level, if it comes up.
- The insider signal only covers open-market Form 4 activity (purchases and
  sales); institutional/13F ownership data is not available on this FMP plan
  (402 on that endpoint). Say so if asked why "institutional buying" isn't
  covered.
- The Day Trade tab's screened picks come from `lib.day_trade_score`
  (added 2026-08-21) — a weighted 0-100 algorithmic score (liquidity,
  volatility, momentum, trend agreement, news catalyst, breakout proximity,
  relative volume), ranked highest-first, sourced from FMP's company-screener
  (liquid/volatile universe) plus today's real movers, not just "biggest %
  move today." It is research/signal generation only — it identifies and
  ranks setups, it does not place trades. The user places their own trades
  based on what it shows.
- Day-trade setups are directional (added 2026-08-21): `day_trade_direction`
  is "long" or "short", from `lib.day_trade_score.determine_direction`
  (short-term momentum — MACD histogram + 10-day momentum, RSI as fallback).
  `lib.price_levels.compute_day_trade_levels` mirrors its zone construction
  for shorts (entry near resistance, target near support, stop above entry).
  Nothing in this system places trades either direction — "short" only means
  the entry/target/stop describes a bearish trade idea for the user to place
  themselves, same as "long" means a bullish one.
- Every day-trade setup also carries `risk_reward_ratio` (added 2026-08-21) —
  reward-to-risk to the near-price edge of the entry and target/exit zones,
  from `compute_day_trade_levels`. Shown on the dashboard as an "X.X:1" line
  under the entry/exit zones, colored good/borderline/poor. `None` when the
  stop and entry zone edge coincide (zero risk to divide by) — render that as
  a dash, don't treat it as zero.
- Relative volume (`rvol` = today's volume ÷ trailing ~20-day average volume,
  added 2026-08-21) is the day-trade score's single highest-weighted
  component (20%) — this FMP plan's `/stable/quote` doesn't return an
  `averageVolume` field, so `avg_volume` has to come from averaging the
  `volume` field across the last ~20 rows of the price-history fetch (step
  1f) instead of a dedicated endpoint. If a ticker is missing `avg_volume`
  (e.g. a fast/partial scan that skipped step 1f), the relative-volume
  component just drops out of the weighted average rather than scoring zero
  — don't treat a missing rvol note as a red flag, it means data wasn't
  fetched, not that volume was low.
- The Day Trade tab shows a per-ticker momentum block (`lib.day_trade_momentum`,
  added 2026-08-21) — a delta vs. the prior trading day's last snapshot
  (price % and score point change) plus a small intraday sparkline built from
  today's hourly snapshots. Built entirely from `results/history/`, same as
  the track records — nothing extra to fetch, just keep calling `save_run()`.
  It reads whichever two calendar dates (UTC) are most recent in history, so
  it naturally has nothing to show for "yesterday" until there's a full prior
  trading day of snapshots, and nothing for "today's trend" until at least 2
  snapshots exist for the current day — both render an honest muted
  placeholder ("No prior-day data yet" / "First snapshot of the day") in that
  case rather than a fake zero. Don't be alarmed if this looks empty for the
  first day or two after a fresh `results/history/` (e.g. right after the
  pre-launch archive move noted below) — it fills in on its own.
- Every day-trade pick now carries a suggested position size
  (`lib.position_sizing`, added 2026-08-21) — how many shares would risk
  exactly `risk_per_trade_pct` of `account_size` (both in
  `config/risk_settings.json`, currently a $100,000 paper-trading account at
  1% risk per trade — set to 5% initially, changed to 1% at the user's
  request the same day, which also matches the standard professional
  guideline) if the stop is hit, capped so it never implies buying more than
  the account can actually afford (a cash/no-margin assumption — see the
  "cash_capped" note when that cap, not the risk budget, is what limited the
  size). This is a sizing SUGGESTION for the user's own paper trades, not an
  order — nothing here executes anything. If the user ever asks for a
  different risk tolerance or account size, just edit
  `config/risk_settings.json` and re-run — no code change needed, and it's
  worth recomputing `position_size` on the current `results/latest.json` in
  place (reusing the `entry_ref` already stored per ticker) rather than
  waiting for the next scheduled run, same as when this changed from 5% to
  1%.
- Day-trade picks also carry an earnings-date risk flag (`lib.earnings_risk`,
  added 2026-08-21, fed by step 1i.5) — "soon" (earnings within 5 days) or
  "imminent" (today/tomorrow). This exists because a stock can be a clean
  technical setup and still gap double digits on an unrelated earnings
  surprise; flagging it lets a backtest tell "the system's read was wrong"
  apart from "an earnings gap blew through the stop for reasons no technical
  score could see coming" — those are different failure modes and
  shouldn't be graded the same way.
- The Day Trade tab has its own track record subsection
  (`lib.day_trade_track_record`, added 2026-08-21), separate from the
  watchlist-only, signal-change-based Track Record tab
  (`lib/track_record.py`). It grades "Prime setup" calls (`day_trade_score`
  crossing up through 70) against what price actually did ~1 day and ~3 days
  later — the horizon a day trade is meant to resolve in, not however long a
  composite signal happens to stay changed. It draws on the watchlist AND
  discovered/screened day-trade candidates (unlike the main track record,
  which is watchlist-only), since the discovered pool is where most real
  day-trade setups actually show up. Fed automatically from the same
  `results/history/` snapshots as the main track record — nothing extra to
  do here beyond continuing to call `save_run()` each run.
- The Sector/concentration check (`lib/sectors.py`) on the Home page (by
  count, across the tracked + screened pool) and the Portfolio tab (by
  dollar value of open positions) uses live FMP `sector` data when a ticker
  has been through step 1h, and a small static fallback map otherwise — so
  it's never empty, but accuracy improves as more tickers get a real
  profile pull. Don't hand-edit the fallback map for one-off corrections;
  fetch the real sector instead.
- The dashboard's Track Record tab (`lib/track_record.py`) is fully automatic
  — it's derived from `results/history/` snapshots (every `save_run()` call
  writes one), which now keeps ~720 hourly snapshots (`MAX_HISTORY` in
  `lib/save_results.py`) instead of 30. Nothing in this runbook needs to
  change to keep it fed; just keep calling `save_run()` as usual. Pre-launch
  testing history (from building this pipeline, not real trading days) was
  moved to `results/history_pretest_archive/` so the track record starts
  clean — don't move anything back into `results/history/` from there.
- The Portfolio tab's math is bookkeeping (FIFO cost basis), not tax advice
  — never present it as such. Trades entered in chat land in `portfolio.json`
  and show on every device; trades entered via the page's own form save to
  that one browser's localStorage and stay there indefinitely, but don't
  follow the user to another device — see the note in Step 0.
- `lib/fundamentals.py` scores two additional components (added 2026-08-21,
  both weight 0.10, both fed by step 1b — no new API calls): balance-sheet
  strength from `debt_to_equity` (tiered: <0.5 strong, 0.5-1 healthy, 1-2
  moderate, 2-4 elevated, 4+ heavy, negative flagged as negative-equity) and
  cash generation from `fcf_margin_pct` (negative = burning cash, 15%+ =
  "strong"). A company can carry a healthy net margin while over-levered or
  cash-burning despite it — margin alone missed that, which matters more
  over a months-long investing hold than a quick day trade. New weighted
  components dilute existing ones proportionally (`total_weight` in
  `score_fundamentals` sums only the weights of components actually present)
  — no manual rebalancing needed as fields get added over time.
- The Investing tab now carries a conviction-weighted position size
  (`lib.position_sizing.compute_investing_allocation`, added 2026-08-21) —
  deliberately a DIFFERENT sizing model than the day-trade one. A day-trade
  stop is tight and meant to be respected, so that one sizes off risk-to-stop.
  A long-hold stop is ~8% away and meant to rarely trigger, so sizing off
  risk-to-stop there would produce oversized positions; instead this scales
  target allocation linearly from `INVESTING_MIN_ALLOCATION_PCT` (3%, at
  `lib.composite.BUY_THRESHOLD` = 63) up to `INVESTING_MAX_ALLOCATION_PCT`
  (12%, at a composite score of 100) of `config/risk_settings.json`'s
  `account_size`. Returns `None` for anything not currently rated BUY — this
  is a sizing suggestion for a candidate worth buying, not a generic
  calculator. Same cash/no-margin, suggestion-not-an-order framing as the
  day-trade sizing feature.
- The Portfolio tab flags position drift (`lib.rebalance.flag_drift`, added
  2026-08-21) — a position built at a modest weight can grow past a sane
  single-name concentration purely from price appreciation, or from sizing
  that wasn't disciplined to begin with. This is a DIFFERENT risk than
  `lib.sectors`'s cross-position sector concentration check: that one is
  about correlated exposure across many tickers in the same sector, this one
  is about one ticker's weight regardless of sector. Pure math over
  `lib.portfolio.compute_portfolio()`'s own output (tracked open positions'
  dollar value ÷ tracked market value) — no network, no new data. `watch` at
  15%+ of tracked portfolio value, `trim` at 20%+ (`WATCH_POSITION_PCT` /
  `MAX_POSITION_PCT` in `lib/rebalance.py`). A flag to consider, not an order
  to trim — nothing here executes anything. Renders as a badge on the
  position row on both the server-rendered table and the client-side
  JS re-render that runs when the user adds a trade via the page's own form
  (kept in sync by hand — if `MAX_POSITION_PCT`/`WATCH_POSITION_PCT` ever
  change in `lib/rebalance.py`, update the matching literals in the JS
  `driftBadge` function in `lib/dashboard.py` too). Nothing to show until
  `portfolio.json` has real open positions — as of 2026-08-21 it's empty, so
  this is wired in and tested against synthetic data but hasn't rendered a
  live flag yet.
