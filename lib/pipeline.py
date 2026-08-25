"""
Single entry point that ties the scoring modules together for one ticker.
No network calls -- all inputs must already be fetched/parsed (see RUNBOOK.md).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.indicators import score_technical
from lib.fundamentals import score_fundamentals
from lib.sentiment import score_sentiment
from lib.insider_signal import score_insider
from lib.day_trade_score import score_day_trade_setup
from lib.composite import build_signal
from lib.price_levels import compute_investing_levels, compute_day_trade_levels
from lib.checklist import day_trade_checklist, investing_checklist
from lib.position_sizing import compute_position_size, compute_investing_allocation
from lib.earnings_risk import classify_earnings_risk


def score_ticker(ticker, name, price, closes_oldest_first, fund_input,
                  price_52w_low=None, price_52w_high=None, headlines=None,
                  insider_filings=None, volume=None, avg_volume=None,
                  next_earnings_date=None):
    closes = closes_oldest_first
    tech = score_technical(closes, price_52w_low=price_52w_low, price_52w_high=price_52w_high)
    fund = score_fundamentals(fund_input or {})
    sent = score_sentiment(headlines or [])
    insider = score_insider(insider_filings)
    sig = build_signal(tech, fund, sent, insider)

    day_trade_setup = score_day_trade_setup(tech, sent, fund.get("market_cap_usd"), volume=volume, avg_volume=avg_volume)

    invest_levels = compute_investing_levels(
        closes, price,
        sma20=tech.get("sma20"), sma50=tech.get("sma50"),
        price_52w_low=price_52w_low, price_52w_high=price_52w_high,
        analyst_upside_pct=fund.get("analyst_upside_pct"),
    )
    day_levels = compute_day_trade_levels(
        closes, price, volatility_pct=tech.get("volatility_pct"), direction=day_trade_setup["direction"],
    )
    day_levels["position_size"] = compute_position_size(day_levels.get("entry_ref"), day_levels.get("stop"))
    invest_levels["allocation"] = compute_investing_allocation(sig.get("composite_score"), price)
    earnings_risk = classify_earnings_risk(next_earnings_date)

    day_trade = day_trade_checklist(tech, fund, sent, price)
    investing = investing_checklist(tech, fund, sent, price, invest_levels)
    return {
        "ticker": ticker,
        "name": name,
        "price": price,
        "sector": fund.get("sector"),
        "technical": tech,
        "fundamental": fund,
        "sentiment": sent,
        "insider": insider,
        "price_levels": invest_levels,       # kept for the Overview tab
        "investing_levels": invest_levels,   # same data, explicit name for the Investing tab
        "day_trade_levels": day_levels,
        "day_trade_checklist": day_trade,
        "investing_checklist": investing,
        "day_trade_score": day_trade_setup["day_trade_score"],
        "day_trade_rating": day_trade_setup["day_trade_rating"],
        "day_trade_direction": day_trade_setup["direction"],
        "day_trade_setup_notes": day_trade_setup["notes"],
        "risk_reward_ratio": day_levels.get("risk_reward_ratio"),
        "position_size": day_levels.get("position_size"),
        "investing_allocation": invest_levels.get("allocation"),
        "earnings_risk": earnings_risk,
        **sig,
    }
