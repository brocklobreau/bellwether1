"""
Renders results/latest.json into the dashboard HTML page.
No network calls. Pure templating.
"""
import json
import os
from datetime import datetime, timezone

from lib.portfolio import load_portfolio, compute_portfolio
from lib.track_record import (
    build_track_record, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    RATCHET_STEPS, SCORE_DROP_EXIT,
)
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
      <td class="price-cell" data-live-price="{esc(r.get('ticker'))}">{'$' + format(price, ',.2f') if price is not None else '—'}</td>
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

VALUE_RATING_COLOR = {
    "Deep value + quality": "var(--good)",
    "Undervalued, worth the work": "var(--accent)",
    "Mildly cheap": "var(--warning)",
    "Not compelling": "var(--critical)",
}


def value_card(r):
    """One deep-value pick. Leads with the estimated re-rating upside because
    that is the number the screen exists to produce, but keeps the arithmetic
    behind it visible in the notes rather than presenting it as an oracle."""
    u = r.get("undervalued") or {}
    rating = u.get("undervalued_rating") or "—"
    color = VALUE_RATING_COLOR.get(rating, "var(--ink-muted)")
    upside = u.get("rerating_upside_pct")
    upside_str = f"{upside:+.0f}%" if upside is not None else "—"
    levels = r.get("investing_levels") or {}
    buy_str = fmt_zone(levels.get("entry_zone"))
    fund = r.get("fundamental") or {}
    tech = r.get("technical") or {}

    pe = fund.get("pe_ratio")
    rp = tech.get("range_position_pct")
    facts = []
    if pe is not None:
        facts.append(f"P/E {pe:.1f}")
    if fund.get("revenue_growth_pct") is not None:
        facts.append(f"rev {fund['revenue_growth_pct']:+.0f}%")
    if fund.get("profit_margin_pct") is not None:
        facts.append(f"margin {fund['profit_margin_pct']:.0f}%")
    if rp is not None:
        facts.append(f"{rp:.0f}% of 52w range")
    facts_str = " · ".join(facts)

    notes_html = "\n".join(f"<li>{esc(n)}</li>" for n in (u.get("notes") or [])[:6])
    sector_str = f" · {esc(r.get('sector'))}" if r.get("sector") else ""

    # Trading-at vs worth-about, side by side. The percentage above is the
    # same arithmetic; people read dollars faster than they read percentages,
    # and seeing both makes the claim concrete enough to argue with.
    cur_price = u.get("current_price") if u.get("current_price") is not None else r.get("price")
    fair_price = u.get("fair_value_price")
    price_block = ""
    if cur_price is not None and fair_price is not None:
        cur_pe, fair_pe = u.get("current_pe"), u.get("fair_pe")
        pe_line = (f"{cur_pe:.1f}x &rarr; {fair_pe:.1f}x earnings"
                   if cur_pe is not None and fair_pe is not None else "")
        price_block = f"""<div class="value-prices">
        <div class="value-price-row">
          <span class="value-price-label">Trading at</span>
          <span class="value-price-num">${cur_price:,.2f}</span>
        </div>
        <div class="value-price-row value-price-fair">
          <span class="value-price-label">Worth about</span>
          <span class="value-price-num">${fair_price:,.2f}</span>
        </div>
        <div class="value-price-pe">{pe_line}</div>
      </div>"""
    elif cur_price is not None:
        price_block = f"""<div class="value-prices">
        <div class="value-price-row">
          <span class="value-price-label">Trading at</span>
          <span class="value-price-num">${cur_price:,.2f}</span>
        </div>
        <div class="value-price-pe">fair value not computable</div>
      </div>"""

    return f"""<div class="pick-card value-card" style="--pill-color:{color}">
      <div class="pick-top">
        <span class="pick-ticker">{esc(r.get('ticker'))}</span>
        <span class="pill" style="--pill-color:{color}">{esc(rating)}</span>
      </div>
      <div class="pick-company">{esc(r.get('name',''))}{sector_str}</div>
      <div class="value-headline">
        <div class="value-upside">{upside_str}<span class="value-upside-label">est. upside to fair value</span></div>
        <div class="value-score">{u.get('undervalued_score','—')}<span class="pick-score-label">value score</span></div>
      </div>
      {price_block}
      <div class="value-facts">{esc(facts_str)}</div>
      <ul class="rationale value-notes">{notes_html}</ul>
      <div class="pick-buy">Buy zone: {buy_str}</div>
    </div>"""


def equity_curve_svg(curve, start_equity, width=760, height=140):
    """Inline SVG sparkline of the bot's equity. No chart library -- this
    page is a single self-contained file and stays that way. Draws the
    starting-equity line so above/below the line is readable at a glance."""
    pts = [p.get("equity") for p in (curve or []) if p.get("equity") is not None]
    if len(pts) < 2:
        return ('<div class="empty-note">Not enough history to plot yet — the curve appears '
                'once the bot has run a few cycles.</div>')

    lo, hi = min(pts + [start_equity]), max(pts + [start_equity])
    span = (hi - lo) or 1.0
    pad = span * 0.08
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    def x(i): return round(i / (len(pts) - 1) * width, 2)
    def y(v): return round(height - (v - lo) / span * height, 2)

    line = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(pts))
    area = f"0,{height} " + line + f" {width},{height}"
    base_y = y(start_equity)
    up = pts[-1] >= start_equity
    stroke = "var(--good)" if up else "var(--critical)"

    return f"""<div class="equity-chart">
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img"
           aria-label="Bot equity curve">
        <polygon points="{area}" fill="{stroke}" opacity="0.10"/>
        <line x1="0" y1="{base_y}" x2="{width}" y2="{base_y}"
              stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="4 4" opacity="0.6"/>
        <polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round"/>
      </svg>
      <div class="equity-axis">
        <span>${lo:,.0f}</span>
        <span class="equity-axis-mid">dashed line = ${start_equity:,.0f} starting equity</span>
        <span>${hi:,.0f}</span>
      </div>
    </div>"""


BOT_CHART_JS = r"""<script>
(function () {
  var host = document.getElementById('bot-chart');
  if (!host) return;
  var dataEl = document.getElementById('bot-curve-data');
  if (!dataEl) return;
  var raw;
  try { raw = JSON.parse(dataEl.textContent || '[]'); } catch (e) { return; }
  if (!raw || raw.length < 2) return;

  var START = parseFloat(host.getAttribute('data-start')) || 0;
  var svg = host.querySelector('svg');
  var gArea = host.querySelector('.eq-area');
  var gLine = host.querySelector('.eq-line');
  var gBase = host.querySelector('.eq-base');
  var gHair = host.querySelector('.eq-hair');
  var gDot = host.querySelector('.eq-dot');
  var tip = host.querySelector('.eq-tip');
  var loEl = host.querySelector('.eq-lo');
  var hiEl = host.querySelector('.eq-hi');
  var sumEl = host.querySelector('.eq-summary');
  var midEl = host.querySelector('.eq-mid');
  var btns = host.querySelectorAll('.eq-range button');
  var W = 760, H = 140;

  // Distinct trading sessions, not calendar days: the market shuts on
  // weekends and holidays, so "1 day" has to mean the last session that
  // actually happened, or the shortest range is empty every Saturday.
  var sessions = [];
  for (var i = 0; i < raw.length; i++) {
    var d = String(raw[i].t).slice(0, 10);
    if (sessions[sessions.length - 1] !== d) sessions.push(d);
  }

  function slice(nSessions) {
    if (!nSessions || nSessions >= sessions.length) return raw.slice();
    var cut = sessions[sessions.length - nSessions];
    return raw.filter(function (p) { return String(p.t).slice(0, 10) >= cut; });
  }

  var view = [], xs = [], ys = [];

  function money(v) {
    return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  }
  function stamp(t) {
    var dt = new Date(t);
    if (isNaN(dt.getTime())) return String(t);
    return dt.toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    });
  }

  function draw(pts) {
    view = pts;
    var vals = pts.map(function (p) { return p.e; });
    var vmin = Math.min.apply(null, vals), vmax = Math.max.apply(null, vals);
    // Only pull the starting-equity line into scale when it is actually near
    // the data. Forcing it into a one-day view squashes the curve into a
    // flat line, which is the usual way a sparkline lies.
    var spread = vmax - vmin;
    var lo = vmin, hi = vmax;
    if (spread <= 0 || Math.abs(START - vals[vals.length - 1]) < Math.max(spread * 6, 1)) {
      lo = Math.min(lo, START); hi = Math.max(hi, START);
    }
    var span = (hi - lo) || Math.max(1, Math.abs(lo) * 0.001);
    lo -= span * 0.10; hi += span * 0.10; span = hi - lo;

    xs = []; ys = [];
    for (var i = 0; i < pts.length; i++) {
      xs.push(pts.length === 1 ? W / 2 : (i / (pts.length - 1)) * W);
      ys.push(H - ((pts[i].e - lo) / span) * H);
    }
    var line = '';
    for (var j = 0; j < xs.length; j++) {
      line += (j ? ' ' : '') + xs[j].toFixed(2) + ',' + ys[j].toFixed(2);
    }

    var first = vals[0], last = vals[vals.length - 1];
    var stroke = last >= first ? 'var(--good)' : 'var(--critical)';

    gLine.setAttribute('points', line);
    gLine.setAttribute('stroke', stroke);
    gArea.setAttribute('points', '0,' + H + ' ' + line + ' ' + W + ',' + H);
    gArea.setAttribute('fill', stroke);

    var by = H - ((START - lo) / span) * H;
    if (by >= 0 && by <= H) {
      gBase.setAttribute('y1', by); gBase.setAttribute('y2', by);
      gBase.style.display = '';
      if (midEl) midEl.textContent = midEl.getAttribute('data-base');
    } else {
      // Off-scale in a short range. Hide the caption too -- a legend for a
      // line that is not on the chart is just wrong.
      gBase.style.display = 'none';
      if (midEl) midEl.textContent = 'starting equity is off this range';
    }

    loEl.textContent = money(lo);
    hiEl.textContent = money(hi);

    var chg = first ? ((last - first) / Math.abs(first)) * 100 : 0;
    var sign = chg >= 0 ? '+' : '';
    sumEl.textContent = money(first) + ' → ' + money(last) + '  (' + sign + chg.toFixed(2) + '%)';
    sumEl.className = 'eq-summary ' + (chg >= 0 ? 'pnl-good' : 'pnl-bad');
    svg.setAttribute('aria-label',
      'Bot equity over ' + pts.length + ' points, ' + money(first) + ' to ' + money(last)
      + ', ' + sign + chg.toFixed(2) + ' percent. Arrow keys read individual points.');
    hideTip();
  }

  function nearest(clientX) {
    var r = svg.getBoundingClientRect();
    if (!r.width) return -1;
    var vx = ((clientX - r.left) / r.width) * W;
    var best = 0, bd = Infinity;
    for (var i = 0; i < xs.length; i++) {
      var d = Math.abs(xs[i] - vx);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function showAt(i) {
    if (i < 0 || i >= view.length) return;
    gHair.setAttribute('x1', xs[i]); gHair.setAttribute('x2', xs[i]);
    gHair.style.display = '';
    gDot.setAttribute('cx', xs[i]); gDot.setAttribute('cy', ys[i]);
    gDot.style.display = '';
    // textContent, never innerHTML -- these strings are data.
    tip.querySelector('.eq-tip-val').textContent = money(view[i].e);
    tip.querySelector('.eq-tip-when').textContent = stamp(view[i].t);
    var delta = view[i].e - view[0].e;
    var dEl = tip.querySelector('.eq-tip-delta');
    dEl.textContent = (delta >= 0 ? '+' : '−') + money(Math.abs(delta)) + ' in range';
    dEl.className = 'eq-tip-delta ' + (delta >= 0 ? 'pnl-good' : 'pnl-bad');
    var pct = (xs[i] / W) * 100;
    tip.style.left = pct + '%';
    tip.style.transform = 'translateX(' + (pct > 70 ? '-100%' : (pct < 30 ? '0%' : '-50%')) + ')';
    tip.style.display = 'flex';   // 'block' would override the CSS column layout
    host.setAttribute('data-idx', i);
  }

  function hideTip() {
    tip.style.display = 'none';
    gHair.style.display = 'none';
    gDot.style.display = 'none';
    host.removeAttribute('data-idx');
  }

  var plot = host.querySelector('.eq-plot');
  plot.addEventListener('pointermove', function (ev) { showAt(nearest(ev.clientX)); });
  plot.addEventListener('pointerleave', hideTip);
  plot.addEventListener('pointerdown', function (ev) { showAt(nearest(ev.clientX)); });

  // Keyboard parity: the same readout without a pointer.
  svg.addEventListener('keydown', function (ev) {
    var cur = parseInt(host.getAttribute('data-idx'), 10);
    if (isNaN(cur)) cur = view.length - 1;
    if (ev.key === 'ArrowLeft') { showAt(Math.max(0, cur - 1)); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight') { showAt(Math.min(view.length - 1, cur + 1)); ev.preventDefault(); }
    else if (ev.key === 'Home') { showAt(0); ev.preventDefault(); }
    else if (ev.key === 'End') { showAt(view.length - 1); ev.preventDefault(); }
    else if (ev.key === 'Escape') { hideTip(); }
  });
  svg.addEventListener('blur', hideTip);

  function pick(btn) {
    var n = parseInt(btn.getAttribute('data-sessions'), 10) || 0;
    for (var k = 0; k < btns.length; k++) {
      btns[k].classList.remove('is-on');
      btns[k].setAttribute('aria-pressed', 'false');
    }
    btn.classList.add('is-on');
    btn.setAttribute('aria-pressed', 'true');
    var pts = slice(n);
    if (pts.length < 2) {
      // Say so rather than draw a flat line, which reads as "the bot did nothing".
      sumEl.textContent = 'Not enough history for this range yet.';
      sumEl.className = 'eq-summary';
      return;
    }
    draw(pts);
  }

  for (var b = 0; b < btns.length; b++) {
    btns[b].addEventListener('click', function (ev) { pick(ev.currentTarget); });
  }

  // Open on the shortest range that actually has data behind it.
  var startBtn = null;
  for (var q = 0; q < btns.length; q++) {
    var n = parseInt(btns[q].getAttribute('data-sessions'), 10) || 0;
    if (slice(n).length >= 2) { startBtn = btns[q]; break; }
  }
  pick(startBtn || btns[btns.length - 1]);
})();
</script>"""


def bot_equity_chart_html(curve, start_equity):
    """Interactive version of the equity sparkline: range presets plus a
    crosshair readout. Drawn client-side because the range buttons have to
    rescale the axis, and a server-rendered SVG can only ever show one
    window. Still no chart library -- the page stays one self-contained file."""
    pts = [p for p in (curve or [])
           if p.get("equity") is not None and p.get("ts")]
    if len(pts) < 2:
        return ('<div class="empty-note">Not enough history to plot yet &mdash; the curve appears '
                'once the bot has run a few cycles.</div>')

    data = esc_json([{"t": p["ts"], "e": round(float(p["equity"]), 2)} for p in pts])
    # Sessions, not calendar days -- see the JS. 1W = 5 sessions, 1M = 21.
    ranges = (("1D", 1), ("3D", 3), ("1W", 5), ("1M", 21), ("3M", 63), ("All", 0))
    buttons = "".join(
        f'<button type="button" data-sessions="{n}" aria-pressed="false">{lbl}</button>'
        for lbl, n in ranges)

    return f"""<div class="equity-chart" id="bot-chart" data-start="{start_equity}">
      <div class="eq-head">
        <div class="eq-range" role="group" aria-label="Chart time range">{buttons}</div>
        <span class="eq-summary"></span>
      </div>
      <div class="eq-plot">
        <svg viewBox="0 0 760 140" preserveAspectRatio="none" role="img" tabindex="0"
             aria-label="Bot equity curve">
          <polygon class="eq-area" points="" fill="var(--good)" opacity="0.10"/>
          <line class="eq-base" x1="0" y1="0" x2="760" y2="0" stroke="var(--ink-faint)"
                stroke-width="1" stroke-dasharray="4 4" opacity="0.6"/>
          <line class="eq-hair" x1="0" y1="0" x2="0" y2="140" stroke="var(--ink-faint)"
                stroke-width="1" opacity="0.8" style="display:none"
                vector-effect="non-scaling-stroke"/>
          <polyline class="eq-line" points="" fill="none" stroke="var(--good)" stroke-width="2"
                    stroke-linejoin="round" stroke-linecap="round"
                    vector-effect="non-scaling-stroke"/>
          <circle class="eq-dot" r="4" fill="var(--surface)" stroke="var(--ink)"
                  stroke-width="2" style="display:none" vector-effect="non-scaling-stroke"/>
        </svg>
        <div class="eq-tip" style="display:none">
          <span class="eq-tip-val"></span>
          <span class="eq-tip-when"></span>
          <span class="eq-tip-delta"></span>
        </div>
      </div>
      <div class="equity-axis">
        <span class="eq-lo"></span>
        <span class="equity-axis-mid eq-mid"
              data-base="dashed line = ${start_equity:,.0f} starting equity">dashed line = ${start_equity:,.0f} starting equity</span>
        <span class="eq-hi"></span>
      </div>
      <script type="application/json" id="bot-curve-data">{data}</script>
    </div>{BOT_CHART_JS}"""



def bot_tab_html(botdata):
    """The paper-trading bot panel: what it holds, what it did, and whether
    it's actually working. Leads with the disclaimer because a page showing
    an equity curve and buy/sell logs looks exactly like a real brokerage
    account, and it is not one."""
    if not botdata:
        return ('<section><h2 class="section-title">Trading bot</h2>'
                '<div class="empty-note">The bot hasn\'t run yet — it starts on the next '
                'refresh cycle during market hours.</div></section>')

    s = botdata.get("summary") or {}
    cfg = botdata.get("config") or {}
    positions = botdata.get("positions") or []
    closed = botdata.get("closed_trades") or []
    actions = botdata.get("actions") or []

    ret = s.get("total_return_pct")
    ret_tone = "pnl-good" if (ret or 0) >= 0 else "pnl-bad"
    exp = s.get("expectancy_pct")

    chart = bot_equity_chart_html(botdata.get("equity_curve"),
                                  s.get("starting_equity") or 100000)

    def pos_row(p):
        up = (p.get("unrealized_pct") or 0) >= 0
        cls = "pnl-good" if up else "pnl-bad"
        why = "; ".join((p.get("entry_reasons") or [])[:3])
        # Make the half-sizing visible. A position that is deliberately
        # smaller looks like a bug unless the page says why it is smaller.
        earn_badge = ""
        if p.get("earnings_sized_down"):
            d = p.get("days_to_earnings")
            earn_badge = (f'<br><span class="pill" style="--pill-color:var(--warning);font-size:9.5px;">'
                          f'half size · earnings {("in " + str(d) + "d") if d is not None else "soon"}</span>')
        strat = p.get("strategy", "")
        strat_color = "var(--accent)" if strat == "INVEST" else "var(--good)"
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(p.get('ticker'))}</div>
            <div class="company">{esc(p.get('name') or '')}</div></td>
        <td><span class="pill" style="--pill-color:{strat_color};font-size:10px;">{esc(strat)}</span></td>
        <td class="price-cell">{p.get('shares'):,} @ ${p.get('entry_price'):,.2f}</td>
        <td class="price-cell" data-live-price="{esc(p.get('ticker'))}">${(p.get('last_price') or 0):,.2f}</td>
        <td class="price-cell {cls}" data-live-pnl="{esc(p.get('ticker'))}"
            data-entry="{p.get('entry_price')}" data-shares="{p.get('shares')}"
            data-last="{p.get('last_price') or p.get('entry_price')}">{pct(p.get('unrealized_pct'))}<br>
            <span style="font-size:10.5px;opacity:0.75">${p.get('unrealized_dollars'):+,.0f}</span></td>
        <td class="price-cell" style="font-size:11px;">
            <span style="color:var(--critical)">${p.get('stop_price'):,.2f}</span> /
            <span style="color:var(--good)">${p.get('target_price'):,.2f}</span><br>
            <span style="font-size:10px;color:var(--ink-faint)">{p.get('entry_rr')}:1 · risk ${p.get('risk_dollars'):,.0f}</span>
            {earn_badge}</td>
        <td style="font-size:11px;color:var(--ink-muted);max-width:34ch;">{esc(why)}</td>
      </tr>"""

    def closed_row(t):
        cls = "pnl-good" if t.get("win") else "pnl-bad"
        tone = {"target hit": "var(--good)", "trailing stop": "var(--good)",
                "stop-loss": "var(--critical)", "signal faded": "var(--warning)",
                "signal reversed": "var(--warning)", "held too long": "var(--ink-muted)"}.get(
                    t.get("exit_label"), "var(--ink-muted)")
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(t.get('ticker'))}</div>
            <div class="company">{esc(t.get('strategy'))}</div></td>
        <td><span class="pill" style="--pill-color:{tone};font-size:10px;">{esc(t.get('exit_label'))}</span></td>
        <td class="price-cell">${t.get('entry_price'):,.2f}</td>
        <td class="price-cell">${t.get('exit_price'):,.2f}</td>
        <td class="price-cell {cls}">{pct(t.get('pnl_pct'))}</td>
        <td class="price-cell {cls}">${t.get('pnl_dollars'):+,.0f}</td>
        <td class="price-cell" style="font-size:11px;color:var(--ink-faint);white-space:nowrap;">
            {esc((t.get('entry_ts') or '')[:10])} → {esc((t.get('exit_ts') or '')[:10])}</td>
      </tr>"""

    pos_html = ("".join(pos_row(p) for p in positions) or
                '<tr><td colspan="7" class="empty-cell">No open positions — nothing currently clears the entry gates.</td></tr>')
    closed_html = ("".join(closed_row(t) for t in closed[:40]) or
                   '<tr><td colspan="7" class="empty-cell">No closed trades yet.</td></tr>')

    action_items = "".join(
        f'<li><span class="act-{esc(a.get("kind"))}">{esc((a.get("kind") or "").upper())}</span> '
        f'<b>{esc(a.get("ticker"))}</b> ×{a.get("shares")} @ ${a.get("price"):,.2f} — '
        f'{esc(a.get("detail"))} <span class="act-ts">{esc((a.get("ts") or "")[:16].replace("T", " "))}</span></li>'
        for a in actions[:15] if a.get("kind") in ("buy", "sell")
    ) or '<li class="act-none">No trades yet.</li>'

    verdict = ""
    n_closed = s.get("closed_count") or 0
    if n_closed:
        good = s.get("profitable")
        verdict = (f"<b>{'Positive' if good else 'Negative'} expectancy</b> at "
                   f"{s.get('expectancy_pct'):+.2f}% per trade over {n_closed} closed "
                   f"trade{'s' if n_closed != 1 else ''}")
        verdict += f", realized reward:risk {s.get('realized_rr')}:1." if s.get("realized_rr") else "."
        if n_closed < 30:
            verdict += " Far too few trades to mean anything yet."

    return f"""
    <section>
      <h2 class="section-title">Trading bot</h2>
      <div class="bot-disclaimer"><b>Paper trading — simulated money.</b> No brokerage is connected and no
        order is ever placed. It started with ${s.get('starting_equity', 0):,.0f} of pretend capital and marks
        itself to market each cycle. It is also <b>forward-tested, not backtested</b>: every decision is made
        from the data available at that moment and written down immediately, so nothing here is fitted after
        the fact — but that also means it has to earn its record in real time, and a handful of trades proves
        nothing either way.</p>
      </div>

      <div class="bot-stat-grid">
        <div class="bot-stat"><span class="bot-stat-num" id="bot-equity"
              data-cash="{s.get('cash', 0)}" data-start="{s.get('starting_equity', 0)}"
              >${s.get('equity', 0):,.0f}</span>
          <span class="bot-stat-label">portfolio value</span></div>
        <div class="bot-stat"><span class="bot-stat-num {ret_tone}" id="bot-return">{pct(ret)}</span>
          <span class="bot-stat-label">total return</span></div>
        <div class="bot-stat"><span class="bot-stat-num">${s.get('cash', 0):,.0f}</span>
          <span class="bot-stat-label">cash</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{s.get('max_drawdown_pct', 0):.1f}%</span>
          <span class="bot-stat-label">max drawdown</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{f"{s.get('hit_rate_pct'):.0f}%" if s.get('hit_rate_pct') is not None else '—'}</span>
          <span class="bot-stat-label">win rate</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{f"{s.get('realized_rr')}:1" if s.get('realized_rr') else '—'}</span>
          <span class="bot-stat-label">realized R:R</span></div>
        <div class="bot-stat"><span class="bot-stat-num {'pnl-good' if (exp or 0) > 0 else ('pnl-bad' if exp is not None else '')}">{pct(exp)}</span>
          <span class="bot-stat-label">expectancy/trade</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{s.get('open_count', 0)} / {s.get('closed_count', 0)}</span>
          <span class="bot-stat-label">open / closed</span></div>
      </div>

      {chart}
      {f'<p class="tab-blurb">{verdict}</p>' if verdict else ''}

      <p class="tab-blurb">How it decides: a position opens only if it clears the score gate
        <b>and</b> offers at least <b>{cfg.get('min_entry_rr')}:1</b> reward-to-risk. Size is set so that being
        stopped out costs exactly <b>{cfg.get('risk_per_trade_pct')}% of equity</b> — a wider stop buys fewer
        shares, so every position loses the same amount when it's wrong. Capped at
        {cfg.get('max_positions')} positions and {cfg.get('max_per_sector')} per sector. Candidates are ranked
        by reward:risk, not by score: given two acceptable setups it takes the one that pays more for the same
        risk.</p>
    </section>

    <section>
      <h2 class="section-title">Open positions</h2>
      <div class="table-scroll">
        <table>
          <thead><tr><th></th><th>Type</th><th>Bought</th><th>Now</th><th>Unrealized</th>
            <th>Stop / target</th><th>Why it bought</th></tr></thead>
          <tbody>{pos_html}</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2 class="section-title">Recent activity</h2>
      <ul class="bot-actions">{action_items}</ul>
    </section>

    <section>
      <h2 class="section-title">Closed trades</h2>
      <p class="tab-blurb">Every completed trade, wins and losses alike, with the exit rule that ended it.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th></th><th>Closed by</th><th>Entry</th><th>Exit</th><th>Return</th>
            <th>P&amp;L</th><th>Held</th></tr></thead>
          <tbody>{closed_html}</tbody>
        </table>
      </div>
    </section>"""


def backtest_tab_html(bt):
    """The backtest panel. Deliberately its own tab, and deliberately loud
    about its limits: a page showing an equity curve next to the live bot's
    equity curve invites confusing a simulation with a track record, and the
    two are not remotely the same kind of evidence."""
    if not bt:
        return """
    <section>
      <h2 class="section-title">Backtest</h2>
      <div class="empty-note">No backtest has been run yet. It executes automatically on the next
        market-hours refresh cycle (then re-runs weekly) and takes a couple of minutes.</div>
    </section>"""

    cfg = bt.get("config") or {}
    win = bt.get("window") or {}
    ret = bt.get("total_return_pct")
    bench = bt.get("benchmark_buy_hold_pct")
    excess = bt.get("excess_vs_benchmark_pct")
    beat = (excess or 0) > 0

    chart = equity_curve_svg(bt.get("equity_curve"), cfg.get("starting_equity") or 100000)

    reasons = bt.get("exit_reasons") or {}
    reason_html = " · ".join(f"<b>{v}</b> {esc(k.replace('_', ' '))}" for k, v in
                             sorted(reasons.items(), key=lambda x: -x[1])) or "—"

    dep = bt.get("deployment") or {}
    deploy_html = ""
    if dep:
        b = dep.get("blocked") or {}
        total_blocked = (b.get("slots_full", 0) + b.get("sector_cap", 0)
                         + b.get("size_reject", 0))
        entered = b.get("entered", 0)
        # The question this panel exists to answer: was the account idle
        # because nothing qualified, or because a cap turned qualifying
        # candidates away? Those have opposite fixes.
        avg_inv = dep.get("avg_invested_pct") or 0
        if avg_inv >= 85:
            verdict = ("Fully deployed. Cash drag is not what is holding the return down, "
                       "so the return has to come from the trades themselves.")
        elif total_blocked > entered:
            verdict = (f"Under-deployed at {avg_inv}% invested, and caps turned away "
                       f"{total_blocked:,} qualifying candidates against {entered:,} taken. "
                       "The limit is the position/sector/sizing caps, not signal quality.")
        else:
            verdict = (f"Under-deployed at {avg_inv}% invested, but caps only turned away "
                       f"{total_blocked:,} candidates against {entered:,} taken. The account "
                       "sat in cash mostly because nothing qualified &mdash; loosening the "
                       "caps would not have filled it.")
        rej = dep.get("size_reject_reasons") or {}
        rej_html = (" &middot; ".join(f"<b>{v:,}</b> {esc(k)}" for k, v in
                                      sorted(rej.items(), key=lambda x: -x[1]))
                    or "&mdash;")
        deploy_html = f"""
    <section>
      <h2 class="section-title">Capital deployment</h2>
      <p class="tab-blurb">A return earned while most of the account sits in cash is a different
        result from the same return earned fully invested &mdash; and the two call for opposite
        fixes. This measures which one happened.</p>
      <div class="bot-stat-grid">
        <div class="bot-stat"><span class="bot-stat-num">{dep.get('avg_invested_pct')}%</span>
          <span class="bot-stat-label">avg invested (median {dep.get('median_invested_pct')}%)</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{dep.get('avg_open_positions')}</span>
          <span class="bot-stat-label">avg open of {dep.get('max_positions_cap')} slots</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{dep.get('pct_sessions_at_cap')}%</span>
          <span class="bot-stat-label">sessions at full cap</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{dep.get('pct_sessions_under_half')}%</span>
          <span class="bot-stat-label">sessions under half full</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{dep.get('avg_candidates_per_session')}</span>
          <span class="bot-stat-label">candidates/session ({b.get('no_candidate_day', 0):,} with none)</span></div>
      </div>
      <p class="tab-blurb" style="margin-top:12px;"><b>Entries taken:</b> {entered:,} &middot;
        <b>turned away by slot cap:</b> {b.get('slots_full', 0):,} &middot;
        <b>by sector cap:</b> {b.get('sector_cap', 0):,} &middot;
        <b>by sizing:</b> {b.get('size_reject', 0):,}<br>
        <span style="opacity:.75">sizing rejections: {rej_html}</span><br>
        <span style="opacity:.6">Turn-aways are counted per candidate per session, so the same
        stock blocked on ten consecutive days counts ten times. Read them as a ratio against
        entries taken, not as a count of missed opportunities.</span></p>
      <div class="empty-note" style="margin-top:12px;">{verdict}</div>
    </section>"""

    gb = bt.get("giveback") or {}
    give_html = ""
    if gb.get("trades_measured"):
        gr_rows = "".join(
            f"<tr><td class='mono'>was up &ge; <b>{g['threshold_pct']:.0f}%</b></td>"
            f"<td class='price-cell mono'>{g['trades']}</td>"
            f"<td class='price-cell mono'>{g['pct_of_all_losers']}%</td>"
            f"<td class='price-cell'>{(str(g['avg_peak_pct']) + '%') if g['avg_peak_pct'] is not None else '&mdash;'}</td>"
            f"<td class='price-cell mono pnl-bad'>${g['giveback_dollars']:,.0f}</td></tr>"
            for g in (gb.get("green_then_red") or []))
        give_html = f"""
    <section>
      <h2 class="section-title">Giveback &mdash; green, then red</h2>
      <p class="tab-blurb">Every trade records how far it ran in your favour before it closed.
        Peak minus realized is exactly what was handed back. The table below is the one that
        matters: trades that ended as losses <i>after</i> having been profitable. Those are the
        ones a tighter gain-protection ladder could have saved &mdash; and the ones that make
        holding a winner feel like a mistake.</p>
      <div class="bot-stat-grid">
        <div class="bot-stat"><span class="bot-stat-num">{gb.get('avg_giveback_pct')}%</span>
          <span class="bot-stat-label">avg giveback per trade</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{gb.get('avg_giveback_winners_pct')}%</span>
          <span class="bot-stat-label">given back by winners</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{gb.get('avg_giveback_losers_pct')}%</span>
          <span class="bot-stat-label">given back by losers</span></div>
        <div class="bot-stat"><span class="bot-stat-num pnl-bad">${gb.get('total_giveback_dollars', 0):,.0f}</span>
          <span class="bot-stat-label">total handed back</span></div>
      </div>
      <div class="table-scroll" style="margin-top:14px;">
        <table>
          <thead><tr><th>Losing trades that&hellip;</th><th>Trades</th><th>Share of losers</th>
            <th>Avg peak</th><th>Given back</th></tr></thead>
          <tbody>{gr_rows}</tbody>
        </table>
      </div>
      <p class="tab-blurb" style="margin-top:10px; opacity:.75">This is measured on the trades that
        actually happened, so it sizes the prize &mdash; it does not prove a tighter ladder would
        have captured it. An earlier exit changes every trade after it, which is what the ladder
        comparison below actually tests.</p>
    </section>"""

    # Precomputed: an f-string expression part cannot contain a backslash,
    # and an inline style attribute needs escaped quotes. (Third time.)
    HILITE = ' style="background:var(--surface-2)"'

    wf = bt.get("walk_forward") or {}
    wf_html = ""
    if wf and not wf.get("error") and wf.get("rows"):
        rows = [r for r in wf["rows"]
                if r.get("train_return_pct") is not None and r.get("test_return_pct") is not None]
        rows.sort(key=lambda r: -r["train_return_pct"])
        picked = wf.get("picked_on_train")
        wrows = "".join(
            f"<tr{HILITE if r['label'] == picked else ''}>"
            f"<td class='mono'>{esc(r['label'])}"
            f"{' &larr; picked on train' if r['label'] == picked else ''}</td>"
            f"<td class='price-cell {'pnl-good' if r['train_return_pct'] >= 0 else 'pnl-bad'}'>"
            f"{pct(r['train_return_pct'])}</td>"
            f"<td class='price-cell {'pnl-good' if r['test_return_pct'] >= 0 else 'pnl-bad'}'>"
            f"{pct(r['test_return_pct'])}</td>"
            f"<td class='price-cell mono'>{r.get('train_trades')}</td>"
            f"<td class='price-cell mono'>{r.get('test_trades')}</td></tr>"
            for r in rows)

        rho = wf.get("rank_correlation")
        rank = wf.get("picked_rank_on_test")
        n = wf.get("candidates")
        edge = wf.get("edge_vs_random_pick_pct")
        if rho is None:
            verdict = "Rank correlation could not be computed."
        elif rho >= 0.5:
            verdict = (f"Rank correlation {rho}. Choosing on the first half carried real "
                       f"information into the second &mdash; the tuning is doing something.")
        elif rho > 0.15:
            verdict = (f"Rank correlation {rho}: weak. Some signal, but a settings choice made on "
                       f"the first half only loosely predicts the second. Treat small gaps between "
                       f"variants as noise.")
        elif rho >= -0.15:
            verdict = (f"Rank correlation {rho}: essentially zero. Ranking settings on one stretch "
                       f"tells you almost nothing about the next one. The in-sample tables above are "
                       f"measuring the window, not the strategy &mdash; prefer the simplest setting "
                       f"over the highest-scoring one.")
        else:
            verdict = (f"Rank correlation {rho}: NEGATIVE. Picking the in-sample winner did worse "
                       f"than picking at random. Any setting chosen off the tables above is likely "
                       f"to be the wrong one.")

        # --- same-concentration random benchmark -------------------------
        rb_all = wf.get("random_benchmark") or {}
        rand_rows, rand_note = "", ""
        for phase in ("train", "test"):
            rb = rb_all.get(phase) or {}
            if not rb or rb.get("error") or rb.get("bot_percentile") is None:
                continue
            p = rb["bot_percentile"]
            rand_rows += (
                f"<tr><td class='mono'><b>{phase}</b></td>"
                f"<td class='price-cell mono'>{pct(rb.get('bot_return_pct'))}</td>"
                f"<td class='price-cell mono'>{pct(rb.get('median_pct'))}</td>"
                f"<td class='price-cell mono' style='opacity:.7'>"
                f"{pct(rb.get('p25_pct'))} &ndash; {pct(rb.get('p75_pct'))}</td>"
                f"<td class='price-cell {'pnl-good' if p >= 50 else 'pnl-bad'}'><b>{p}th</b></td></tr>")
        if rand_rows:
            tp = ((rb_all.get("test") or {}).get("bot_percentile"))
            if tp is None:
                rand_note = ""
            elif tp >= 65:
                rand_note = ("Out-of-sample the bot beat most equally-concentrated random portfolios. "
                             "The entry signal is adding something beyond luck.")
            elif tp >= 35:
                rand_note = ("Out-of-sample the bot sits mid-pack among random portfolios of the same "
                             "size. The picking is not measurably better than chance &mdash; the gap to "
                             "buy-and-hold is mostly the cost of holding ten names instead of the whole "
                             "universe, not bad selection.")
            else:
                rand_note = ("Out-of-sample the bot lost to most random portfolios of the same size. "
                             "The entry score is choosing worse than chance, and holding more names "
                             "would beat picking better ones.")
            rb_t = rb_all.get("test") or {}
            rand_html = f"""
      <h2 class="section-title" style="margin-top:26px;">Versus random portfolios of the same size</h2>
      <p class="tab-blurb">Buy-and-hold of all {rb_t.get('universe_size', '')} names is the wrong
        yardstick for a bot that holds {rb_t.get('names_per_portfolio', 10)} at a time &mdash; a
        concentrated portfolio trails a broad one in a broad rally with nothing wrong with its picks.
        So: {rb_t.get('trials', '')} random {rb_t.get('names_per_portfolio', 10)}-name portfolios drawn
        from the same universe, held through the same window, paying the same costs, with zero skill.
        Where the bot lands in that spread separates <i>bad selection</i> from <i>plain
        concentration</i>.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Window</th><th>Bot</th><th>Random median</th>
            <th>Random 25th&ndash;75th</th><th>Bot percentile</th></tr></thead>
          <tbody>{rand_rows}</tbody>
        </table>
      </div>
      <div class="empty-note" style="margin-top:12px;">{rand_note}</div>"""
        else:
            rand_html = ""

        # --- entry-signal experiments ------------------------------------
        ents = [e for e in (wf.get("entry_experiments") or [])
                if "error" not in e and e.get("test_return_pct") is not None]
        entry_html = ""
        if ents:
            control = next((e for e in ents if e.get("mode") == "any"), None)
            live_row_e = next((e for e in ents if "live" in e.get("label", "")), None)
            best_e = max(ents, key=lambda e: e.get("test_percentile") or -1)
            erows = "".join(
                f"<tr{HILITE if e is best_e else ''}>"
                f"<td class='mono'>{esc(e['label'])}"
                f"{' &larr; best out-of-sample' if e is best_e else ''}</td>"
                f"<td class='price-cell {'pnl-good' if (e.get('train_return_pct') or 0) >= 0 else 'pnl-bad'}'>"
                f"{pct(e.get('train_return_pct'))}</td>"
                f"<td class='price-cell {'pnl-good' if (e.get('test_return_pct') or 0) >= 0 else 'pnl-bad'}'>"
                f"{pct(e.get('test_return_pct'))}</td>"
                f"<td class='price-cell {'pnl-good' if (e.get('test_percentile') or 0) >= 50 else 'pnl-bad'}'>"
                f"<b>{e.get('test_percentile')}th</b></td>"
                f"<td class='price-cell mono'>{e.get('test_trades')}</td>"
                f"<td class='price-cell mono'>{e.get('test_invested_pct')}%</td></tr>"
                for e in ents)

            cp = (control or {}).get("test_percentile")
            lp = (live_row_e or {}).get("test_percentile")
            if cp is None or lp is None:
                enote = "Control or live row missing &mdash; read the table directly."
            elif lp > cp + 10:
                enote = (f"The live scoring rule ({lp}th percentile) beat the no-signal control "
                         f"({cp}th) out-of-sample. The score is contributing something.")
            elif lp < cp - 10:
                enote = (f"The no-signal control ({cp}th percentile) beat the live scoring rule "
                         f"({lp}th) out-of-sample &mdash; filling slots at random did better than "
                         f"scoring them. On this evidence the entry score is subtracting value, and "
                         f"holding more names beats picking better ones.")
            else:
                enote = (f"Live rule {lp}th vs no-signal control {cp}th percentile: too close to "
                         f"separate. The scoring is not measurably better OR worse than choosing at "
                         f"random, which is its own verdict &mdash; it is not earning the complexity.")

            entry_html = f"""
      <h2 class="section-title" style="margin-top:26px;">Entry signal &mdash; does the score help at all?</h2>
      <p class="tab-blurb">Same split, same risk engine, same costs &mdash; only the BUY rule changes.
        <b>strength</b> is the live rule (buy names already scoring high) at three different bars.
        <b>weakness</b> is the identical score with the sign flipped (buy the worst-scoring names first) &mdash;
        mean reversion instead of momentum. <b>no signal</b> fills the slots at random and is the honest
        floor: any rule that cannot beat it is not paying for itself. The percentile column is each rule
        against the same {(rb_all.get('test') or {}).get('trials', '')} coin-flip portfolios, on the
        half none of them were chosen on.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Buy rule</th><th>Train</th><th>Test</th>
            <th>Test percentile</th><th>Test trades</th><th>Invested</th></tr></thead>
          <tbody>{erows}</tbody>
        </table>
      </div>
      <div class="empty-note" style="margin-top:12px;">{enote}</div>
      <p class="tab-blurb" style="margin-top:10px; opacity:.75">A flipped signal that looks brilliant is
        the single easiest thing to curve-fit, so the train column is context, not evidence. Only the
        test column and its percentile count &mdash; and one split is still one sample.</p>"""

        wf_html = f"""
    <section>
      <h2 class="section-title">Walk-forward &mdash; the only out-of-sample number here</h2>
      <p class="tab-blurb">Every other table on this page picks its winner and measures it on the same
        two years, so the winner's margin is partly just the best draw out of many. This splits the
        window: each candidate is ranked on the first half (<b>train</b>), then run on the second half
        (<b>test</b>), which had no say in the choice. The question is not which row is highest &mdash;
        it is whether being highest on train predicts anything at all on test.</p>
      <div class="bot-stat-grid">
        <div class="bot-stat"><span class="bot-stat-num">{pct(wf.get('picked_test_return_pct'))}</span>
          <span class="bot-stat-label">train winner, out-of-sample</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{rank}/{n}</span>
          <span class="bot-stat-label">its rank on test</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{pct(wf.get('avg_test_return_pct'))}</span>
          <span class="bot-stat-label">avg candidate on test</span></div>
        <div class="bot-stat"><span class="bot-stat-num {'pnl-good' if (edge or 0) >= 0 else 'pnl-bad'}">{pct(edge)}</span>
          <span class="bot-stat-label">edge from tuning vs random pick</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{rho}</span>
          <span class="bot-stat-label">train&rarr;test rank correlation</span></div>
      </div>
      <div class="empty-note" style="margin-top:12px;">{verdict}</div>
      <div class="table-scroll" style="margin-top:14px;">
        <table>
          <thead><tr><th>Candidate</th>
            <th>Train ({esc(wf['train_window']['from'])} &rarr; {esc(wf['train_window']['to'])})</th>
            <th>Test ({esc(wf['test_window']['from'])} &rarr; {esc(wf['test_window']['to'])})</th>
            <th>Train trades</th><th>Test trades</th></tr></thead>
          <tbody>{wrows}</tbody>
        </table>
      </div>
      <p class="tab-blurb" style="margin-top:10px; opacity:.75">Each half starts fresh at the same
        starting equity, so the two columns are independent runs rather than one compounding sequence.
        Half a window is also a small sample &mdash; this check is good at exposing overfitting, and
        weak at proving an edge exists.</p>
      {rand_html}
      {entry_html}
    </section>"""

    ladders = [l for l in (bt.get("ratchet_sweep") or []) if "error" not in l]
    ladder_html = ""
    if ladders:
        best = max(ladders, key=lambda l: l.get("return_pct") or -999)
        lrows = "".join(
            f"<tr{HILITE if l is best else ''}>"
            f"<td class='mono'>{esc(l['label'])}{' &larr; best' if l is best else ''}</td>"
            f"<td class='price-cell {'pnl-good' if (l.get('return_pct') or 0) >= 0 else 'pnl-bad'}'>"
            f"{pct(l.get('return_pct'))}</td>"
            f"<td class='price-cell'>{pct(l.get('max_dd_pct'))}</td>"
            f"<td class='price-cell mono'>{l.get('trades')}</td>"
            f"<td class='price-cell mono'>{l.get('hit_rate_pct')}%</td>"
            f"<td class='price-cell mono'>{l.get('realized_rr')}:1</td>"
            f"<td class='price-cell mono'>{l.get('green_then_red_5')}</td></tr>"
            for l in ladders)
        ladder_html = f"""
    <section>
      <h2 class="section-title">Gain protection &mdash; ladder comparison</h2>
      <p class="tab-blurb">The whole backtest re-run with different stop-ratchet ladders. A ladder
        that protects earlier turns green-then-red losses into small wins &mdash; and pays for it by
        being stopped out of trades that would have recovered and run to target. Watch the last two
        columns move in opposite directions: that tension, not any single number, is the real
        selling problem.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Ladder</th><th>Return</th><th>Max DD</th><th>Trades</th>
            <th>Win rate</th><th>Realized R:R</th><th>Green&rarr;red (&ge;5%)</th></tr></thead>
          <tbody>{lrows}</tbody>
        </table>
      </div>
    </section>"""

    yearly = bt.get("yearly") or []
    yearly_html = ""
    if len(yearly) > 1:
        yrows = "".join(
            f"<tr><td class='mono'><b>{esc(y.get('year'))}</b></td>"
            f"<td class='price-cell {'pnl-good' if (y.get('return_pct') or 0) >= 0 else 'pnl-bad'}'>"
            f"{pct(y.get('return_pct'))}</td>"
            f"<td class='price-cell mono'>${y.get('start_equity'):,.0f} &rarr; ${y.get('end_equity'):,.0f}</td>"
            f"<td class='price-cell'>{pct(y.get('max_drawdown_pct'))}</td>"
            f"<td class='price-cell mono'>{y.get('trades')}</td>"
            f"<td class='price-cell mono'>{y.get('hit_rate_pct')}%</td></tr>"
            for y in yearly)
        yearly_html = f"""
    <section>
      <h2 class="section-title">Year by year</h2>
      <p class="tab-blurb">The headline number is the whole window compounded. This splits it, because one
        strong stretch carrying a flat one looks identical in aggregate to a steady edge &mdash; and only the
        second is worth trusting. Equity carries across years: gains are reinvested, so position sizes grow
        with the account.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Year</th><th>Return</th><th>Equity</th><th>Max DD</th>
            <th>Trades</th><th>Win rate</th></tr></thead>
          <tbody>{yrows}</tbody>
        </table>
      </div>
    </section>"""

    sens = bt.get("sensitivity") or []
    # One column per calendar year, so a variant that only works in one year is
    # visible as such instead of hiding inside a two-year total.
    sens_years = sorted({y.get("year") for v in sens for y in (v.get("yearly") or [])
                         if y.get("year")})
    sens_parts = []
    for v in sens:
        if "error" in v:
            continue
        live = (bool(v.get("vol_scaled")) == bool(cfg.get("vol_scaled"))
                and (v.get("vol_scaled")
                     or (v.get("stop_pct") == cfg.get("stop_pct")
                         and v.get("target_pct") == cfg.get("target_pct"))))
        ret_cls = "pnl-good" if (v.get("return_pct") or 0) >= 0 else "pnl-bad"
        exp_cls = "pnl-good" if (v.get("expectancy_pct") or 0) >= 0 else "pnl-bad"
        # Precomputed: an f-string expression cannot contain a backslash, and
        # a nested quoted f-string here needs one.
        label = ("vol-scaled" if v.get("vol_scaled")
                 else f"{v.get('stop_pct')}% / {v.get('target_pct')}%")
        vy = {y.get("year"): y for y in (v.get("yearly") or [])}
        year_cells = ""
        for yr in sens_years:
            y = vy.get(yr)
            if not y:
                year_cells += "<td class='price-cell'>&mdash;</td>"
                continue
            ycls = "pnl-good" if (y.get("return_pct") or 0) >= 0 else "pnl-bad"
            year_cells += f"<td class='price-cell {ycls}'>{pct(y.get('return_pct'))}</td>"
        sens_parts.append(
            f"<tr{HILITE if live else ''}>"
            f"<td class='mono'>{label}"
            f"{' &larr; live' if live else ''}</td>"
            f"<td class='mono'>{v.get('rr')}:1</td>"
            f"<td class='price-cell {ret_cls}'>{pct(v.get('return_pct'))}</td>"
            f"<td class='price-cell'>{pct(v.get('max_dd_pct'))}</td>"
            f"<td class='price-cell mono'>{v.get('trades')}</td>"
            f"<td class='price-cell mono'>{v.get('hit_rate_pct')}%</td>"
            f"<td class='price-cell {exp_cls}'>{pct(v.get('expectancy_pct'))}</td>"
            f"{year_cells}</tr>")
    sens_rows = "".join(sens_parts) or \
        f'<tr><td colspan="{7 + len(sens_years)}" class="empty-cell">Sensitivity sweep unavailable.</td></tr>'
    sens_year_heads = "".join(f"<th>{esc(y)}</th>" for y in sens_years)

    trades = bt.get("trades") or []
    trade_rows = "".join(
        f"<tr><td class='ticker-cell'><div class='ticker'>{esc(t.get('ticker'))}</div>"
        f"<div class='company'>{esc(t.get('sector') or '')}</div></td>"
        f"<td><span class='pill' style=\"--pill-color:{'var(--good)' if t.get('reason') in ('target','trailing') else ('var(--critical)' if t.get('reason')=='stop' else 'var(--warning)')};font-size:10px;\">{esc((t.get('reason') or '').replace('_',' '))}</span></td>"
        f"<td class='price-cell'>${t.get('entry_price'):,.2f}</td>"
        f"<td class='price-cell'>${t.get('exit_price'):,.2f}</td>"
        f"<td class='price-cell {'pnl-good' if (t.get('pnl_pct') or 0) >= 0 else 'pnl-bad'}'>{pct(t.get('pnl_pct'))}</td>"
        f"<td class='price-cell {'pnl-good' if (t.get('pnl_dollars') or 0) >= 0 else 'pnl-bad'}'>${t.get('pnl_dollars'):+,.0f}</td>"
        f"<td class='price-cell mono' style='font-size:11px;color:var(--ink-faint)'>{t.get('held_days')}d</td></tr>"
        for t in trades[:40]
    ) or '<tr><td colspan="7" class="empty-cell">No trades.</td></tr>'

    return f"""
    <section>
      <h2 class="section-title">Backtest — risk engine</h2>

      <div class="bot-disclaimer" style="border-left-color:var(--critical)">
        <b>This is a simulation, not the bot's track record.</b> It tests the <b>exit rules and position
        sizing</b> over {esc(win.get('from') or '')} → {esc(win.get('to') or '')} on
        {win.get('universe_size')} real tickers — <b>not</b> the live bot's entry signal. Entries here use
        technicals only, because fundamentals, analyst targets and news sentiment cannot be reconstructed
        for past dates; using today's values against last year's prices would be lookahead bias and would
        make any result meaningless. Also: survivorship bias (only tickers that still exist today), no
        commissions or slippage, stops checked against daily closes rather than intraday, and one year is
        one sample of one market regime. Treat it as evidence about the <b>risk framework</b>, nothing more.
      </div>

      <div class="bot-stat-grid">
        <div class="bot-stat"><span class="bot-stat-num">${bt.get('final_equity', 0):,.0f}</span>
          <span class="bot-stat-label">ending equity</span></div>
        <div class="bot-stat"><span class="bot-stat-num {'pnl-good' if (ret or 0) >= 0 else 'pnl-bad'}">{pct(ret)}</span>
          <span class="bot-stat-label">strategy return</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{pct(bench)}</span>
          <span class="bot-stat-label">buy &amp; hold</span></div>
        <div class="bot-stat"><span class="bot-stat-num {'pnl-good' if beat else 'pnl-bad'}">{pct(excess)}</span>
          <span class="bot-stat-label">vs benchmark</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{pct(bt.get('max_drawdown_pct'))}</span>
          <span class="bot-stat-label">max drawdown</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{bt.get('hit_rate_pct')}%</span>
          <span class="bot-stat-label">win rate</span></div>
        <div class="bot-stat"><span class="bot-stat-num">{f"{bt.get('realized_rr')}:1" if bt.get('realized_rr') else '—'}</span>
          <span class="bot-stat-label">realized R:R</span></div>
        <div class="bot-stat"><span class="bot-stat-num {'pnl-good' if (bt.get('expectancy_pct') or 0) >= 0 else 'pnl-bad'}">{pct(bt.get('expectancy_pct'))}</span>
          <span class="bot-stat-label">expectancy/trade</span></div>
      </div>

      {chart}

      <p class="tab-blurb">
        <b>{'Beat' if beat else 'Did not beat'} buy-and-hold by {abs(excess or 0):.1f} points.</b>
        {bt.get('closed_trades')} closed trades, average hold {bt.get('avg_hold_days')} days,
        average win {pct(bt.get('avg_win_pct'))} against average loss {pct(bt.get('avg_loss_pct'))}.
        Exits: {reason_html}.
        Sized at {cfg.get('risk_per_trade_pct')}% risk per trade from ${cfg.get('starting_equity', 0):,.0f},
        max {cfg.get('max_positions')} positions.</p>
    </section>

    {deploy_html}

    {give_html}

    {wf_html}

    {ladder_html}

    {yearly_html}

    <section>
      <h2 class="section-title">Parameter sensitivity</h2>
      <p class="tab-blurb">The same window re-run at different stop/target settings. This matters more than the
        headline number: one parameter set that looks good is usually luck, whereas a whole neighbourhood
        that looks good is closer to a real effect. If only the highlighted row works, the result is fitted
        to this particular window and should not be trusted. The year columns split each variant apart &mdash;
        a setting that wins the total on the back of a single strong year is fitted, not better, and only
        the split can tell those two apart.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Stop / target</th><th>R:R</th><th>Return</th><th>Max DD</th>
            <th>Trades</th><th>Win rate</th><th>Expectancy</th>{sens_year_heads}</tr></thead>
          <tbody>{sens_rows}</tbody>
        </table>
      </div>
    </section>

    <section>
      <h2 class="section-title">Simulated trades</h2>
      <p class="tab-blurb">Most recent {min(len(trades), 40)} of {bt.get('closed_trades')} closed positions.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th></th><th>Closed by</th><th>Entry</th><th>Exit</th><th>Return</th>
            <th>P&amp;L</th><th>Held</th></tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>
    </section>"""


def risk_reward_panel(summary):
    """The risk/reward scoreboard -- the honest answer to "is this actually
    making money". Leads with EXPECTANCY rather than hit rate, because hit
    rate on its own says nothing: 60% right with small wins and big losses
    loses money, 35% right at 2.5:1 makes it. Shows the breakeven hit rate
    the current reward:risk implies, and how far above or below it you are."""
    target_rr = summary.get("target_rr")
    realized_rr = summary.get("realized_rr")
    exp = summary.get("expectancy_pct")
    be = summary.get("breakeven_hit_rate_pct")
    edge = summary.get("edge_vs_breakeven_pct")
    hit = summary.get("hit_rate_pct")
    n = summary.get("total_closed") or 0

    if n == 0:
        return (f'<div class="rr-panel"><div class="rr-headline">Configured for '
                f'<b>{target_rr}:1</b> reward:risk</div>'
                f'<p class="rr-note">Targeting +{TAKE_PROFIT_PCT:.0f}% against a −{STOP_LOSS_PCT:.0f}% stop. '
                f'At that ratio the break-even hit rate is <b>{be}%</b> — everything above that is profit. '
                f'No calls have closed yet, so there is nothing measured to show.</p></div>')

    exp_tone = "good" if (exp or 0) > 0 else "bad"
    edge_tone = "good" if (edge or 0) > 0 else "bad"
    verdict = ("Positive expectancy — this is making money on the math."
               if (exp or 0) > 0 else
               "Negative expectancy — as graded, this loses money over time.")

    return f"""<div class="rr-panel">
      <div class="rr-grid">
        <div class="rr-cell">
          <span class="rr-num rr-{exp_tone}">{exp:+.2f}%</span>
          <span class="rr-label">expectancy per call</span>
        </div>
        <div class="rr-cell">
          <span class="rr-num">{f'{realized_rr}:1' if realized_rr else '—'}</span>
          <span class="rr-label">realized reward:risk</span>
        </div>
        <div class="rr-cell">
          <span class="rr-num">{f'{hit:.0f}%' if hit is not None else '—'}
            <span class="rr-vs">vs {be}% needed</span></span>
          <span class="rr-label">hit rate vs break-even</span>
        </div>
        <div class="rr-cell">
          <span class="rr-num rr-{edge_tone}">{f'{edge:+.1f}' if edge is not None else '—'} pts</span>
          <span class="rr-label">edge over break-even</span>
        </div>
      </div>
      <p class="rr-note"><b>{esc(verdict)}</b> Expectancy is what one average call is worth, and it is the
        only number that settles the question — hit rate alone can't, since being right 60% of the time with
        small wins and large losses still loses. At the configured {target_rr}:1 target you only need to be
        right <b>{be}%</b> of the time. Measured over {n} closed call{'s' if n != 1 else ''}.</p>
    </div>"""


def value_tab_html(picks, rejected, scan_note):
    """The Hidden Gems panel. Deliberately shows the rejection list alongside
    the picks: a screen that only ever displays its winners teaches you
    nothing about its own selectivity, and the rejections are where most of
    this screen's actual work shows up."""
    blurb = (
        "Stocks that are <b>cheap on their own numbers</b>, attached to a business that is actually "
        "<b>good</b> (profitable, cash-generative, growing, not over-levered), and that "
        "<b>have not moved yet</b>. That last condition is the closest anything can honestly get to "
        "&ldquo;before it pops&rdquo;: a name near its 52-week high, or one that just ran 35%+ in two weeks, is "
        "vetoed outright here &mdash; whatever the story was, the market already priced it. Nothing on this page "
        "predicts a rise. It enforces the conditions that have to hold for an early entry to still be "
        "available, and most candidates fail them."
    )

    if picks:
        picks_html = f'<div class="screener-grid">{"".join(value_card(r) for r in picks)}</div>'
    else:
        picks_html = (
            '<div class="empty-note">No stock cleared every gate this cycle. That is a real result, not a '
            'failure &mdash; the filters are not loosened to keep this tab populated, because a padded list of '
            'mediocre &ldquo;bargains&rdquo; is worse than an empty one.</div>'
        )

    rejected_html = ""
    if rejected:
        rows = "\n".join(
            f"<tr><td class='mono'>{esc(x.get('ticker'))}</td>"
            f"<td>{esc(x.get('name') or '')}</td>"
            f"<td class='mono'>{x.get('score') if x.get('score') is not None else '—'}</td>"
            f"<td>{esc('; '.join(x.get('reasons') or []))}</td></tr>"
            for x in rejected[:25]
        )
        rejected_html = f"""
        <section>
          <h2 class="section-title">Disqualified this cycle</h2>
          <p class="tab-blurb">Names that scored well on cheapness and quality but failed a hard gate. Shown
            because why a candidate was rejected is usually more informative than the ones that passed &mdash;
            and because a screen you cannot audit is a screen you should not trust.</p>
          <div class="table-scroll">
            <table>
              <thead><tr><th>Ticker</th><th>Company</th><th>Score</th><th>Why it was dropped</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </section>"""

    note_html = f'<p class="tab-blurb">{esc(scan_note)}</p>' if scan_note else ""

    return f"""
    <section>
      <h2 class="section-title">Hidden gems</h2>
      <p class="tab-blurb">{blurb}</p>
      {note_html}
      {picks_html}
    </section>
    {rejected_html}"""


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
        reason = c.get("close_reason")
        reason_badge = ""
        if reason and reason != "reversal":
            # Colour by what the exit means for you, not by which rule fired:
            # a booked gain reads green, a cut loss red, a faded thesis amber.
            tone = {"take_profit": "var(--good)", "trailing_stop": "var(--good)",
                    "stop_loss": "var(--critical)", "score_drop": "var(--warning)"}.get(reason, "var(--ink-muted)")
            label = c.get("close_reason_label") or reason
            reason_badge = (f' <span class="pill" style="--pill-color:{tone};font-size:10px;">'
                            f'{esc(label)}</span>')
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(c['ticker'])}</div><div class="company">{esc(c.get('name') or '')}</div></td>
        <td class="signal-cell"><span class="pill" style="--pill-color:{st['color']}">{st['icon']} {st['label']}</span>{reason_badge}</td>
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
    cum_ret = summary["cumulative_return_pct"]
    stop_count = summary["stop_loss_count"]
    rr_panel = risk_reward_panel(summary)
    ratchet_desc = ", ".join(
        f"+{r:.0f}% &rarr; stop {'breakeven' if l == 0 else f'+{l:.0f}%'}"
        for r, l in sorted(RATCHET_STEPS))

    return f"""
    <h2 class="section-title">Signal track record</h2>
    <p class="tab-blurb">Every BUY/SELL call the system has made, graded against what actually happened to the
      price afterward — a BUY is "correct" if it finished up, a SELL if it finished down. A call stays open
      through a fade to HOLD (a single dip isn't a reversal) and closes on whichever of five exits fires first,
      each tagged in the table below: <b>target hit</b> at {TAKE_PROFIT_PCT:.0f}%,
      <b>trailing stop</b> &mdash; the stop ratchets up as the call gains ({ratchet_desc}) and closes if price falls back to it,
      <b>stop-loss</b> at −{STOP_LOSS_PCT:.0f}% from entry, <b>signal faded</b> if the composite score drops
      {SCORE_DROP_EXIT:.0f}+ points below where it opened, or a full <b>reversal</b> to the opposite signal.
      Based on {tr['snapshot_count']} snapshots since {esc(tr['first_snapshot_at'][:10])}.</p>

    {rr_panel}

    <div class="summary-strip">
      <div class="summary-chip"><b>{f'{hit:.0f}%' if hit is not None else '—'}</b>&nbsp;overall hit rate</div>
      <div class="summary-chip"><b>{summary['total_closed']}</b>&nbsp;closed calls</div>
      <div class="summary-chip"><b>{f'{buy_hit:.0f}%' if buy_hit is not None else '—'}</b>&nbsp;BUY hit rate</div>
      <div class="summary-chip"><b>{f'{sell_hit:.0f}%' if sell_hit is not None else '—'}</b>&nbsp;SELL hit rate</div>
      <div class="summary-chip {'pnl-good' if (avg_ret or 0) >= 0 else 'pnl-bad'}"><b>{pct(avg_ret)}</b>&nbsp;avg return/call</div>
      <div class="summary-chip {'pnl-good' if (cum_ret or 0) >= 0 else 'pnl-bad'}"><b>{pct(cum_ret)}</b>&nbsp;cumulative return</div>
      <div class="summary-chip"><b>{stop_count}</b>&nbsp;stopped out</div>
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

    def ret_cell(ret, correct, status=None):
        # An ungraded cell used to render as a bare dash, which reads as
        # "broken" when in fact the horizon simply hasn't arrived. Say which.
        if ret is None:
            label = {"pending": "pending",
                     "no_data": "no data"}.get(status, "—")
            return (f'<td class="price-cell" style="color:var(--ink-faint);font-size:11px">{label}</td>'
                    '<td class="price-cell"></td>')
        cls = "pnl-good" if correct is True else ("pnl-bad" if correct is False else "")
        icon = "✓" if correct is True else ("✗" if correct is False else "…")
        return f'<td class="price-cell {cls}">{pct(ret)}</td><td class="price-cell {cls}">{icon}</td>'

    def call_row(c):
        start_date = (c.get("start_ts") or "")[:16].replace("T", " ")
        return f"""
      <tr>
        <td class="ticker-cell"><div class="ticker">{esc(c['ticker'])}</div><div class="company">{esc(c.get('name') or '')}</div></td>
        <td class="signal-cell">{dir_pill(c['direction'])}</td>
        <td class="price-cell">${c['start_price']:,.2f}</td>
        {ret_cell(c.get('return_pct_1d'), c.get('correct_1d'), c.get('status_1d'))}
        {ret_cell(c.get('return_pct_3d'), c.get('correct_3d'), c.get('status_3d'))}
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
      graded against what price actually did 1 and 3 <i>trading sessions</i> later — a long is "correct" if it
      finished up from the call price, a short if it finished down. Sessions rather than clock hours, so a
      Friday call is graded against Monday instead of being lost to the weekend. "pending" means that session
      hasn't happened yet; "no data" means the ticker rotated out of the pool before it could be graded.
      Based on {tr['snapshot_count']} snapshots.</p>

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
    value_picks = payload.get("value_picks", [])
    value_rejected = payload.get("value_rejected", [])
    value_scan_note = payload.get("value_scan_note", "")
    botdata = payload.get("bot")
    backtestdata = payload.get("backtest")
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

    value_html = value_tab_html(value_picks, value_rejected, value_scan_note)
    bot_html = bot_tab_html(botdata)
    backtest_html = backtest_tab_html(backtestdata)

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
  .countdown {{
    display: flex; align-items: baseline; gap: 6px; margin-top: 4px;
  }}
  .countdown-label {{ color: var(--ink-faint); }}
  .countdown-value {{
    color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 600;
  }}
  .countdown.is-running .countdown-value {{ color: var(--accent); }}
  .countdown.is-closed .countdown-value {{ color: var(--ink-muted); font-weight: 400; }}
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
  .value-headline {{
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 10px; margin: 4px 0 8px;
    padding-bottom: 8px; border-bottom: 1px solid var(--border);
  }}
  .value-upside {{
    font-family: 'IBM Plex Mono', monospace; font-size: 26px; font-weight: 600;
    line-height: 1.1; color: var(--pill-color); font-variant-numeric: tabular-nums;
    display: flex; flex-direction: column;
  }}
  .value-upside-label {{
    font-family: 'IBM Plex Sans', sans-serif; font-size: 10px; font-weight: 400;
    color: var(--ink-faint); letter-spacing: 0.02em; margin-top: 2px;
  }}
  .value-score {{
    font-family: 'IBM Plex Mono', monospace; font-size: 17px; font-weight: 600;
    color: var(--ink-muted); font-variant-numeric: tabular-nums;
    display: flex; flex-direction: column; align-items: flex-end; text-align: right;
  }}

  .bot-disclaimer {{
    background: var(--surface-2); border: 1px solid var(--border);
    border-left: 3px solid var(--warning);
    border-radius: 8px; padding: 12px 14px; margin-bottom: 16px;
    font-size: 12px; line-height: 1.55; color: var(--ink-muted); max-width: 78ch;
  }}
  .bot-disclaimer b {{ color: var(--ink); }}
  .bot-stat-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 12px 16px; margin-bottom: 18px;
  }}
  .bot-stat {{ display: flex; flex-direction: column; gap: 2px; }}
  .bot-stat-num {{
    font-family: 'IBM Plex Mono', monospace; font-size: 19px; font-weight: 600;
    color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.15;
  }}
  .bot-stat-label {{
    font-size: 9.5px; color: var(--ink-faint); letter-spacing: 0.04em; text-transform: uppercase;
  }}
  .equity-chart {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px 8px; margin-bottom: 16px;
  }}
  .equity-chart svg {{ width: 100%; height: 140px; display: block; outline: none; }}
  .equity-chart svg:focus-visible {{ box-shadow: 0 0 0 2px var(--accent); border-radius: 6px; }}
  .eq-head {{
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; margin-bottom: 10px; flex-wrap: wrap;
  }}
  .eq-range {{ display: flex; gap: 2px; }}
  .eq-range button {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: 0.04em;
    padding: 4px 9px; border-radius: 6px; cursor: pointer;
    background: transparent; border: 1px solid transparent; color: var(--ink-faint);
    transition: background 120ms ease, color 120ms ease;
  }}
  .eq-range button:hover {{ background: var(--surface-2); color: var(--ink-muted); }}
  .eq-range button.is-on {{
    background: var(--surface-2); border-color: var(--border); color: var(--ink);
  }}
  .eq-summary {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    font-variant-numeric: tabular-nums; color: var(--ink-muted);
  }}
  .eq-plot {{ position: relative; touch-action: none; }}
  .eq-tip {{
    position: absolute; top: -4px; pointer-events: none; z-index: 4;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 6px 9px; box-shadow: 0 4px 14px rgba(0,0,0,0.16);
    display: flex; flex-direction: column; gap: 1px; white-space: nowrap;
  }}
  .eq-tip-val {{
    font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 600;
    color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.2;
  }}
  .eq-tip-when {{ font-size: 10px; color: var(--ink-faint); }}
  .eq-tip-delta {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    font-variant-numeric: tabular-nums;
  }}
  .equity-axis {{
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 6px; font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; color: var(--ink-faint); font-variant-numeric: tabular-nums;
  }}
  .equity-axis-mid {{ font-family: 'IBM Plex Sans', sans-serif; letter-spacing: 0.02em; }}
  ul.bot-actions {{ list-style: none; padding: 0; margin: 0; font-size: 12px; }}
  ul.bot-actions li {{
    padding: 7px 0; border-bottom: 1px solid var(--border); color: var(--ink-muted);
  }}
  ul.bot-actions li:last-child {{ border-bottom: none; }}
  .act-buy, .act-sell {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 600;
    padding: 1px 6px; border-radius: 4px; margin-right: 6px;
  }}
  .act-buy {{ background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }}
  .act-sell {{ background: color-mix(in srgb, var(--critical) 18%, transparent); color: var(--critical); }}
  .act-ts {{ color: var(--ink-faint); font-size: 10.5px; margin-left: 4px; }}
  .act-none {{ color: var(--ink-faint); }}
  .rr-panel {{
    background: var(--surface); border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 10px; padding: 16px 18px; margin: 4px 0 18px;
    box-shadow: var(--shadow);
  }}
  .rr-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px 18px;
  }}
  .rr-cell {{ display: flex; flex-direction: column; gap: 3px; }}
  .rr-num {{
    font-family: 'IBM Plex Mono', monospace; font-size: 21px; font-weight: 600;
    color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.15;
  }}
  .rr-num.rr-good {{ color: var(--good); }}
  .rr-num.rr-bad {{ color: var(--critical); }}
  .rr-vs {{
    font-family: 'IBM Plex Sans', sans-serif; font-size: 11px;
    font-weight: 400; color: var(--ink-faint); margin-left: 4px;
  }}
  .rr-label {{
    font-size: 10px; color: var(--ink-faint); letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .rr-note {{
    margin: 14px 0 0; padding-top: 12px; border-top: 1px solid var(--border);
    font-size: 12px; line-height: 1.55; color: var(--ink-muted); max-width: 70ch;
  }}
  .rr-note b {{ color: var(--ink); }}
  .value-prices {{
    background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 11px; margin-bottom: 9px;
  }}
  .value-price-row {{
    display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  }}
  .value-price-row + .value-price-row {{ margin-top: 3px; }}
  .value-price-num {{
    font-family: 'IBM Plex Mono', monospace; font-size: 16px; font-weight: 600;
    color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.2;
  }}
  .value-price-fair .value-price-num {{ color: var(--pill-color); }}
  .value-price-label {{
    font-size: 10px; color: var(--ink-faint); letter-spacing: 0.03em;
    text-transform: uppercase; white-space: nowrap;
  }}
  .value-price-pe {{
    margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border);
    font-family: 'IBM Plex Mono', monospace; text-align: right;
    font-size: 10.5px; color: var(--ink-faint); font-variant-numeric: tabular-nums;
  }}
  .value-facts {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--ink-muted);
    margin-bottom: 8px; font-variant-numeric: tabular-nums;
  }}
  ul.value-notes {{ max-width: none; font-size: 11.5px; }}
  td.mono {{ font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }}

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
        <button class="nav-btn" data-tab="gems" role="tab" aria-selected="false">Hidden Gems</button>
        <button class="nav-btn" data-tab="bot" role="tab" aria-selected="false">Trading Bot</button>
        <button class="nav-btn" data-tab="backtest" role="tab" aria-selected="false">Backtest</button>
        <button class="nav-btn" data-tab="portfolio" role="tab" aria-selected="false">Portfolio</button>
        <button class="nav-btn" data-tab="trackrecord" role="tab" aria-selected="false">Track Record</button>
      </nav>

      <div class="sidebar-status">
        <div class="updated"><span class="live-dot"></span>Updated {esc(updated_str)}</div>
        <div class="countdown" id="refresh-countdown" data-next="{esc(payload.get('next_refresh_at') or '')}"
             data-interval="{payload.get('refresh_interval_seconds') or 900}">
          <span class="countdown-label">Next refresh</span>
          <span class="countdown-value" id="refresh-countdown-value">—</span>
        </div>
        <div class="countdown" id="price-tick">
          <span class="countdown-label">Prices</span>
          <span class="countdown-value" id="price-tick-value">—</span>
        </div>
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

      <div class="tab-panel" data-panel="gems">
        {value_html}
      </div>

      <div class="tab-panel" data-panel="bot">
        {bot_html}
      </div>

      <div class="tab-panel" data-panel="backtest">
        {backtest_html}
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
        <div>Next refresh: every 15 minutes, market hours, weekdays.</div>
      </footer>

    </main>
  </div>

</div>
<script>

  // --- Live price polling --------------------------------------------------
  // Updates only the numbers, never the page. The heavy dashboard (scores,
  // screeners, rationale) is regenerated server-side every 15 minutes; this
  // keeps quotes and open-position P&L current in between at the cost of one
  // small JSON fetch. If the endpoint is missing or the market is shut, it
  // fails quiet and leaves the server-rendered values alone.
  (function() {{
    var out = document.getElementById('price-tick-value');
    var wrap = document.getElementById('price-tick');
    if (!out) return;
    var TICK_MS = 30000;

    function money(v) {{
      return '$' + v.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}
    function ago(iso) {{
      var s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
      if (s < 60) return s + 's ago';
      var m = Math.floor(s / 60);
      return m < 60 ? m + 'm ago' : Math.floor(m / 60) + 'h ago';
    }}
    function flash(el, up) {{
      el.style.transition = 'none';
      el.style.color = up ? 'var(--good)' : 'var(--critical)';
      setTimeout(function() {{ el.style.transition = 'color 1.2s'; el.style.color = ''; }}, 60);
    }}

    function apply(data) {{
      var prices = (data && data.prices) || {{}};
      var n = 0;
      document.querySelectorAll('[data-live-price]').forEach(function(el) {{
        var p = prices[el.getAttribute('data-live-price')];
        if (!p || p.price == null) return;
        var next = money(p.price);
        if (el.textContent.trim() !== next) {{
          var prev = parseFloat(el.textContent.replace(/[^0-9.\\-]/g, ''));
          el.textContent = next;
          if (!isNaN(prev)) flash(el, p.price >= prev);
        }}
        n++;
      }});
      // Recompute open-position P&L from the live price rather than waiting
      // for the next full cycle -- otherwise the price column moves and the
      // P&L beside it stays stale, which looks broken and misleads.
      document.querySelectorAll('[data-live-pnl]').forEach(function(el) {{
        var p = prices[el.getAttribute('data-live-pnl')];
        var entry = parseFloat(el.getAttribute('data-entry'));
        var shares = parseFloat(el.getAttribute('data-shares'));
        if (!p || p.price == null || !entry || !shares) return;
        var pctv = (p.price - entry) / entry * 100;
        var dollars = (p.price - entry) * shares;
        el.classList.toggle('pnl-good', pctv >= 0);
        el.classList.toggle('pnl-bad', pctv < 0);
        el.innerHTML = (pctv >= 0 ? '+' : '') + pctv.toFixed(2) + '%<br>' +
          '<span style="font-size:10.5px;opacity:0.75">' +
          (dollars >= 0 ? '+$' : '-$') +
          Math.abs(dollars).toLocaleString('en-US', {{maximumFractionDigits: 0}}) + '</span>';
      }});
      recomputeBotEquity(prices);
      if (data && data.updated_at && n) {{
        wrap.className = 'countdown';
        out.textContent = ago(data.updated_at);
      }} else {{
        wrap.className = 'countdown is-closed';
        out.textContent = 'idle';
      }}
    }}

    // Portfolio value is cash + market value of open positions. Cash only
    // changes when the bot trades (every 15 min at most), so the browser can
    // hold it fixed and re-mark the positions against live quotes. Without
    // this the position prices ticked while the headline total sat still,
    // which reads as a broken page.
    function recomputeBotEquity(prices) {{
      var eqEl = document.getElementById('bot-equity');
      if (!eqEl) return;
      var cash = parseFloat(eqEl.getAttribute('data-cash'));
      var start = parseFloat(eqEl.getAttribute('data-start'));
      if (isNaN(cash)) return;
      var mv = 0;
      document.querySelectorAll('[data-live-pnl]').forEach(function(el) {{
        var shares = parseFloat(el.getAttribute('data-shares'));
        var p = prices[el.getAttribute('data-live-pnl')];
        // Fall back to the server's last mark for anything the tick missed,
        // so one missing quote can't silently drop a position from the total.
        var px = (p && p.price != null) ? p.price : parseFloat(el.getAttribute('data-last'));
        if (!isNaN(shares) && !isNaN(px)) mv += shares * px;
      }});
      var equity = cash + mv;
      eqEl.textContent = '$' + Math.round(equity).toLocaleString('en-US');
      var retEl = document.getElementById('bot-return');
      if (retEl && start > 0) {{
        var r = (equity / start - 1) * 100;
        retEl.textContent = (r >= 0 ? '+' : '') + r.toFixed(2) + '%';
        retEl.classList.toggle('pnl-good', r >= 0);
        retEl.classList.toggle('pnl-bad', r < 0);
      }}
    }}

    function poll() {{
      fetch('/api/prices', {{cache: 'no-store'}})
        .then(function(r) {{ return r.ok ? r.json() : null; }})
        .then(function(d) {{ if (d) apply(d); }})
        .catch(function() {{ /* offline or endpoint absent: keep rendered values */ }});
    }}
    poll();
    setInterval(poll, TICK_MS);
  }})();

  // --- Next-refresh countdown -------------------------------------------
  // Market state is computed HERE rather than trusted from the page data.
  // Outside market hours the server skips its work, so generated_at can be
  // hours or days stale -- a countdown driven purely by that timestamp
  // would sit at zero all weekend claiming a refresh was overdue. Working
  // it out client-side keeps the display honest whatever the page's age.
  (function() {{
    var el = document.getElementById('refresh-countdown');
    var out = document.getElementById('refresh-countdown-value');
    if (!el || !out) return;

    var interval = (parseInt(el.getAttribute('data-interval'), 10) || 900) * 1000;
    var nextAt = Date.parse(el.getAttribute('data-next') || '') || 0;
    // A refresh cycle takes a few minutes; the page only changes once it
    // finishes, so reloading the instant the countdown hits zero would just
    // re-fetch the same page. Wait for the run to plausibly complete.
    var CYCLE_MS = 6 * 60 * 1000;
    var reloaded = false;

    function etParts(d) {{
      var f = new Intl.DateTimeFormat('en-US', {{
        timeZone: 'America/New_York', hour12: false,
        weekday: 'short', hour: '2-digit', minute: '2-digit'
      }});
      var p = {{}};
      f.formatToParts(d).forEach(function(x) {{ p[x.type] = x.value; }});
      return p;
    }}

    var DAYS = {{Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6}};

    function marketState(now) {{
      var p = etParts(now);
      var dow = DAYS[p.weekday];
      var mins = parseInt(p.hour, 10) * 60 + parseInt(p.minute, 10);
      var open = 9 * 60 + 30, close = 16 * 60;
      if (dow >= 1 && dow <= 5 && mins >= open && mins < close) {{
        return {{open: true}};
      }}
      // Minutes until the next weekday 9:30 ET, in ET wall-clock terms.
      var wait;
      if (dow >= 1 && dow <= 5 && mins < open) {{
        wait = open - mins;
      }} else {{
        var d = dow, add = 0;
        do {{ add += 1; d = (d + 1) % 7; }} while (d === 0 || d === 6);
        wait = (1440 - mins) + (add - 1) * 1440 + open;
      }}
      return {{open: false, minsToOpen: wait}};
    }}

    function fmt(ms) {{
      if (ms < 0) ms = 0;
      var s = Math.floor(ms / 1000);
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      if (h > 0) return h + 'h ' + String(m).padStart(2, '0') + 'm';
      return m + ':' + String(sec).padStart(2, '0');
    }}

    function tick() {{
      var now = Date.now();
      var st = marketState(new Date(now));

      if (!st.open) {{
        el.className = 'countdown is-closed';
        el.querySelector('.countdown-label').textContent = 'Market opens in';
        out.textContent = fmt(st.minsToOpen * 60 * 1000);
        return;
      }}

      // Market is open.
      // If the anchor is badly out of date the server's actual schedule is
      // unknowable from here -- rolling a days-old timestamp forward in
      // 15-minute steps produces a confident-looking number with nothing
      // behind it. Say the data is stale instead, which is the true and
      // more useful statement, and reload once in case the server has since
      // published something newer.
      var age = nextAt ? (now - nextAt) : 0;
      if (!nextAt || age > 3 * interval) {{
        el.className = 'countdown is-closed';
        el.querySelector('.countdown-label').textContent = 'Data stale';
        out.textContent = nextAt ? fmt(age) + ' old' : 'unknown';
        if (!reloaded && nextAt) {{
          reloaded = true;
          setTimeout(function() {{ location.reload(); }}, 5000);
        }}
        return;
      }}

      // Only a cycle or two behind -- the phase is still meaningful, so
      // roll forward to the next scheduled slot.
      var target = nextAt;
      while (target + CYCLE_MS < now) target += interval;

      var remaining = target - now;
      if (remaining > 0) {{
        el.className = 'countdown';
        el.querySelector('.countdown-label').textContent = 'Next refresh';
        out.textContent = fmt(remaining);
      }} else {{
        el.className = 'countdown is-running';
        el.querySelector('.countdown-label').textContent = 'Refreshing';
        out.textContent = 'now…';
        // Once the cycle has had time to finish, pull the new page in. Guarded
        // so this can only ever happen once per page load.
        if (!reloaded && now > target + CYCLE_MS) {{
          reloaded = true;
          setTimeout(function() {{ location.reload(); }}, 3000);
        }}
      }}
    }}

    tick();
    setInterval(tick, 1000);
  }})();

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
