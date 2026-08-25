"""
Position sizing for day-trade picks: given the realistic entry/stop from
lib.price_levels.compute_day_trade_levels ("entry_ref"/"stop") and the
user's risk settings (config/risk_settings.json), compute how many shares
would risk exactly the configured percent of the account if the stop is
hit. Pure math, no network, no execution -- this is a sizing SUGGESTION to
size a paper-trading backtest with, not an order, and it never touches a
real or paper brokerage on its own.

Why this exists: the day-trade score/levels tell you WHAT to trade: this
tells you HOW MUCH, which is the piece a backtest actually needs to turn
"the system called this trade right" into "the system made/lost $X" --
without it, a backtest can only report win rate, not account-level P&L.
"""
import json
import math
import os

from lib.composite import BUY_THRESHOLD

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(BASE, "config", "risk_settings.json")

DEFAULTS = {"account_size": 100000, "risk_per_trade_pct": 1}

# Investing allocation is a DIFFERENT sizing model than the day-trade one
# above -- a long-hold stop is ~8% away and meant to rarely get hit, so
# sizing off risk-to-stop the way day trades do would produce oversized
# positions. Instead this scales conviction (composite_score) linearly
# between a starter position at the BUY threshold and a single-position
# ceiling at a composite of 100.
INVESTING_MIN_ALLOCATION_PCT = 3
INVESTING_MAX_ALLOCATION_PCT = 12


def load_risk_settings():
    if not os.path.exists(SETTINGS_PATH):
        return dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return {
            "account_size": data.get("account_size", DEFAULTS["account_size"]),
            "risk_per_trade_pct": data.get("risk_per_trade_pct", DEFAULTS["risk_per_trade_pct"]),
        }
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)


def compute_position_size(entry_ref, stop, account_size=None, risk_per_trade_pct=None):
    """entry_ref/stop come straight from compute_day_trade_levels (works for
    both long and short -- stop is already on the correct side either way,
    so risk_per_share = abs(entry_ref - stop) is direction-agnostic).

    Two independent caps, take whichever is more conservative:
      - risk cap: shares such that shares * risk_per_share == the account's
        risk budget (the whole point of the feature)
      - cash cap: shares such that shares * entry_ref <= account_size --
        this account is modeled as cash/no-margin, so a tight stop can never
        imply buying more stock than the account actually holds. Without
        this, a $1.50-wide stop on a $100 stock at 5% risk on a $100k
        account "sizes" to 3,333 shares (~$333k) -- more than 3x the whole
        account, which cannot actually be bought."""
    settings = load_risk_settings()
    account_size = account_size if account_size is not None else settings["account_size"]
    risk_pct = risk_per_trade_pct if risk_per_trade_pct is not None else settings["risk_per_trade_pct"]

    if entry_ref is None or stop is None or not account_size or entry_ref <= 0:
        return None
    risk_per_share = abs(entry_ref - stop)
    # Guard against a near-zero (but not exactly zero) risk_per_share -- e.g.
    # a one-cent-wide stop -- which would otherwise imply an absurd share
    # count before the cash cap even gets a chance to apply cleanly.
    if risk_per_share < 0.01:
        return {
            "shares": 0, "dollar_risk": 0.0, "position_value": 0.0, "pct_of_account": 0.0,
            "risk_per_share": round(risk_per_share, 4),
            "account_size": account_size, "risk_per_trade_pct": risk_pct,
            "note": "Entry and stop are essentially the same price -- too tight to size meaningfully.",
        }

    dollar_risk_budget = account_size * risk_pct / 100
    risk_cap_shares = math.floor(dollar_risk_budget / risk_per_share)
    cash_cap_shares = math.floor(account_size / entry_ref)
    shares = max(0, min(risk_cap_shares, cash_cap_shares))

    if shares <= 0:
        reason = ("too far to size even 1 share within the risk budget" if risk_cap_shares <= 0
                   else "the account can't afford even 1 share at this price")
        return {
            "shares": 0, "dollar_risk": 0.0, "position_value": 0.0, "pct_of_account": 0.0,
            "risk_per_share": round(risk_per_share, 2),
            "account_size": account_size, "risk_per_trade_pct": risk_pct,
            "note": f"Stop is ${risk_per_share:,.2f}/share away -- {reason} ({risk_pct:g}% of ${account_size:,.0f}).",
        }

    dollar_risk = round(shares * risk_per_share, 2)
    position_value = round(shares * entry_ref, 2)
    pct_of_account = round(position_value / account_size * 100, 1)
    cash_capped = cash_cap_shares < risk_cap_shares

    note = (f"{shares:,} sh (~${position_value:,.0f}, {pct_of_account:.1f}% of account) risks "
            f"${dollar_risk:,.0f} ({risk_pct:g}% of account) if the stop is hit.")
    if cash_capped:
        note += " Capped by account size, not the risk budget -- a wider stop than usual for this price."

    return {
        "shares": shares,
        "dollar_risk": dollar_risk,
        "position_value": position_value,
        "pct_of_account": pct_of_account,
        "risk_per_share": round(risk_per_share, 2),
        "account_size": account_size,
        "risk_per_trade_pct": risk_pct,
        "cash_capped": cash_capped,
        "note": note,
    }


def compute_investing_allocation(composite_score, price, account_size=None):
    """Conviction-weighted target position size for a long-horizon BUY
    candidate. Returns None for anything that isn't currently a BUY (score
    below lib.composite.BUY_THRESHOLD) -- this is a sizing suggestion for a
    candidate worth buying, not a generic calculator."""
    settings = load_risk_settings()
    account_size = account_size if account_size is not None else settings["account_size"]

    if composite_score is None or price is None or price <= 0 or not account_size:
        return None
    if composite_score < BUY_THRESHOLD:
        return None

    span = 100 - BUY_THRESHOLD
    frac = (composite_score - BUY_THRESHOLD) / span if span > 0 else 1.0
    frac = max(0.0, min(1.0, frac))
    target_pct = INVESTING_MIN_ALLOCATION_PCT + frac * (INVESTING_MAX_ALLOCATION_PCT - INVESTING_MIN_ALLOCATION_PCT)
    target_value = account_size * target_pct / 100
    shares = math.floor(target_value / price)

    if shares <= 0:
        return {
            "target_pct": round(target_pct, 1), "shares": 0, "position_value": 0.0, "actual_pct": 0.0,
            "account_size": account_size,
            "note": (f"Target allocation ~{target_pct:.1f}% (${target_value:,.0f}) is less than one share "
                     f"at ${price:,.2f}."),
        }

    position_value = round(shares * price, 2)
    actual_pct = round(position_value / account_size * 100, 1)

    return {
        "target_pct": round(target_pct, 1),
        "shares": shares,
        "position_value": position_value,
        "actual_pct": actual_pct,
        "account_size": account_size,
        "note": f"Conviction-sized target: ~{actual_pct:.1f}% of account ({shares:,} sh, ~${position_value:,.0f}).",
    }
