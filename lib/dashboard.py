"""
Renders results/latest.json into the dashboard HTML page.
No network calls. Pure templating.
"""
import json
import os
from datetime import datetime, timezone

from lib.portfolio import load_portfolio, compute_portfolio
from lib.track_record import build_track_record
from lib.day_trade_track_record import build_day_trade_track_record
from lib.day_trade_momentum import build_momentum
from lib.sectors import get_sector, sector_breakdown
from lib.rebalance import flag_drift

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(BASE, "results", "latest.json")
OUT_PATH = os.path.join(BASE, "site", "dashboard.html")

STATUS = {
    "BUY":  {"color": "var(--good)",     "icon": "▲", "label": "BUY"},
    "HOLD": {"color": "var(--warning)",  "icon": "▬", "label": "HOLD"},
    "SELL": {"color": "var(--critical)", "icon": "▼", "label": "SELL"},
    "NO DATA": {"color": "var(--ink-muted)", "icon": "–", "label": "NO DATA"},
}


SHORT_LABEL = {
    "Liquid": "Liquid",
    "Volatile enough": "Volatile",
    "Momentum, not dead zone": "Momentum",
    "Trend confirmed": "Trend",
    "News catalyst today": "News",
    "At an inflection point": "Level",
    "Reasonable valuation": "Value",
    "Growing revenue": "Growth",
    "Genuinely profitable": "Margin",
    "Analysts on board": "Analysts",
    "Long-term uptrend": "Trend",
    "Not chasing the price": "Entry",
}


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def fmt_zone(z):
    if not z:
        return "—"
    lo, hi = z
    return f"${lo:,.2f}" if abs(lo - hi) < 0.01 else f"${lo:,.2f}–${hi:,.2f}"


def levels_block(levels, entry_label="Entry", exit_label="Exit"):
    """Compact entry/exit/stop block shared by the overview row, screener
    cards, and both checklist tabs -- entry_zone/exit_zone/stop is the
    common shape returned by compute_investing_levels and
    compute_day_trade_levels alike. compute_day_trade_levels additionally
    carries direction/risk_reward_ratio, shown here when present; investing
    levels don't have those keys so nothing extra renders for that tab."""
    levels = levels or {}
    entry_zone = levels.get("entry_zone")
    exit_zone = levels.get("exit_zone")
    stop = levels.get("stop")
    if not entry_zone and not exit_zone:
        return '<span class="empty-cell">—</span>'
    entry_flag = '<span class="zone-flag zone-flag-buy">in zone</span>' if levels.get("in_entry_zone") else ""
    exit_flag = '<span class="zone-flag zone-flag-sell">at target</span>' if levels.get("at_exit_target") else ""
    if levels.get("direction") == "short":
        entry_label, exit_label = f"Short {entry_label}", f"Cover {exit_label}"
    rr = levels.get("risk_reward_ratio")
    rr_row = ""
    if rr is not None:
        rr_cls = "rr-good" if rr >= 2 else ("rr-mid" if rr >= 1 else "rr-bad")
        rr_row = f'<div class="level-row rr-row {rr_cls}">R:R {rr:.1f}:1</div>'
    size = levels.get("position_size")
    size_row = ""
    if size:
        if size.get("shares"):
            size_row = (f'<div class="level-row size-row">Size {size["shares"]:,} sh '
                        f'(${size["dollar_risk"]:,.0f} risk, {size["pct_of_account"]:.1f}% acct)</div>')
        else:
            size_row = '<div class="level-row size-row size-row-none">Stop too wide to size within risk budget</div>'
    allocation = levels.get("allocation")
    alloc_row = ""
    if allocation:
        if allocation.get("shares"):
            alloc_row = (f'<div class="level-row size-row">Target ~{allocation["actual_pct"]:.1f}% acct '
                        f'({allocation["shares"]:,} sh, ~${allocation["position_value"]:,.0f})</div>')
        else:
            alloc_row = f'<div class="level-row size-row size-row-none">{esc(allocation["note"])}</div>'
    return f"""<div class="levels">
        <div class="level-row"><span class="level-tag level-tag-buy">{esc(entry_label)}</span> {fmt_zone(entry_zone)} {entry_flag}</div>
        <div class="level-row"><span class="level-tag level-tag-sell">{esc(exit_label)}</span> {fmt_zone(exit_zone)} {exit_flag}</div>
        <div class="level-row stop">Stop {f'${stop:,.2f}' if stop is not None else '—'}</div>
        {rr_row}
        {size_row}
        {alloc_row}
      </div>"""


def insider_badge(r):
    insider = r.get("insider") or {}
    score = insider.get("insider_score")
    if score is None or score == 50:
        return ""
    buys, sells = insider.get("insider_buys", 0), insider.get("insider_sells", 0)
    if score >= 60:
        return (f'<div class="insider-chip insider-buy" title="{esc((insider.get("notes") or [""])[0])}">'
                f'▲ Insider buying ({buys})</div>')
    return (f'<div class="insider-chip insider-sell" title="{esc((insider.get("notes") or [""])[0])}">'
            f'▼ Insider selling ({sells})</div>')


def direction_badge(r):
    direction = r.get("day_trade_direction")
    if not direction:
        return ""
    if direction == "short":
        return '<div class="insider-chip insider-sell">▼ SHORT setup</div>'
    return '<div class="insider-chip insider-buy">▲ LONG setup</div>'


def earnings_badge(r):
    er = r.get("earnings_risk") or {}
    flag = er.get("flag")
    if not flag:
        return ""
    days = er.get("days_until")
    label = "Earnings today" if days == 0 else ("Earnings tomorrow" if days == 1 else f"Earnings in {days}d")
    cls = "earnings-imminent" if flag == "imminent" else "earnings-soon"
    return f'<div class="insider-chip {cls}" title="{esc(er.get("note") or "")}">⚠ {esc(label)}</div>'


def _sparkline_svg(points, w=64, h=18):
    """Tiny inline price sparkline from today's intraday snapshots.
    points: [(ts, price, score), ...] chronological, len >= 2."""
    prices = [p[1] for p in points]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1
    n = len(prices)
    xs = [round(i / (n - 1) * (w - 2) + 1, 1) for i in range(n)]
    ys = [round(h - 1 - (p - lo) / span * (h - 2), 1) for p in prices]
    path = " ".join(f"{x},{y}" for x, y in zip(xs, ys))
    up = prices[-1] >= prices[0]
    color = "var(--good)" if up else "var(--critical)"
    return f'<svg class="sparkline" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.5"/></svg>'


def momentum_block(m):
    """Vs-yesterday's-close delta + today's intraday trend for the Day Trade
    tab -- built from results/history/ snapshots (lib.day_trade_momentum).
    Returns "" once the underlying data genuinely has nothing yet (a ticker
    that just entered the screened pool this run) rather than a fake zero."""
    if not m:
        return ""
    parts = []

    if m.get("has_prior_day") and m.get("vs_prior_close_price_pct") is not None:
        pct_val = m["vs_prior_close_price_pct"]
        cls = "pnl-good" if pct_val >= 0 else "pnl-bad"
        arrow = "▲" if pct_val >= 0 else "▼"
        parts.append(f'<div class="momentum-row {cls}">{arrow} {abs(pct_val):.1f}% vs y\'day close</div>')
    elif not m.get("has_prior_day"):
        parts.append('<div class="momentum-row momentum-muted">No prior-day data yet</div>')

    points = m.get("intraday_points") or []
    if len(points) >= 2:
        chg = m.get("intraday_price_change_pct")
        chg_str = f'{"+" if (chg or 0) >= 0 else ""}{chg:.1f}% today' if chg is not None else ""
        parts.append(f'<div class="momentum-row momentum-spark">{_sparkline_svg(points)} <span>{chg_str}</span></div>')
    else:
        parts.append('<div class="momentum-row momentum-muted">First snapshot of the day</div>')

    return f'<div class="momentum-block">{"".join(parts)}</div>'


def bar(value, label):
    value = 0 if value is None else max(0, min(100, value))
    return f"""<div class="subscore">
      <div class="subscore-label">{esc(label)}<span class="subscore-val">{value:.0f}</span></div>
      <div class="subscore-track"><div class="subscore-fill" style="width:{value:.1f}%"></div></div>
    </div>"""


def row(r, rank):
    signal = r.get("signal", "NO DATA")
    st = STATUS.get(signal, STATUS["NO DATA"])
    price = r.get("price")
    composite = r.get("composite_score")
    confidence = r.get("confidence")
    noteworthy = r.get("noteworthy")
    rationale_items = "".join(f"<li>{esc(n)}</li>" for n in r.get("rationale", [])[:12]) or "<li>No detail available.</li>"

    flag = '<span class="noteworthy-flag" title="Changed since last check">● new</span>' if noteworthy else ""

    levels = r.get("price_levels") or {}
    levels_cell = levels_block(levels, "Buy", "Sell")
    level_notes = levels.get("notes", [])

    rationale_items_full = "".join(f"<li>{esc(n)}</li>" for n in (level_notes + r.get("rationale", []))[:14]) or "<li>No detail available.</li>"

    return f"""
    <tr class="row" style="--row-accent:{st['color']}">
      <td class="rank">{rank}</td>
      <td class="ticker-cell">
        <div class="ticker">{esc(r.get('ticker'))}</div>
        <div class="company">{esc(r.get('name',''))}</div>
        {insider_badge(r)}
      </td>
      <td class="price-cell">{'$' + format(price, ',.2f') if price is not None else '—'}</td>
      <td class="signal-cell">
        <span class="pill" style="--pill-color:{st['color']}">{st['icon']} {st['label']}</span>
        {flag}
      </td>
      <td class="composite-cell">
        <span class="composite-num">{composite if composite is not None else '—'}</span>
        <span class="confidence">{f'{confidence:.0f}% conf.' if confidence is not None else ''}</span>
      </td>
      <td class="levels-cell">{levels_cell}</td>
      <td class="subscores-cell">
        {bar(r.get('technical_score'), 'Technical')}
        {bar(r.get('fundamental_score'), 'Fundamental')}
        {bar(r.get('sentiment_score'), 'News')}
        {bar(r.get('insider_score'), 'Insider') if r.get('insider_score') is not None else ''}
      </td>
      <td class="detail-cell">
        <details>
          <summary>Why</summary>
          <ul class="rationale">{rationale_items_full}</ul>
        </details>
      </td>
    </tr>"""


def screener_card(r):
    signal = r.get("signal", "NO DATA")
    st = STATUS.get(signal, STATUS["NO DATA"])
    levels = r.get("price_levels") or {}
    buy_zone = levels.get("entry_zone")
    buy_str = fmt_zone(buy_zone)
    return f"""<div class="pick-card" style="--pill-color:{st['color']}">
      <div class="pick-top">
        <span class="pick-ticker">{esc(r.get('ticker'))}</span>
        <span class="pill" style="--pill-color:{st['color']}">{st['icon']} {st['label']}</span>
      </div>
      <div class="pick-company">{esc(r.get('name',''))}</div>
      <div class="pick-score">{r.get('composite_score','—')} <span class="pick-score-label">composite</span></div>
      <div class="pick-buy">Buy zone: {buy_str}</div>
    </div>"""


RATING_TONE = {
    "Prime setup": "good", "Strong candidate": "good",
    "Worth watching": "warning", "Reasonable, some caveats": "warning",
    "Not today": "critical", "Not compelling right now": "critical",
}

NEWS_TONE_COLOR = {"positive": "var(--good)", "negative": "var(--critical)", "neutral": "var(--warning)"}


def news_feed_html(sentiment):
    """Per-ticker recent-news list -- date, source, headline, tagged by tone
    (positive/negative/neutral, with a star for the ones flagged as a real
    catalyst rather than routine noise). sentiment['headline_feed'] is a
    ROLLING list maintained across refresh cycles by scripts/refresh.py
    (added 2026-08-25, user-requested) -- not just whatever a single cycle's
    fetch happened to return, so a real catalyst stays visible here for a
    few days even on a cycle where that ticker's news feed was thin.
    Deliberately shows negative/neutral headlines too, not just positive
    ones -- a research tool that only surfaced good news would be actively
    misleading about what's driving the price."""
    feed = (sentiment or {}).get("headline_feed") or []
    if not feed:
        return '<div class="empty-note">No recent news found.</div>'
    items = []
    for h in feed[:12]:
        tone = h.get("tone", "neutral")
        color = NEWS_TONE_COLOR.get(tone, "var(--warning)")
        title = esc(h.get("title", "") or "")
        date = esc(h.get("date", "") or "")
        site = esc(h.get("site", "") or "")
        url = h.get("url")
        star = " ★" if h.get("magnitude") else ""
        headline_html = f'<a href="{esc(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
        meta = date + (f" — {site}" if site else "")
        meta_html = f'<span class="news-meta">{meta}</span>' if meta else ""
        items.append(
            f'<li class="news-item" style="border-left-color:{color}">'
            f'<span class="news-tone" style="color:{color}">{tone}{star}</span>'
            f'{headline_html}'
            f'{meta_html}'
            f'</li>'
        )
    return f'<ul class="news-feed">{"".join(items)}</ul>'


def checklist_row(r, checklist_key, levels_key, entry_label, exit_label, rank, score_key=None, momentum=None):
    cl = r.get(checklist_key) or {}
    items = cl.get("items", [])
    passed, total = cl.get("passed", 0), cl.get("total", len(items))

    if score_key and r.get(score_key) is not None:
        rating_key = score_key.replace("_score", "_rating")
        rating = r.get(rating_key, cl.get("rating", ""))
        tone = RATING_TONE.get(rating, "warning")
        score_val = r[score_key]
        score_badge = f'<div class="algo-score" style="--score-color:var(--{tone})">{score_val:.0f}</div>'
    else:
        rating = cl.get("rating", "")
        tone = RATING_TONE.get(rating, "warning")
        score_badge = ""

    levels = r.get(levels_key) or {}
    levels_cell = levels_block(levels, entry_label, exit_label)

    chips = "".join(
        f'<span class="chip chip-{"pass" if it["passed"] else "fail"}" title="{esc(it["detail"])}">'
        f'{"✓" if it["passed"] else "·"} {esc(SHORT_LABEL.get(it["label"], it["label"]))}</span>'
        for it in items
    )
    detail_items = "".join(
        f'<li class="{"pass" if it["passed"] else "fail"}"><b>{esc(it["label"])}:</b> {esc(it["detail"])}</li>'
        for it in items
    )
    zone_notes = "".join(f'<li class="pass"><b>Price zone:</b> {esc(n)}</li>' for n in levels.get("notes", []))

    price = r.get("price")
    return f"""
    <tr class="row" style="--row-accent:var(--{tone})">
      <td class="rank">{rank}</td>
      <td class="ticker-cell">
        <div class="ticker">{esc(r.get('ticker'))}</div>
        <div class="company">{esc(r.get('name',''))}</div>
        {f'<div class="source-tag">{esc(r["source_label"])}</div>' if r.get('source_label') else ''}
        {direction_badge(r) if score_key == "day_trade_score" else ""}
        {earnings_badge(r)}
        {momentum_block((momentum or {}).get(r.get('ticker'))) if score_key == "day_trade_score" else ""}
        {insider_badge(r)}
      </td>
      <td class="price-cell">{'$' + format(price, ',.2f') if price is not None else '—'}</td>
      <td class="rating-cell">
        {score_badge}
        <span class="pill" style="--pill-color:var(--{tone})">{rating}</span>
        <span class="pass-count">{passed}/{total}</span>
      </td>
      <td class="levels-cell">{levels_cell}</td>
      <td class="chips-cell">{chips}</td>
      <td class="detail-cell">
        <details>
          <summary>Detail</summary>
          <ul class="rationale checklist-detail">{zone_notes}{detail_items}</ul>
        </details>
        <details>
          <summary>News</summary>
          {news_feed_html(r.get("sentiment"))}
        </details>
      </td>
    </tr>"""


def checklist_table(results, checklist_key, levels_key, entry_label, exit_label, title, blurb, score_key=None, momentum=None):
    if score_key:
        ranked = sorted(
            results,
            key=lambda r: (r.get(score_key) is None, -(r.get(score_key) or 0), -(r.get(checklist_key, {}).get("passed") or 0)),
        )
        top = [r for r in ranked if (r.get(score_key) or 0) >= 70]
        top_str = ", ".join(r["ticker"] for r in top) if top else "none right now"
        summary_line = f"Scored algorithmically (0-100, weighting liquidity, volatility, momentum, trend agreement, news catalyst, and proximity to a breakout/breakdown level) and ranked highest-first. Currently rated \"Prime setup\" (70+): <b>{esc(top_str)}</b>."
    else:
        ranked = sorted(results, key=lambda r: -(r.get(checklist_key, {}).get("passed") or 0))
        top = [r for r in ranked if (r.get(checklist_key, {}).get("passed") or 0) >= 5]
        top_str = ", ".join(r["ticker"] for r in top) if top else "none right now"
        summary_line = f"Currently clearing 5+ of 6 criteria: <b>{esc(top_str)}</b>."

    rows = "".join(
        checklist_row(r, checklist_key, levels_key, entry_label, exit_label, i + 1, score_key=score_key, momentum=momentum)
        for i, r in enumerate(ranked)
    )
    return f"""
    <h2 class="section-title">{esc(title)}</h2>
    <p class="tab-blurb">{esc(blurb)} {summary_line}</p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr><th></th><th>Ticker</th><th>Price</th><th>Rating</th><th>{esc(entry_label)} / {esc(exit_label)}</th><th>Checklist</th><th>Detail</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def money(v):
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def pct(v):
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def portfolio_tab_html(trades, price_lookup, levels_lookup, sector_lookup):
    computed = compute_portfolio(trades, price_lookup, levels_lookup)
    open_pos, closed, warnings, totals = (
        computed["open_positions"], computed["closed_lots"], computed["warnings"], computed["totals"]
    )

    sector_items = [
        (p["ticker"], p["current_price"] * p["shares"] if p["tracked"] else None,
         sector_lookup.get(p["ticker"]) or get_sector(p["ticker"]))
        for p in open_pos
    ]
    sector_result = sector_breakdown(sector_items)
    if sector_result["breakdown"]:
        untracked_note = "" if all(p["tracked"] for p in open_pos) else \
            '<div class="empty-note" style="margin-top:8px;">Positions not tracked by the pipeline aren\'t weighed in here (no live price to size them by).</div>'
        sector_section = f"""
    <h2 class="section-title" style="margin-top:28px;">Sector exposure</h2>
    <p class="tab-blurb">Dollar-weighted by current market value. <span id="portfolio-sector-rating">{sector_rating_pill(sector_result['rating'])}
      {esc(sector_result['top_sector'])} makes up {sector_result['top_pct']:.0f}% of your tracked position value.</span></p>
    <div class="sector-mix" id="portfolio-sector-mix">{sector_bars_html(sector_result['breakdown'])}</div>
    {untracked_note}"""
    else:
        sector_section = """
    <h2 class="section-title" style="margin-top:28px;">Sector exposure</h2>
    <div class="empty-note" id="portfolio-sector-mix">Add an open position to see how it's spread across sectors.</div>"""

    def pnl_class(v):
        if v is None:
            return ""
        return "pnl-good" if v > 0 else ("pnl-bad" if v < 0 else "")

    drift = flag_drift(open_pos, totals.get('tracked_market_value'))

    def drift_badge(ticker):
        d = drift.get(ticker) or {}
        flag = d.get("flag")
        if not flag:
            return ""
        cls = "earnings-imminent" if flag == "trim" else "earnings-soon"
        label = "Trim — overweight" if flag == "trim" else "Watch — approaching target weight"
        return f'<div class="insider-chip {cls}" title="{esc(d.get("note") or "")}">⚠ {esc(label)} ({d["weight_pct"]:.1f}%)</div>'

    pos_rows = "".join(f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(p['ticker'])}</div>
          {'<div class="source-tag">not tracked</div>' if not p['tracked'] else ''}
          {f'<div class="insider-chip insider-buy">In {esc(p["zone_flag"])} zone</div>' if p.get('zone_flag') else ''}
          {drift_badge(p['ticker'])}
        </td>
        <td class="price-cell">{p['shares']:g}</td>
        <td class="price-cell">${p['avg_cost']:,.2f}</td>
        <td class="price-cell">{'$' + format(p['current_price'], ',.2f') if p['current_price'] is not None else '—'}</td>
        <td class="price-cell">{money(p['current_price'] * p['shares']) if p['current_price'] is not None else '—'}</td>
        <td class="price-cell {pnl_class(p['unrealized_pnl'])}">{money(p['unrealized_pnl'])}</td>
        <td class="price-cell {pnl_class(p['unrealized_pnl'])}">{pct(p['unrealized_pnl_pct'])}</td>
      </tr>""" for p in open_pos) or '<tr><td colspan="7" class="empty-cell">No open positions yet — add a buy below.</td></tr>'

    closed_rows = "".join(f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(c['ticker'])}</div></td>
        <td class="price-cell">{c['shares']:g}</td>
        <td class="price-cell">${c['buy_price']:,.2f}</td>
        <td class="price-cell">${c['sell_price']:,.2f}</td>
        <td class="price-cell">{esc(c.get('sell_date') or '—')}</td>
        <td class="price-cell {pnl_class(c['realized_pnl'])}">{money(c['realized_pnl'])}</td>
        <td class="price-cell {pnl_class(c['realized_pnl'])}">{pct(c['realized_pnl_pct'])}</td>
      </tr>""" for c in closed[:20]) or '<tr><td colspan="7" class="empty-cell">No closed trades yet.</td></tr>'

    warn_html = "".join(f'<li>{esc(w)}</li>' for w in warnings)
    warn_block = f'<ul class="rationale" style="max-width:none;color:var(--critical)">{warn_html}</ul>' if warnings else ""

    seed_json = esc_json({"trades": trades})
    price_json = esc_json(price_lookup)
    levels_json = esc_json(levels_lookup)
    sector_json = esc_json(sector_lookup)

    return f"""
    <h2 class="section-title">Your portfolio</h2>
    <p class="tab-blurb">Trades you've reported — either in chat or with the form below. P&amp;L is computed
      automatically (FIFO cost basis) against the latest tracked price. Positions in tickers outside the
      watchlist/screened pool show as "not tracked" — the math still works, you'll just need to give it a
      current price by adding a closing trade yourself. This is bookkeeping, not tax or accounting advice.</p>

    <div class="summary-strip" id="portfolio-summary">
      <div class="summary-chip"><b>{money(totals['tracked_cost_basis'])}</b>&nbsp;cost basis</div>
      <div class="summary-chip"><b>{money(totals['tracked_market_value'])}</b>&nbsp;market value</div>
      <div class="summary-chip {pnl_class(totals['unrealized_pnl'])}"><b>{money(totals['unrealized_pnl'])}</b>&nbsp;unrealized ({pct(totals['unrealized_pnl_pct'])})</div>
      <div class="summary-chip {pnl_class(totals['realized_pnl'])}"><b>{money(totals['realized_pnl'])}</b>&nbsp;realized (all-time)</div>
    </div>
    {warn_block}

    <div class="table-scroll">
      <table id="portfolio-open-table">
        <thead><tr><th></th><th>Shares</th><th>Avg cost</th><th>Price</th><th>Value</th><th>Unrealized $</th><th>Unrealized %</th></tr></thead>
        <tbody id="portfolio-open-body">{pos_rows}</tbody>
      </table>
    </div>
    {sector_section}

    <h2 class="section-title" style="margin-top:28px;">Add a trade</h2>
    <form id="trade-form" class="trade-form">
      <input type="text" name="ticker" placeholder="Ticker (e.g. AAPL)" required maxlength="8" style="text-transform:uppercase;">
      <select name="side"><option value="buy">Buy</option><option value="sell">Sell</option></select>
      <input type="number" name="shares" placeholder="Shares" step="any" min="0.0001" required>
      <input type="number" name="price" placeholder="Price ($)" step="any" min="0.0001" required>
      <input type="date" name="date" required>
      <input type="text" name="note" placeholder="Note (optional)" maxlength="80">
      <button type="submit">Add trade</button>
    </form>
    <p class="tab-blurb" id="trade-form-status">Trades added here are saved permanently in this browser — they'll still be here after the hourly refresh, tomorrow, next year. (They're tied to this browser/device only; on a new device, add them there too, or just tell Claude in chat and they'll show up everywhere.)</p>

    <h2 class="section-title" style="margin-top:28px;">Closed trades</h2>
    <div class="table-scroll">
      <table>
        <thead><tr><th></th><th>Shares</th><th>Buy</th><th>Sell</th><th>Sold</th><th>Realized $</th><th>Realized %</th></tr></thead>
        <tbody id="portfolio-closed-body">{closed_rows}</tbody>
      </table>
    </div>

    <script type="application/json" id="portfolio-seed">{seed_json}</script>
    <script type="application/json" id="portfolio-prices">{price_json}</script>
    <script type="application/json" id="portfolio-levels">{levels_json}</script>
    <script type="application/json" id="portfolio-sectors">{sector_json}</script>
    """


def track_record_tab_html():
    tr = build_track_record()

    if tr["insufficient_history"]:
        n = tr["snapshot_count"]
        return f"""
    <h2 class="section-title">Signal track record</h2>
    <p class="tab-blurb">How past BUY/SELL calls have actually performed, graded automatically against what
      the price did afterward. This builds up from the hourly refresh — right now there {'is' if n == 1 else 'are'}
      only {n} snapshot{'s' if n != 1 else ''} recorded, not enough to grade a single call yet. Check back
      after a few days of runs.</p>
    <div class="empty-note">No graded calls yet.</div>
    """

    summary = tr["summary"]
    closed = tr["closed_calls"]
    open_calls = tr["open_calls"]

    def call_row(c):
        icon = "✓" if c["correct"] is True else ("✗" if c["correct"] is False else "…")
        cls = "pnl-good" if c["correct"] is True else ("pnl-bad" if c["correct"] is False else "")
        st = STATUS.get(c["signal"], STATUS["NO DATA"])
        start_date = (c.get("start_ts") or "")[:10]
        end_date = (c.get("end_ts") or "")[:10]
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(c['ticker'])}</div><div class="company">{esc(c.get('name') or '')}</div></td>
        <td class="signal-cell"><span class="pill" style="--pill-color:{st['color']}">{st['icon']} {st['label']}</span></td>
        <td class="price-cell">${c['start_price']:,.2f}</td>
        <td class="price-cell">{'$' + format(c['end_price'], ',.2f') if c['end_price'] is not None else '—'}</td>
        <td class="price-cell {cls}">{pct(c['return_pct'])}</td>
        <td class="price-cell {cls}">{icon}</td>
        <td class="price-cell" style="white-space:nowrap;font-size:11px;color:var(--ink-faint);">{esc(start_date)} → {esc(end_date)}</td>
      </tr>"""

    closed_rows = "".join(call_row(c) for c in closed[:40]) or '<tr><td colspan="7" class="empty-cell">No closed calls yet.</td></tr>'
    open_rows = "".join(call_row(c) for c in open_calls) or '<tr><td colspan="7" class="empty-cell">No open calls right now.</td></tr>'

    hit = summary["hit_rate_pct"]
    buy_hit = summary["buy_hit_rate_pct"]
    sell_hit = summary["sell_hit_rate_pct"]
    avg_ret = summary["avg_return_pct"]

    return f"""
    <h2 class="section-title">Signal track record</h2>
    <p class="tab-blurb">Every BUY/SELL call the system has made, graded against what actually happened to the
      price afterward — a BUY is "correct" if it finished up, a SELL if it finished down. Based on
      {tr['snapshot_count']} hourly snapshots since {esc(tr['first_snapshot_at'][:10])}. HOLD isn't graded —
      it's not a directional bet.</p>

    <div class="summary-strip">
      <div class="summary-chip"><b>{f'{hit:.0f}%' if hit is not None else '—'}</b>&nbsp;overall hit rate</div>
      <div class="summary-chip"><b>{summary['total_closed']}</b>&nbsp;closed calls</div>
      <div class="summary-chip"><b>{f'{buy_hit:.0f}%' if buy_hit is not None else '—'}</b>&nbsp;BUY hit rate</div>
      <div class="summary-chip"><b>{f'{sell_hit:.0f}%' if sell_hit is not None else '—'}</b>&nbsp;SELL hit rate</div>
      <div class="summary-chip {'pnl-good' if (avg_ret or 0) >= 0 else 'pnl-bad'}"><b>{pct(avg_ret)}</b>&nbsp;avg return/call</div>
    </div>

    <h2 class="section-title" style="margin-top:24px;">Closed calls</h2>
    <div class="table-scroll">
      <table>
        <thead><tr><th></th><th>Signal</th><th>Called at</th><th>Closed at</th><th>Return</th><th></th><th>Window</th></tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </div>

    <h2 class="section-title" style="margin-top:24px;">Open calls</h2>
    <p class="tab-blurb">Still active — not graded yet, shown provisionally against the latest known price.</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th></th><th>Signal</th><th>Called at</th><th>Now</th><th>Return so far</th><th></th><th>Window</th></tr></thead>
        <tbody>{open_rows}</tbody>
      </table>
    </div>
    """


def day_trade_track_record_html():
    tr = build_day_trade_track_record()

    if tr["insufficient_history"]:
        n = tr["snapshot_count"]
        return f"""
    <h2 class="section-title" style="margin-top:32px;">Day-trade track record</h2>
    <p class="tab-blurb">How "Prime setup" calls (day-trade score ≥ 70) have actually resolved, graded automatically
      ~1 day and ~3 days after the call — the horizon a day trade is meant to play out on, not the weeks-long
      window the signal track record above uses. Right now there {'is' if n == 1 else 'are'} only {n}
      snapshot{'s' if n != 1 else ''} recorded, not enough to grade a single call yet.</p>
    <div class="empty-note">No graded day-trade calls yet.</div>
    """

    summary = tr["summary"]
    closed = tr["closed_calls"]
    open_calls = tr["open_calls"]

    def dir_pill(direction):
        tone = "good" if direction == "long" else "critical"
        arrow = "▲" if direction == "long" else "▼"
        return f'<span class="pill" style="--pill-color:var(--{tone})">{arrow} {direction.upper()}</span>'

    def ret_cell(ret, correct):
        cls = "pnl-good" if correct is True else ("pnl-bad" if correct is False else "")
        icon = "✓" if correct is True else ("✗" if correct is False else "…")
        if ret is None:
            return '<td class="price-cell">—</td><td class="price-cell"></td>'
        return f'<td class="price-cell {cls}">{pct(ret)}</td><td class="price-cell {cls}">{icon}</td>'

    def call_row(c):
        start_date = (c.get("start_ts") or "")[:16].replace("T", " ")
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(c['ticker'])}</div><div class="company">{esc(c.get('name') or '')}</div></td>
        <td class="signal-cell">{dir_pill(c['direction'])}</td>
        <td class="price-cell">${c['start_price']:,.2f}</td>
        {ret_cell(c.get('return_pct_1d'), c.get('correct_1d'))}
        {ret_cell(c.get('return_pct_3d'), c.get('correct_3d'))}
        <td class="price-cell" style="white-space:nowrap;font-size:11px;color:var(--ink-faint);">{esc(start_date)} UTC</td>
      </tr>"""

    closed_rows = "".join(call_row(c) for c in closed[:40]) or '<tr><td colspan="7" class="empty-cell">No closed calls yet.</td></tr>'
    open_rows = "".join(call_row(c) for c in open_calls) or '<tr><td colspan="7" class="empty-cell">No open calls right now.</td></tr>'

    hit1 = summary["hit_rate_1d_pct"]
    hit3 = summary["hit_rate_3d_pct"]
    avg1 = summary["avg_return_1d_pct"]
    avg3 = summary["avg_return_3d_pct"]

    return f"""
    <h2 class="section-title" style="margin-top:32px;">Day-trade track record</h2>
    <p class="tab-blurb">Every "Prime setup" call (day-trade score ≥ 70, watchlist and screened candidates alike),
      graded against what price actually did ~1 day and ~3 days later — a long is "correct" if it finished up
      from the call price, a short if it finished down. Based on {tr['snapshot_count']} snapshots.</p>

    <div class="summary-strip">
      <div class="summary-chip"><b>{f'{hit1:.0f}%' if hit1 is not None else '—'}</b>&nbsp;1-day hit rate</div>
      <div class="summary-chip"><b>{f'{hit3:.0f}%' if hit3 is not None else '—'}</b>&nbsp;3-day hit rate</div>
      <div class="summary-chip"><b>{summary['total_closed']}</b>&nbsp;closed calls</div>
      <div class="summary-chip"><b>{summary['long_count']}</b>&nbsp;long / <b>{summary['short_count']}</b>&nbsp;short</div>
      <div class="summary-chip {'pnl-good' if (avg1 or 0) >= 0 else 'pnl-bad'}"><b>{pct(avg1)}</b>&nbsp;avg 1d return</div>
      <div class="summary-chip {'pnl-good' if (avg3 or 0) >= 0 else 'pnl-bad'}"><b>{pct(avg3)}</b>&nbsp;avg 3d return</div>
    </div>

    <h2 class="section-title" style="margin-top:24px;">Closed calls</h2>
    <div class="table-scroll">
      <table>
        <thead><tr><th></th><th>Direction</th><th>Called at</th><th>1d return</th><th></th><th>3d return</th><th></th><th>Called</th></tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </div>

    <h2 class="section-title" style="margin-top:24px;">Open calls</h2>
    <p class="tab-blurb">Still active or too recent to reach a horizon yet.</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th></th><th>Direction</th><th>Called at</th><th>1d return</th><th></th><th>3d return</th><th></th><th>Called</th></tr></thead>
        <tbody>{open_rows}</tbody>
      </table>
    </div>
    """


def esc_json(obj):
    """JSON for embedding inside a <script type="application/json"> tag -- escape
    '</' so a ticker/note string can never prematurely close the script tag."""
    return json.dumps(obj).replace("</", "<\\/")


def sector_bars_html(breakdown):
    return "".join(f"""
      <div class="sector-row">
        <div class="sector-label">{esc(b['sector'])}<span class="sector-pct">{b['pct']:.0f}%</span></div>
        <div class="subscore-track"><div class="subscore-fill" style="width:{b['pct']:.1f}%"></div></div>
      </div>""" for b in breakdown)


def sector_rating_pill(rating):
    tone = "critical" if rating == "Concentrated" else ("warning" if rating == "Leaning heavy" else "good")
    return f'<span class="pill" style="--pill-color:var(--{tone})">{esc(rating)}</span>'


def sector_mix_html(pool):
    items = []
    for r in pool:
        ticker = r.get("ticker")
        if not ticker:
            continue
        items.append((ticker, 1, get_sector(ticker, r.get("fundamental"))))
    result = sector_breakdown(items)
    if not result["breakdown"]:
        return """
    <h2 class="section-title">Sector mix</h2>
    <div class="empty-note">Not enough data yet.</div>
    """
    return f"""
    <h2 class="section-title">Sector mix</h2>
    <p class="tab-blurb">How the tracked watchlist and screened picks are spread across sectors — a heads-up before
      adding more names from an already-heavy bucket. {sector_rating_pill(result['rating'])}
      {esc(result['top_sector'])} is the largest slice at {result['top_pct']:.0f}%.</p>
    <div class="sector-mix">{sector_bars_html(result['breakdown'])}</div>
    """


def home_highlights_html(results):
    """A few scannable callouts for the front page -- noteworthy changes if
    there are any, else just the strongest BUY and weakest SELL right now.
    Returns "" when there's nothing worth calling out, rather than forcing
    a highlight that isn't real."""
    cards = []
    noteworthy = [r for r in results if r.get("noteworthy")]
    if noteworthy:
        for r in noteworthy[:3]:
            st = STATUS.get(r.get("signal", "NO DATA"), STATUS["NO DATA"])
            reasons = r.get("noteworthy_reasons") or []
            body = reasons[0] if reasons else "Signal changed since the last check."
            cards.append(f"""<div class="highlight-card" style="--pill-color:{st['color']}">
              <div class="h-eyebrow">Noteworthy</div>
              <div class="h-title">{esc(r['ticker'])} — {st['label']}</div>
              <div class="h-body">{esc(body)}</div>
            </div>""")
    else:
        buys = sorted([r for r in results if r.get("signal") == "BUY"], key=lambda r: -(r.get("composite_score") or 0))
        if buys:
            top = buys[0]
            st = STATUS["BUY"]
            cards.append(f"""<div class="highlight-card" style="--pill-color:{st['color']}">
              <div class="h-eyebrow">Top signal</div>
              <div class="h-title">{esc(top['ticker'])} — BUY, {top.get('composite_score', '—')}</div>
              <div class="h-body">Strongest composite score on the watchlist right now.</div>
            </div>""")
        sells = sorted([r for r in results if r.get("signal") == "SELL"], key=lambda r: (r.get("composite_score") if r.get("composite_score") is not None else 999))
        if sells:
            top = sells[0]
            st = STATUS["SELL"]
            cards.append(f"""<div class="highlight-card" style="--pill-color:{st['color']}">
              <div class="h-eyebrow">Weakest signal</div>
              <div class="h-title">{esc(top['ticker'])} — SELL, {top.get('composite_score', '—')}</div>
              <div class="h-body">Lowest composite score on the watchlist right now.</div>
            </div>""")

    if not cards:
        return ""
    return f'<div class="highlight-row">{"".join(cards)}</div>'


def generate_html(payload=None):
    if payload is None:
        with open(RESULTS_PATH) as f:
            payload = json.load(f)

    results = sorted(payload.get("watchlist_results", []),
                      key=lambda r: (r.get("composite_score") is None, -(r.get("composite_score") or 0)))
    discovered = payload.get("discovered_candidates", [])
    checklist_pool = results + discovered  # checklist tabs rank the watchlist *and* screened finds together
    screener = payload.get("screener_picks", [])
    pending = payload.get("pending_tickers", [])
    generated_at = payload.get("generated_at")
    market_note = payload.get("market_note", "")

    try:
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00")) if generated_at else datetime.now(timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    updated_str = dt.strftime("%b %d, %Y – %H:%M UTC")

    rows_html = "\n".join(row(r, i + 1) for i, r in enumerate(results))
    highlights_html = home_highlights_html(results)
    screener_html = "\n".join(screener_card(r) for r in screener) if screener else \
        '<div class="empty-note">No screener picks yet — the daily screener pass hasn’t run.</div>'

    price_lookup = {r["ticker"]: r["price"] for r in checklist_pool if r.get("ticker") and r.get("price") is not None}
    levels_lookup = {r["ticker"]: r["investing_levels"] for r in checklist_pool if r.get("ticker") and r.get("investing_levels")}
    sector_lookup = {r["ticker"]: get_sector(r["ticker"], r.get("fundamental")) for r in checklist_pool if r.get("ticker")}
    sector_mix = sector_mix_html(checklist_pool)
    portfolio = load_portfolio()
    portfolio_html = portfolio_tab_html(portfolio.get("trades", []), price_lookup, levels_lookup, sector_lookup)
    track_record_html = track_record_tab_html()
    day_trade_track_record_html_ = day_trade_track_record_html()
    momentum = build_momentum()
    pending_html = ""
    if pending:
        pending_html = f'<div class="pending-note">Also tracking, awaiting first data pull: {esc(", ".join(pending))}</div>'

    day_trade_html = checklist_table(
        checklist_pool, "day_trade_checklist", "day_trade_levels", "Entry", "Target",
        "Day-trade screener",
        "Every ticker gets a day-trade score, not just a pass/fail grade — the same six factors "
        "(liquidity, volatility, live momentum, trend agreement, a real news catalyst, proximity to a "
        "breakout/breakdown level), combined into one 0-100 number so setups can be ranked against each "
        "other. Entry/target/stop are sized off the last 5-10 sessions and the stock's own daily volatility — a "
        "trade meant to resolve in hours to days, not months. Includes the watchlist plus screened "
        "candidates (tagged below the ticker) — this is research, not an order; it never places trades for you.",
        score_key="day_trade_score",
        momentum=momentum,
    ) if checklist_pool else '<div class="empty-note">No data yet.</div>'

    investing_html = checklist_table(
        checklist_pool, "investing_checklist", "investing_levels", "Buy", "Sell",
        "Investing checklist",
        "Six pass/fail checks tuned for buy-and-hold: reasonable valuation, real revenue growth, genuine "
        "profitability, analyst confidence, a long-term uptrend, and not chasing an extended price. "
        "Buy/sell/stop are sized off moving averages, the 52-week range, and the analyst price target — "
        "levels for a position you might hold for months. Includes the watchlist plus screened growth "
        "candidates (tagged below the ticker)."
    ) if checklist_pool else '<div class="empty-note">No data yet.</div>'

    buy_count = sum(1 for r in results if r.get("signal") == "BUY")
    sell_count = sum(1 for r in results if r.get("signal") == "SELL")
    hold_count = sum(1 for r in results if r.get("signal") == "HOLD")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bellwether</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    color-scheme: light;
    --bg: #EFEDE7;
    --surface: #FFFFFF;
    --surface-2: #F8F6F0;
    --ink: #1C1B18;
    --ink-muted: #5B594F;
    --ink-faint: #948F7D;
    --border: #DDD9CC;
    --accent: #9C6B1F;
    --accent-soft: #E9DCC3;
    --good: #0ca30c;
    --warning: #c98500;
    --critical: #d03b3b;
    --seq-blue: #2a78d6;
    --seq-blue-track: #E3ECF8;
    --shadow: 0 1px 2px rgba(28,27,24,0.06), 0 8px 24px rgba(28,27,24,0.05);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --bg: #14171B;
      --surface: #1B1F24;
      --surface-2: #20252B;
      --ink: #ECE9E0;
      --ink-muted: #A6A392;
      --ink-faint: #6E7178;
      --border: #2B3038;
      --accent: #D9A441;
      --accent-soft: #3A311E;
      --good: #3FBF5F;
      --warning: #fab219;
      --critical: #E2664F;
      --seq-blue: #5598e7;
      --seq-blue-track: #202B3A;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.35);
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --bg: #14171B;
    --surface: #1B1F24;
    --surface-2: #20252B;
    --ink: #ECE9E0;
    --ink-muted: #A6A392;
    --ink-faint: #6E7178;
    --border: #2B3038;
    --accent: #D9A441;
    --accent-soft: #3A311E;
    --good: #3FBF5F;
    --warning: #fab219;
    --critical: #E2664F;
    --seq-blue: #5598e7;
    --seq-blue-track: #202B3A;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.35);
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: 'IBM Plex Sans', -apple-system, 'Segoe UI', sans-serif;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 1320px;
    margin: 0 auto;
    padding: clamp(18px, 3vw, 36px) clamp(16px, 4vw, 40px) 64px;
  }}

  .shell {{
    display: flex;
    align-items: flex-start;
    gap: clamp(20px, 3vw, 40px);
  }}

  /* ---------- sidebar ---------- */
  .sidebar {{
    flex-shrink: 0;
    width: 220px;
    position: sticky;
    top: 20px;
    display: flex;
    flex-direction: column;
    gap: 26px;
  }}
  .brand {{ display: flex; align-items: center; gap: 11px; }}
  .brand-mark {{
    flex-shrink: 0;
    width: 38px;
    height: 38px;
    border-radius: 10px;
    background: linear-gradient(155deg, var(--accent) 0%, color-mix(in srgb, var(--accent) 70%, black) 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--surface);
    box-shadow: var(--shadow);
  }}
  .brand-text h1 {{
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: 22px;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .brand-text .eyebrow {{
    display: block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--ink-faint);
    margin-top: 1px;
  }}

  .side-nav {{
    display: flex;
    flex-direction: column;
    gap: 2px;
  }}
  .nav-btn {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13.5px;
    font-weight: 500;
    color: var(--ink-muted);
    background: none;
    border: none;
    border-left: 2px solid transparent;
    text-align: left;
    padding: 9px 12px;
    border-radius: 0 7px 7px 0;
    cursor: pointer;
    white-space: nowrap;
  }}
  .nav-btn:hover {{ color: var(--ink); background: var(--surface-2); }}
  .nav-btn.is-active {{
    color: var(--ink);
    font-weight: 600;
    background: var(--surface-2);
    border-left-color: var(--accent);
  }}

  .sidebar-status {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11.5px;
    color: var(--ink-faint);
    padding-top: 18px;
    border-top: 1px solid var(--border);
    line-height: 1.7;
  }}
  .sidebar-status .updated {{
    color: var(--ink-muted);
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .live-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--good);
    animation: live-pulse 2.4s ease-out infinite;
  }}
  @keyframes live-pulse {{
    0%   {{ box-shadow: 0 0 0 0 color-mix(in srgb, var(--good) 45%, transparent); }}
    70%  {{ box-shadow: 0 0 0 6px transparent; }}
    100% {{ box-shadow: 0 0 0 0 transparent; }}
  }}

  /* ---------- main ---------- */
  .main {{ flex: 1; min-width: 0; }}

  .home-tagline {{
    color: var(--ink-muted);
    font-size: 14px;
    max-width: 66ch;
    margin: 0 0 22px;
  }}

  .highlight-row {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .highlight-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--pill-color, var(--accent));
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: var(--shadow);
  }}
  .highlight-card .h-eyebrow {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--ink-faint);
    margin-bottom: 5px;
  }}
  .highlight-card .h-title {{
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 15px;
    margin-bottom: 3px;
  }}
  .highlight-card .h-body {{
    font-size: 12.5px;
    color: var(--ink-muted);
  }}

  .summary-strip {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 24px;
  }}
  .summary-chip {{
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 8px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 13px;
    color: var(--ink-muted);
  }}
  .summary-chip b {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 15px;
    color: var(--ink);
  }}
  .summary-chip.buy b {{ color: var(--good); }}
  .summary-chip.sell b {{ color: var(--critical); }}

  .sector-mix {{
    display: flex;
    flex-direction: column;
    gap: 9px;
    max-width: 560px;
  }}
  .sector-row {{ display: flex; flex-direction: column; gap: 4px; }}
  .sector-label {{
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    color: var(--ink-muted);
  }}
  .sector-pct {{
    font-family: 'IBM Plex Mono', monospace;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    font-weight: 500;
  }}

  .tab-panel {{ display: none; }}
  .tab-panel.is-active {{ display: block; }}
  .tab-blurb {{
    font-size: 13px;
    color: var(--ink-muted);
    max-width: 72ch;
    margin: 0 0 14px;
  }}
  .tab-blurb b {{ color: var(--ink); }}

  .rating-cell {{ white-space: nowrap; }}
  .algo-score {{
    display: inline-block;
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 15px;
    color: var(--score-color, var(--ink));
    margin-bottom: 3px;
  }}
  .pass-count {{
    display: block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--ink-faint);
    margin-top: 3px;
  }}
  .chips-cell {{ min-width: 300px; }}
  .chip {{
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    font-weight: 500;
    padding: 3px 7px;
    border-radius: 6px;
    margin: 0 4px 4px 0;
    white-space: nowrap;
  }}
  .chip-pass {{ color: var(--good); background: color-mix(in srgb, var(--good) 13%, transparent); }}
  .chip-fail {{ color: var(--ink-faint); background: var(--surface-2); }}
  ul.checklist-detail {{ max-width: 52ch; }}
  ul.checklist-detail li.pass b {{ color: var(--good); }}
  ul.checklist-detail li.fail b {{ color: var(--ink-faint); }}

  ul.news-feed {{ max-width: 52ch; list-style: none; margin: 8px 0 0; padding: 0; }}
  li.news-item {{
    border-left: 3px solid var(--warning);
    padding: 4px 0 4px 10px;
    margin-bottom: 6px;
    font-size: 12.5px;
    line-height: 1.4;
  }}
  li.news-item a {{ color: inherit; text-decoration: underline; text-decoration-color: color-mix(in srgb, currentColor 35%, transparent); }}
  .news-tone {{ font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.02em; margin-right: 4px; }}
  .news-meta {{ display: block; color: var(--ink-faint); font-size: 11px; margin-top: 1px; }}

  section {{ margin-bottom: 36px; }}
  h2.section-title {{
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: 19px;
    margin: 0 0 14px;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  h2.section-title::after {{
    content: "";
    flex: 1;
    height: 1px;
    background: var(--border);
  }}

  .table-scroll {{ overflow-x: auto; border-radius: 12px; box-shadow: var(--shadow); }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--surface);
    min-width: 920px;
  }}
  thead th {{
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    font-weight: 600;
    padding: 12px 14px;
    background: var(--surface-2);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  tbody tr.row {{
    border-left: 3px solid var(--row-accent);
    border-bottom: 1px solid var(--border);
  }}
  tbody tr.row:last-child {{ border-bottom: none; }}
  tbody tr.row:hover {{ background: var(--surface-2); }}
  td {{ padding: 12px 14px; vertical-align: middle; font-size: 14px; }}
  td.rank {{ font-family: 'IBM Plex Mono', monospace; color: var(--ink-faint); font-size: 12px; width: 28px; }}

  .ticker-cell .ticker {{ font-family: 'IBM Plex Mono', monospace; font-weight: 600; font-size: 14.5px; }}
  .ticker-cell .company {{ color: var(--ink-muted); font-size: 12px; margin-top: 1px; }}
  .ticker-cell .source-tag {{
    display: inline-block;
    margin-top: 4px;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--accent);
    background: var(--accent-soft);
    padding: 1px 6px;
    border-radius: 4px;
  }}

  .insider-chip {{
    display: inline-block;
    margin-top: 4px;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.02em;
    padding: 1px 6px;
    border-radius: 4px;
  }}
  .insider-buy {{ color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }}
  .insider-sell {{ color: var(--critical); background: color-mix(in srgb, var(--critical) 14%, transparent); }}

  .price-cell {{ font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }}

  .pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.03em;
    color: var(--pill-color);
    background: color-mix(in srgb, var(--pill-color) 14%, transparent);
    white-space: nowrap;
  }}
  .noteworthy-flag {{
    display: inline-block;
    margin-left: 6px;
    font-size: 10.5px;
    color: var(--accent);
    font-weight: 600;
  }}

  .composite-cell {{ font-family: 'IBM Plex Mono', monospace; white-space: nowrap; }}
  .composite-num {{ font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .confidence {{ display: block; font-size: 10.5px; color: var(--ink-faint); margin-top: 1px; }}

  .levels-cell {{ min-width: 176px; }}
  .levels {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; font-variant-numeric: tabular-nums; }}
  .level-row {{ display: flex; align-items: center; gap: 6px; margin-bottom: 3px; white-space: nowrap; }}
  .level-row.stop {{ color: var(--ink-faint); font-size: 11px; }}
  .rr-row {{ font-size: 11px; font-weight: 600; }}
  .rr-row.rr-good {{ color: var(--good); }}
  .rr-row.rr-mid {{ color: var(--warning); }}
  .rr-row.rr-bad {{ color: var(--critical); }}
  .size-row {{ font-size: 11px; color: var(--ink-muted); }}
  .size-row.size-row-none {{ color: var(--critical); font-style: italic; }}
  .insider-chip.earnings-imminent {{ color: var(--critical); background: color-mix(in srgb, var(--critical) 20%, transparent); }}
  .insider-chip.earnings-soon {{ color: var(--warning); background: color-mix(in srgb, var(--warning) 20%, transparent); }}
  .momentum-block {{ margin-top: 4px; }}
  .momentum-row {{ font-size: 10.5px; font-family: 'IBM Plex Mono', monospace; display: flex; align-items: center; gap: 5px; margin-bottom: 2px; }}
  .momentum-row.pnl-good {{ color: var(--good); }}
  .momentum-row.pnl-bad {{ color: var(--critical); }}
  .momentum-row.momentum-muted {{ color: var(--ink-faint); font-style: italic; }}
  .momentum-row.momentum-spark {{ color: var(--ink-muted); }}
  .sparkline {{ display: block; vertical-align: middle; }}
  .level-tag {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 4px;
    min-width: 30px;
    text-align: center;
    flex-shrink: 0;
  }}
  .level-tag-buy {{ color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }}
  .level-tag-sell {{ color: var(--critical); background: color-mix(in srgb, var(--critical) 14%, transparent); }}
  .zone-flag {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 9.5px;
    font-weight: 600;
    padding: 1px 5px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }}
  .zone-flag-buy {{ color: var(--good); background: color-mix(in srgb, var(--good) 16%, transparent); }}
  .zone-flag-sell {{ color: var(--critical); background: color-mix(in srgb, var(--critical) 16%, transparent); }}
  .empty-cell {{ color: var(--ink-faint); }}

  .subscores-cell {{ min-width: 210px; }}
  .subscore {{ margin-bottom: 5px; }}
  .subscore:last-child {{ margin-bottom: 0; }}
  .subscore-label {{
    display: flex;
    justify-content: space-between;
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-bottom: 2px;
  }}
  .subscore-val {{ font-family: 'IBM Plex Mono', monospace; color: var(--ink-muted); }}
  .subscore-track {{
    height: 4px;
    background: var(--seq-blue-track);
    border-radius: 2px;
    overflow: hidden;
  }}
  .subscore-fill {{
    height: 100%;
    background: var(--seq-blue);
    border-radius: 2px;
  }}

  details summary {{
    cursor: pointer;
    font-size: 12.5px;
    color: var(--accent);
    font-weight: 500;
    list-style: none;
  }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::after {{ content: " \\2193"; }}
  details[open] summary::after {{ content: " \\2191"; }}
  ul.rationale {{
    margin: 8px 0 0;
    padding-left: 16px;
    font-size: 12.5px;
    color: var(--ink-muted);
    max-width: 42ch;
  }}
  ul.rationale li {{ margin-bottom: 4px; }}

  .screener-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
  }}
  .pick-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-top: 3px solid var(--pill-color);
    border-radius: 10px;
    padding: 14px;
    box-shadow: var(--shadow);
  }}
  .pick-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
  .pick-ticker {{ font-family: 'IBM Plex Mono', monospace; font-weight: 600; }}
  .pick-company {{ font-size: 12px; color: var(--ink-muted); margin-bottom: 10px; }}
  .pick-score {{ font-family: 'IBM Plex Mono', monospace; font-size: 18px; font-weight: 600; }}
  .pick-score-label {{ font-family: 'IBM Plex Sans', sans-serif; font-size: 10.5px; color: var(--ink-faint); font-weight: 400; }}
  .pick-buy {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--good); font-variant-numeric: tabular-nums; }}

  .empty-note, .pending-note {{
    font-size: 13px;
    color: var(--ink-faint);
    padding: 14px 16px;
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 10px;
  }}
  .pending-note {{ margin-top: 10px; }}

  .pnl-good {{ color: var(--good); }}
  .pnl-bad {{ color: var(--critical); }}
  .summary-chip.pnl-good b {{ color: var(--good); }}
  .summary-chip.pnl-bad b {{ color: var(--critical); }}

  .trade-form {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
    box-shadow: var(--shadow);
  }}
  .trade-form input, .trade-form select {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2);
    color: var(--ink);
  }}
  .trade-form input[name="ticker"] {{ width: 110px; }}
  .trade-form input[name="shares"], .trade-form input[name="price"] {{ width: 100px; }}
  .trade-form input[name="note"] {{ flex: 1; min-width: 120px; }}
  .trade-form button {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    background: var(--accent);
    color: var(--surface);
    cursor: pointer;
  }}
  .trade-form button:hover {{ opacity: 0.9; }}
  .trade-row-actions button {{
    font-size: 10.5px;
    padding: 2px 7px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--ink-muted);
    cursor: pointer;
    margin-top: 4px;
  }}

  footer {{
    border-top: 1px solid var(--border);
    padding-top: 20px;
    font-size: 12px;
    color: var(--ink-faint);
    display: flex;
    flex-wrap: wrap;
    gap: 8px 20px;
    justify-content: space-between;
  }}
  footer .disclaimer {{ max-width: 62ch; }}

  @media (max-width: 880px) {{
    .shell {{ flex-direction: column; }}
    .sidebar {{
      width: 100%;
      position: static;
      flex-direction: row;
      align-items: center;
      gap: 16px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }}
    .side-nav {{
      flex-direction: row;
      overflow-x: auto;
      scrollbar-width: none;
      gap: 2px;
      flex: 1;
    }}
    .side-nav::-webkit-scrollbar {{ display: none; }}
    .nav-btn {{
      border-left: none;
      border-bottom: 2px solid transparent;
      border-radius: 6px 6px 0 0;
      flex-shrink: 0;
    }}
    .nav-btn.is-active {{ border-left-color: transparent; border-bottom-color: var(--accent); }}
    .sidebar-status {{ display: none; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 40 40" width="21" height="21">
            <circle cx="20" cy="8.5" r="1.7" fill="currentColor"/>
            <rect x="18.7" y="9.8" width="2.6" height="3.4" rx="1.2" fill="currentColor"/>
            <path d="M20 13c-5.4 0-9.2 4.3-9.2 9.6v3.6l-2.6 3.9c-.7 1 .1 2.4 1.3 2.4h21c1.2 0 2-1.4 1.3-2.4l-2.6-3.9v-3.6c0-5.3-3.8-9.6-9.2-9.6Z" fill="currentColor"/>
            <path d="M15.6 32.4a4.6 4.6 0 0 0 8.8 0" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" fill="none"/>
          </svg>
        </div>
        <div class="brand-text">
          <h1>Bellwether</h1>
          <span class="eyebrow">Signal desk</span>
        </div>
      </div>

      <nav class="side-nav" role="tablist" aria-label="Sections">
        <button class="nav-btn is-active" data-tab="overview" role="tab" aria-selected="true">Home</button>
        <button class="nav-btn" data-tab="daytrade" role="tab" aria-selected="false">Day Trade</button>
        <button class="nav-btn" data-tab="investing" role="tab" aria-selected="false">Investing</button>
        <button class="nav-btn" data-tab="portfolio" role="tab" aria-selected="false">Portfolio</button>
        <button class="nav-btn" data-tab="trackrecord" role="tab" aria-selected="false">Track Record</button>
      </nav>

      <div class="sidebar-status">
        <div class="updated"><span class="live-dot"></span>Updated {esc(updated_str)}</div>
        <div>{len(results)} tracked · {len(discovered)} screened</div>
        <div>{len(screener)} screener picks</div>
      </div>
    </aside>

    <main class="main">

      <div class="tab-panel is-active" data-panel="overview">
        <p class="home-tagline">Rules-based signals across technicals, fundamentals, news, and insider activity — with
          buy/sell price zones, day-trade and investing checklists, your own portfolio, and a graded track record.
          Refreshed hourly through market hours.</p>

        {highlights_html}

        <div class="summary-strip">
          <div class="summary-chip buy"><b>{buy_count}</b> BUY</div>
          <div class="summary-chip"><b>{hold_count}</b> HOLD</div>
          <div class="summary-chip sell"><b>{sell_count}</b> SELL</div>
        </div>

        <section>
          {sector_mix}
        </section>

        <section>
          <h2 class="section-title">Watchlist</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th></th>
                  <th>Ticker</th>
                  <th>Price</th>
                  <th>Signal</th>
                  <th>Score</th>
                  <th>Buy / sell price</th>
                  <th>Breakdown</th>
                  <th>Rationale</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
          {pending_html}
        </section>
      </div>

      <div class="tab-panel" data-panel="daytrade">
        <section>
          {day_trade_html}
        </section>
        <section>
          {day_trade_track_record_html_}
        </section>
      </div>

      <div class="tab-panel" data-panel="investing">
        <section>
          {investing_html}
        </section>
        <section>
          <h2 class="section-title">Screener picks</h2>
          <p class="tab-blurb">Beyond the core watchlist — other quality large-caps whose numbers currently earn a strong composite score.</p>
          <div class="screener-grid">
            {screener_html}
          </div>
        </section>
      </div>

      <div class="tab-panel" data-panel="portfolio">
        <section id="portfolio-tab-root">
          {portfolio_html}
        </section>
      </div>

      <div class="tab-panel" data-panel="trackrecord">
        <section>
          {track_record_html}
        </section>
      </div>

      <footer>
        <div class="disclaimer">{esc(market_note) if market_note else ''} This is a research tool built from public web data and a rules-based scoring model — not financial advice. Verify anything before trading on it.</div>
        <div>Next refresh: top of the hour, market hours, weekdays.</div>
      </footer>

    </main>
  </div>

</div>
<script>
  (function() {{
    var buttons = document.querySelectorAll('.nav-btn');
    var panels = document.querySelectorAll('.tab-panel');
    buttons.forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var target = btn.getAttribute('data-tab');
        buttons.forEach(function(b) {{
          b.classList.toggle('is-active', b === btn);
          b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        }});
        panels.forEach(function(p) {{
          p.classList.toggle('is-active', p.getAttribute('data-panel') === target);
        }});
      }});
    }});
  }})();
</script>
<script>
  (function() {{
    var seedEl = document.getElementById('portfolio-seed');
    if (!seedEl) return;
    var priceEl = document.getElementById('portfolio-prices');
    var levelsEl = document.getElementById('portfolio-levels');
    var sectorEl = document.getElementById('portfolio-sectors');
    var prices = JSON.parse((priceEl && priceEl.textContent) || '{{}}');
    var levels = JSON.parse((levelsEl && levelsEl.textContent) || '{{}}');
    var sectorLookup = JSON.parse((sectorEl && sectorEl.textContent) || '{{}}');
    var seed = JSON.parse(seedEl.textContent || '{{}}');
    var state = {{ trades: seed.trades || [] }};

    function money(v) {{
      if (v === null || v === undefined) return '—';
      var sign = v < 0 ? '-' : '';
      return sign + '$' + Math.abs(v).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}
    function pct(v) {{
      if (v === null || v === undefined) return '—';
      return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
    }}
    function pnlClass(v) {{
      if (v === null || v === undefined) return '';
      return v > 0 ? 'pnl-good' : (v < 0 ? 'pnl-bad' : '');
    }}
    function esc(s) {{
      return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    function computePortfolio(trades) {{
      var byTicker = {{}};
      trades.forEach(function(t) {{
        var tk = (t.ticker || '').toUpperCase();
        (byTicker[tk] = byTicker[tk] || []).push(t);
      }});
      var openPositions = [], closedLots = [], warnings = [], totalRealized = 0;
      Object.keys(byTicker).forEach(function(ticker) {{
        var tks = byTicker[ticker].slice().sort(function(a, b) {{
          return (a.date || '').localeCompare(b.date || '') || String(a.id || '').localeCompare(String(b.id || ''));
        }});
        var lots = [];
        tks.forEach(function(t) {{
          var shares = parseFloat(t.shares) || 0, price = parseFloat(t.price) || 0, side = (t.side || '').toLowerCase();
          if (shares <= 0 || price <= 0) {{ warnings.push('Skipped a ' + ticker + ' trade with missing/invalid shares or price.'); return; }}
          if (side === 'buy') {{
            lots.push([shares, price, t.date]);
          }} else if (side === 'sell') {{
            var remaining = shares;
            while (remaining > 1e-9 && lots.length) {{
              var lot = lots[0], matched = Math.min(lot[0], remaining);
              var realized = (price - lot[1]) * matched;
              totalRealized += realized;
              closedLots.push({{ticker: ticker, shares: matched, buy_price: lot[1], sell_price: price, sell_date: t.date,
                realized_pnl: realized, realized_pnl_pct: lot[1] ? (price - lot[1]) / lot[1] * 100 : null}});
              lot[0] -= matched; remaining -= matched;
              if (lot[0] <= 1e-9) lots.shift();
            }}
            if (remaining > 1e-9) warnings.push('Sold ' + remaining + ' more ' + ticker + ' shares than were on record — ignored the excess.');
          }} else {{
            warnings.push('Skipped a ' + ticker + ' trade with an unrecognized side.');
          }}
        }});
        var remShares = lots.reduce(function(s, l) {{ return s + l[0]; }}, 0);
        if (remShares > 1e-9) {{
          var costBasis = lots.reduce(function(s, l) {{ return s + l[0] * l[1]; }}, 0);
          var avgCost = costBasis / remShares;
          var curPrice = (ticker in prices) ? prices[ticker] : null;
          var unreal = curPrice !== null ? (curPrice - avgCost) * remShares : null;
          var unrealPct = curPrice !== null ? (curPrice - avgCost) / avgCost * 100 : null;
          var lv = levels[ticker], zoneFlag = null;
          if (lv && curPrice !== null) {{
            if (lv.at_exit_target) zoneFlag = 'exit';
            else if (lv.in_entry_zone) zoneFlag = 'entry';
          }}
          openPositions.push({{ticker: ticker, shares: remShares, avg_cost: avgCost, current_price: curPrice,
            tracked: curPrice !== null, unrealized_pnl: unreal, unrealized_pnl_pct: unrealPct, zone_flag: zoneFlag}});
        }}
      }});
      openPositions.sort(function(a, b) {{ return (b.unrealized_pnl || 0) - (a.unrealized_pnl || 0); }});
      closedLots.sort(function(a, b) {{ return (b.sell_date || '').localeCompare(a.sell_date || ''); }});
      var trackedPos = openPositions.filter(function(p) {{ return p.tracked; }});
      var trackedCost = trackedPos.reduce(function(s, p) {{ return s + p.avg_cost * p.shares; }}, 0);
      var trackedVal = trackedPos.reduce(function(s, p) {{ return s + p.current_price * p.shares; }}, 0);
      var unrealTotal = openPositions.reduce(function(s, p) {{ return s + (p.unrealized_pnl || 0); }}, 0);
      return {{
        open: openPositions, closed: closedLots, warnings: warnings,
        totals: {{
          tracked_cost_basis: trackedCost, tracked_market_value: trackedVal, unrealized_pnl: unrealTotal,
          unrealized_pnl_pct: trackedCost ? unrealTotal / trackedCost * 100 : null, realized_pnl: totalRealized
        }}
      }};
    }}

    function sectorBreakdown(openPositions) {{
      var totals = {{}}, grandTotal = 0;
      openPositions.forEach(function(p) {{
        if (!p.tracked) return;
        var value = p.current_price * p.shares;
        var sector = sectorLookup[p.ticker] || 'Other / Unclassified';
        totals[sector] = (totals[sector] || 0) + value;
        grandTotal += value;
      }});
      if (grandTotal <= 0) return {{breakdown: [], rating: null, top_sector: null, top_pct: null}};
      var breakdown = Object.keys(totals).map(function(s) {{
        return {{sector: s, value: totals[s], pct: totals[s] / grandTotal * 100}};
      }}).sort(function(a, b) {{ return b.value - a.value; }});
      var top = breakdown[0];
      var rating = top.pct >= 45 ? 'Concentrated' : (top.pct >= 30 ? 'Leaning heavy' : 'Well diversified');
      return {{breakdown: breakdown, rating: rating, top_sector: top.sector, top_pct: top.pct}};
    }}
    function sectorTone(rating) {{
      return rating === 'Concentrated' ? 'critical' : (rating === 'Leaning heavy' ? 'warning' : 'good');
    }}

    function render() {{
      var c = computePortfolio(state.trades);
      var summary = document.getElementById('portfolio-summary');
      if (summary) {{
        summary.innerHTML =
          '<div class="summary-chip"><b>' + money(c.totals.tracked_cost_basis) + '</b>&nbsp;cost basis</div>' +
          '<div class="summary-chip"><b>' + money(c.totals.tracked_market_value) + '</b>&nbsp;market value</div>' +
          '<div class="summary-chip ' + pnlClass(c.totals.unrealized_pnl) + '"><b>' + money(c.totals.unrealized_pnl) + '</b>&nbsp;unrealized (' + pct(c.totals.unrealized_pnl_pct) + ')</div>' +
          '<div class="summary-chip ' + pnlClass(c.totals.realized_pnl) + '"><b>' + money(c.totals.realized_pnl) + '</b>&nbsp;realized (all-time)</div>';
      }}
      var openBody = document.getElementById('portfolio-open-body');
      if (openBody) {{
        var trackedVal = c.totals.tracked_market_value;
        var driftBadge = function(p) {{
          if (!p.tracked || !trackedVal) return '';
          var weightPct = p.current_price * p.shares / trackedVal * 100;
          if (weightPct >= 20) {{
            return '<div class="insider-chip earnings-imminent" title="' + weightPct.toFixed(1) + '% of the tracked portfolio — at/above the 20% single-position guideline, worth trimming back toward target.">⚠ Trim — overweight (' + weightPct.toFixed(1) + '%)</div>';
          }} else if (weightPct >= 15) {{
            return '<div class="insider-chip earnings-soon" title="' + weightPct.toFixed(1) + '% of the tracked portfolio — approaching the 20% single-position guideline.">⚠ Watch — approaching target weight (' + weightPct.toFixed(1) + '%)</div>';
          }}
          return '';
        }};
        openBody.innerHTML = c.open.length ? c.open.map(function(p) {{
          return '<tr><td class="ticker-cell"><div class="ticker">' + esc(p.ticker) + '</div>' +
            (!p.tracked ? '<div class="source-tag">not tracked</div>' : '') +
            (p.zone_flag ? '<div class="insider-chip insider-buy">In ' + esc(p.zone_flag) + ' zone</div>' : '') +
            driftBadge(p) + '</td>' +
            '<td class="price-cell">' + p.shares + '</td>' +
            '<td class="price-cell">$' + p.avg_cost.toFixed(2) + '</td>' +
            '<td class="price-cell">' + (p.current_price !== null ? '$' + p.current_price.toFixed(2) : '—') + '</td>' +
            '<td class="price-cell">' + (p.current_price !== null ? money(p.current_price * p.shares) : '—') + '</td>' +
            '<td class="price-cell ' + pnlClass(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + '</td>' +
            '<td class="price-cell ' + pnlClass(p.unrealized_pnl) + '">' + pct(p.unrealized_pnl_pct) + '</td></tr>';
        }}).join('') : '<tr><td colspan="7" class="empty-cell">No open positions yet — add a buy below.</td></tr>';
      }}
      var closedBody = document.getElementById('portfolio-closed-body');
      if (closedBody) {{
        closedBody.innerHTML = c.closed.length ? c.closed.slice(0, 20).map(function(x) {{
          return '<tr><td class="ticker-cell"><div class="ticker">' + esc(x.ticker) + '</div></td>' +
            '<td class="price-cell">' + x.shares + '</td>' +
            '<td class="price-cell">$' + x.buy_price.toFixed(2) + '</td>' +
            '<td class="price-cell">$' + x.sell_price.toFixed(2) + '</td>' +
            '<td class="price-cell">' + esc(x.sell_date || '—') + '</td>' +
            '<td class="price-cell ' + pnlClass(x.realized_pnl) + '">' + money(x.realized_pnl) + '</td>' +
            '<td class="price-cell ' + pnlClass(x.realized_pnl) + '">' + pct(x.realized_pnl_pct) + '</td></tr>';
        }}).join('') : '<tr><td colspan="7" class="empty-cell">No closed trades yet.</td></tr>';
      }}
      var sectorEl2 = document.getElementById('portfolio-sector-mix');
      if (sectorEl2) {{
        var sb = sectorBreakdown(c.open);
        sectorEl2.className = sb.breakdown.length ? 'sector-mix' : 'empty-note';
        sectorEl2.innerHTML = sb.breakdown.length ? sb.breakdown.map(function(b) {{
          return '<div class="sector-row"><div class="sector-label">' + esc(b.sector) +
            '<span class="sector-pct">' + b.pct.toFixed(0) + '%</span></div>' +
            '<div class="subscore-track"><div class="subscore-fill" style="width:' + b.pct.toFixed(1) + '%"></div></div></div>';
        }}).join('') : 'Add an open position to see how it\\'s spread across sectors.';
        var ratingEl = document.getElementById('portfolio-sector-rating');
        if (ratingEl && sb.rating) {{
          ratingEl.innerHTML = '<span class="pill" style="--pill-color:var(--' + sectorTone(sb.rating) + ')">' + esc(sb.rating) + '</span> ' +
            esc(sb.top_sector) + ' makes up ' + sb.top_pct.toFixed(0) + '% of your tracked position value.';
        }}
      }}
    }}

    // Durable, per-browser store: trades added on this page live in this
    // browser's localStorage, keyed to this artifact's own origin -- they
    // are NOT touched by the hourly server-side republish (that only ever
    // rewrites the page's markup, never a viewer's local storage), so once
    // saved here they stay put indefinitely on this device. They combine
    // with whatever Claude has logged from chat (embedded in the page each
    // refresh) so both sources show up together; a trade reported through
    // both channels for the same fill will double-count, so pick one path
    // per trade.
    var LS_KEY = 'signal-ledger-portfolio-trades-v1';
    var statusEl = document.getElementById('trade-form-status');

    function loadLocalTrades() {{
      try {{
        var raw = window.localStorage.getItem(LS_KEY);
        var parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
      }} catch (e) {{
        return null; // storage unavailable (private mode, blocked, etc.)
      }}
    }}
    function saveLocalTrades(trades) {{
      try {{
        window.localStorage.setItem(LS_KEY, JSON.stringify(trades));
        return true;
      }} catch (e) {{
        return false;
      }}
    }}

    var localTrades = loadLocalTrades();
    var storageOk = localTrades !== null;
    if (storageOk && localTrades.length) {{
      state.trades = state.trades.concat(localTrades);
      render();
    }}
    if (!storageOk && statusEl) {{
      statusEl.textContent = 'This browser is blocking local storage (private window, or site data disabled), so trades added here won\\'t be saved after you leave. Tell Claude in chat instead so they stick.';
    }}

    var form = document.getElementById('trade-form');
    if (form) {{
      form.addEventListener('submit', function(e) {{
        e.preventDefault();
        var fd = new FormData(form);
        var trade = {{
          id: 't' + Date.now() + Math.floor(Math.random() * 1000),
          ticker: (fd.get('ticker') || '').toUpperCase().trim(),
          side: fd.get('side'),
          shares: fd.get('shares'),
          price: fd.get('price'),
          date: fd.get('date'),
          note: fd.get('note') || ''
        }};
        if (!trade.ticker || !trade.shares || !trade.price || !trade.date) return;
        state.trades.push(trade);
        render();
        if (storageOk) {{
          localTrades = (localTrades || []).concat([trade]);
          var ok = saveLocalTrades(localTrades);
          if (statusEl) statusEl.textContent = ok
            ? 'Saved — this trade will still be here after the hourly refresh, on this browser.'
            : 'Could not save to this browser\\'s storage. It shows for now, but tell Claude in chat too so it sticks.';
        }}
        form.reset();
      }});
    }}
  }})();
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    return OUT_PATH


if __name__ == "__main__":
    path = generate_html()
    print(f"Wrote {path}")
