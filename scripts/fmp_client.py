"""
Real HTTP client for the Financial Modeling Prep (FMP) `/stable/` API.

This replaces the old WebFetch-based data collection described in
RUNBOOK.md. WebFetch summarizes every response through a small LLM before
handing it back, which turned out to occasionally mangle numbers on bulk
requests (confirmed wrong closes for JNJ, AMZN, V, MU, AVGO on 2026-08-24 --
see RUNBOOK.md's "Notes / known limitations"). A plain `requests.get()` call
parses the JSON directly with no summarization step in between, so that
whole class of error can't happen here. It also means this script can run
completely unattended (a GitHub Actions cron job), instead of depending on
an interactive Claude session that turned out to fail silently for days at
a time (see RUNBOOK.md's "Silent scheduled-run failure" note).

Every function returns already-parsed JSON (a dict or list) or raises
FMPError with a message that says plainly what went wrong -- callers should
let that surface rather than silently substituting guessed data.
"""
import collections
import os
import time
import requests

BASE_URL = "https://financialmodelingprep.com/stable"
TIMEOUT = 20
MAX_RETRIES = 3

# Proactive pacing, not just reactive 429 retries -- added 2026-08-27 when the
# day-trade/investing screeners started pulling much bigger candidate pools
# (dozens of tickers x several endpoints each per cycle). Stays safely under
# a 300-calls/min plan limit instead of finding the ceiling by tripping it.
RATE_LIMIT_PER_MIN = 250
_call_times = collections.deque()


def _throttle():
    now = time.time()
    while _call_times and now - _call_times[0] > 60:
        _call_times.popleft()
    if len(_call_times) >= RATE_LIMIT_PER_MIN:
        sleep_for = 60 - (now - _call_times[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)
    _call_times.append(time.time())


class FMPError(RuntimeError):
    pass


def _api_key():
    key = os.environ.get("FMP_API_KEY")
    if not key:
        # Local/manual fallback -- never commit this file's contents to git.
        local_key_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "fmp_api_key.txt",
        )
        if os.path.exists(local_key_path):
            with open(local_key_path) as f:
                key = f.read().strip()
    if not key:
        raise FMPError(
            "No FMP API key found. Set the FMP_API_KEY environment variable "
            "(a GitHub Actions secret in CI) or create config/fmp_api_key.txt locally."
        )
    return key


def _get(path, params=None):
    params = dict(params or {})
    params["apikey"] = _api_key()
    url = f"{BASE_URL}/{path}"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.5 * attempt)
            continue
        if resp.status_code == 401 or resp.status_code == 403:
            raise FMPError(
                f"FMP returned {resp.status_code} for {path} (symbol={params.get('symbol')}) "
                "-- the API key may be invalid, expired, or over its plan/rate limit."
            )
        if resp.status_code == 429:
            last_err = FMPError(f"FMP rate-limited {path} (429)")
            time.sleep(2.0 * attempt)
            continue
        if resp.status_code >= 500:
            last_err = FMPError(f"FMP returned {resp.status_code} for {path}")
            time.sleep(1.5 * attempt)
            continue
        if resp.status_code != 200:
            raise FMPError(f"FMP returned {resp.status_code} for {path}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise FMPError(f"FMP response for {path} wasn't valid JSON: {e}")
    raise FMPError(f"FMP request for {path} failed after {MAX_RETRIES} attempts: {last_err}")


def quote(symbol):
    data = _get("quote", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else (data or {})


def ratios(symbol):
    data = _get("ratios", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else {}


def financial_growth(symbol):
    data = _get("financial-growth", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else {}


def price_target_consensus(symbol):
    data = _get("price-target-consensus", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else {}


def grades_consensus(symbol):
    data = _get("grades-consensus", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else {}


def profile(symbol):
    data = _get("profile", {"symbol": symbol})
    return data[0] if isinstance(data, list) and data else {}


def earnings(symbol):
    data = _get("earnings", {"symbol": symbol})
    return data if isinstance(data, list) else []


def insider_trading_search(symbol, limit=15):
    data = _get("insider-trading/search", {"symbol": symbol, "limit": limit})
    return data if isinstance(data, list) else []


def news_stock(symbol, limit=10):
    data = _get("news/stock", {"symbols": symbol, "limit": limit})
    return data if isinstance(data, list) else []


def historical_price_full(symbol, from_date, to_date):
    """Returns records newest-first, each with at least date/close/volume."""
    data = _get("historical-price-eod/full", {"symbol": symbol, "from": from_date, "to": to_date})
    if isinstance(data, dict) and "historical" in data:
        data = data["historical"]
    return data if isinstance(data, list) else []


def company_screener(**params):
    data = _get("company-screener", params)
    return data if isinstance(data, list) else []


def most_actives():
    data = _get("most-actives")
    return data if isinstance(data, list) else []


def biggest_gainers():
    data = _get("biggest-gainers")
    return data if isinstance(data, list) else []


def biggest_losers():
    data = _get("biggest-losers")
    return data if isinstance(data, list) else []


def shares_float_all(page=0, limit=1000):
    """Bulk float-shares listing (up to `limit` symbols per page) -- used to
    rank day-trade candidates by float instead of fetching float one symbol
    at a time (added 2026-08-27 for the low-float day-trade screener)."""
    data = _get("shares-float-all", {"page": page, "limit": limit})
    return data if isinstance(data, list) else []
