"""
Earnings-date risk flag for day-trade picks. A stock can be a textbook
technical setup and still gap double digits overnight on an unrelated
earnings surprise -- none of the technical/volume/momentum scoring has any
visibility into that. This flags it explicitly so a setup with real
earnings risk isn't mistaken for a clean technical read, and so a backtest
can tell "the system's technicals were wrong" apart from "an earnings gap
blew through the stop, which no technical score could have predicted."

Pure date math -- the actual next-earnings date comes from FMP's
/stable/earnings endpoint (see RUNBOOK.md step 1j) and is passed in here,
not fetched by this module.
"""
from datetime import datetime, date

IMMINENT_DAYS = 1   # today or tomorrow -- treat as maximum caution
SOON_DAYS = 5        # within a work week -- worth a heads-up


def classify_earnings_risk(next_earnings_date, today=None):
    """next_earnings_date: "YYYY-MM-DD" string or None/unknown.
    Returns {"next_earnings_date", "days_until", "flag", "note"} --
    flag is "imminent", "soon", or None (nothing to flag: date unknown,
    already passed, or safely far away)."""
    empty = {"next_earnings_date": None, "days_until": None, "flag": None, "note": None}
    if not next_earnings_date:
        return empty
    try:
        ed = datetime.strptime(next_earnings_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return empty

    today = today or date.today()
    days_until = (ed - today).days

    if days_until < 0:
        # stale/past date slipped through -- nothing to warn about
        return {"next_earnings_date": next_earnings_date, "days_until": days_until, "flag": None, "note": None}

    if days_until <= IMMINENT_DAYS:
        flag = "imminent"
        when = "today" if days_until == 0 else "tomorrow"
        note = f"Earnings {when} — a move from here could be the earnings reaction, not this setup playing out."
    elif days_until <= SOON_DAYS:
        flag = "soon"
        note = f"Earnings in {days_until} days — this setup may not have time to resolve before then."
    else:
        flag, note = None, None

    return {"next_earnings_date": next_earnings_date, "days_until": days_until, "flag": flag, "note": note}
