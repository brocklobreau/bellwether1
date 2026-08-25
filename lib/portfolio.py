"""
User-entered trade log -> automatic P&L. No network, pure math over a plain
list of {ticker, side, shares, price, date, note} trades the user reports in
chat (or via the dashboard's own add-trade form, see lib/portfolio_web.py) --
this module just does the accounting once the trades exist.

FIFO lot matching per ticker: every "sell" closes out the oldest still-open
"buy" lot(s) first. This is the standard, simplest convention -- it's an
accounting choice, not a tax instruction (see the disclaimer baked into the
dashboard). Selling more shares than are on record for a ticker just closes
whatever lots exist and flags the excess rather than modeling a short.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO_PATH = os.path.join(BASE, "portfolio.json")


def load_portfolio():
    if not os.path.exists(PORTFOLIO_PATH):
        return {"trades": []}
    with open(PORTFOLIO_PATH) as f:
        data = json.load(f)
    data.setdefault("trades", [])
    return data


def save_portfolio(data):
    tmp_path = PORTFOLIO_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, PORTFOLIO_PATH)


def _sort_key(t):
    return (t.get("date") or "", t.get("id") or "")


def compute_portfolio(trades, price_lookup=None, levels_lookup=None):
    """
    trades: list of {id, ticker, side ('buy'/'sell'), shares, price, date, note}
    price_lookup: {ticker: current_price} for tickers the pipeline already tracks
    levels_lookup: {ticker: investing_levels dict} (entry_zone/exit_zone/stop),
        used to flag "in your exit zone" etc. on open positions

    Returns:
      {
        "open_positions": [ {ticker, shares, avg_cost, cost_basis, current_price,
            unrealized_pnl, unrealized_pnl_pct, tracked, zone_flag, zone_note}, ... ],
        "closed_lots": [ {ticker, shares, buy_price, sell_price, buy_date, sell_date,
            realized_pnl, realized_pnl_pct}, ... ],
        "warnings": [ "..." ],
        "totals": {cost_basis, market_value, unrealized_pnl, realized_pnl, tracked_value},
      }
    """
    price_lookup = price_lookup or {}
    levels_lookup = levels_lookup or {}

    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t.get("ticker", "").upper(), []).append(t)

    open_positions = []
    closed_lots = []
    warnings = []
    total_realized = 0.0

    for ticker, tks in by_ticker.items():
        tks = sorted(tks, key=_sort_key)
        lots = []  # queue of [shares_remaining, price, date]
        for t in tks:
            shares = float(t.get("shares") or 0)
            price = float(t.get("price") or 0)
            side = (t.get("side") or "").lower()
            date = t.get("date")
            if shares <= 0 or price <= 0:
                warnings.append(f"Skipped a {ticker} trade with missing/invalid shares or price.")
                continue
            if side == "buy":
                lots.append([shares, price, date])
            elif side == "sell":
                remaining = shares
                while remaining > 1e-9 and lots:
                    lot_shares, lot_price, lot_date = lots[0]
                    matched = min(lot_shares, remaining)
                    realized = (price - lot_price) * matched
                    total_realized += realized
                    closed_lots.append({
                        "ticker": ticker,
                        "shares": round(matched, 4),
                        "buy_price": lot_price,
                        "sell_price": price,
                        "buy_date": lot_date,
                        "sell_date": date,
                        "realized_pnl": round(realized, 2),
                        "realized_pnl_pct": round((price - lot_price) / lot_price * 100, 2) if lot_price else None,
                    })
                    lot_shares -= matched
                    remaining -= matched
                    if lot_shares <= 1e-9:
                        lots.pop(0)
                    else:
                        lots[0][0] = lot_shares
                if remaining > 1e-9:
                    warnings.append(
                        f"Sold {remaining:g} more {ticker} shares than were on record — "
                        f"ignored the excess rather than guessing a cost basis for it."
                    )
            else:
                warnings.append(f"Skipped a {ticker} trade with an unrecognized side ({t.get('side')!r}).")

        remaining_shares = sum(l[0] for l in lots)
        if remaining_shares > 1e-9:
            cost_basis = sum(l[0] * l[1] for l in lots)
            avg_cost = cost_basis / remaining_shares
            current_price = price_lookup.get(ticker)
            unrealized = (current_price - avg_cost) * remaining_shares if current_price is not None else None
            unrealized_pct = ((current_price - avg_cost) / avg_cost * 100) if current_price is not None else None

            zone_flag, zone_note = None, None
            levels = levels_lookup.get(ticker)
            if levels and current_price is not None:
                if levels.get("at_exit_target"):
                    zone_flag, zone_note = "exit", "Price is at/above the computed sell zone."
                elif levels.get("in_entry_zone"):
                    zone_flag, zone_note = "entry", "Price is back in the computed buy zone."

            open_positions.append({
                "ticker": ticker,
                "shares": round(remaining_shares, 4),
                "avg_cost": round(avg_cost, 2),
                "cost_basis": round(cost_basis, 2),
                "current_price": current_price,
                "tracked": current_price is not None,
                "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                "unrealized_pnl_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
                "zone_flag": zone_flag,
                "zone_note": zone_note,
            })

    open_positions.sort(key=lambda p: -(p["unrealized_pnl"] or 0))
    closed_lots.sort(key=lambda c: c.get("sell_date") or "", reverse=True)

    cost_basis_total = sum(p["cost_basis"] for p in open_positions)
    tracked_value = sum(p["current_price"] * p["shares"] for p in open_positions if p["tracked"])
    tracked_cost = sum(p["cost_basis"] for p in open_positions if p["tracked"])
    unrealized_total = sum(p["unrealized_pnl"] for p in open_positions if p["unrealized_pnl"] is not None)

    return {
        "open_positions": open_positions,
        "closed_lots": closed_lots,
        "warnings": warnings,
        "totals": {
            "cost_basis": round(cost_basis_total, 2),
            "tracked_cost_basis": round(tracked_cost, 2),
            "tracked_market_value": round(tracked_value, 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "unrealized_pnl_pct": round(unrealized_total / tracked_cost * 100, 2) if tracked_cost else None,
            "realized_pnl": round(total_realized, 2),
        },
    }
