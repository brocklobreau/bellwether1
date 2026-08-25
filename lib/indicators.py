"""
Pure-math technical indicators. No network calls — operates on price series
that have already been fetched (via WebFetch) and parsed into plain lists.

All price series are expected OLDEST -> NEWEST.
"""
from statistics import mean


def sma(closes, window):
    if len(closes) < window:
        return None
    return mean(closes[-window:])


def ema_series(closes, window):
    """Return the full EMA series (same length as closes, None for the
    warm-up period) using the standard smoothing formula."""
    if len(closes) < window:
        return [None] * len(closes)
    k = 2 / (window + 1)
    out = [None] * (window - 1)
    seed = mean(closes[:window])
    out.append(seed)
    prev = seed
    for price in closes[window:]:
        val = price * k + prev * (1 - k)
        out.append(val)
        prev = val
    return out


def ema(closes, window):
    series = ema_series(closes, window)
    return series[-1] if series else None


def rsi(closes, window=14):
    """Standard Wilder RSI, 0-100. Needs window+1 closes minimum."""
    if len(closes) < window + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = mean(gains[:window])
    avg_loss = mean(losses[:window])
    for i in range(window, len(gains)):
        avg_gain = (avg_gain * (window - 1) + gains[i]) / window
        avg_loss = (avg_loss * (window - 1) + losses[i]) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram) for the most recent point,
    or None values if there isn't enough data."""
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_vals = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_vals.append(None)
        else:
            macd_vals.append(f - s)
    clean = [v for v in macd_vals if v is not None]
    if len(clean) < signal:
        return None, None, None
    sig_series = ema_series(clean, signal)
    macd_line = clean[-1]
    signal_line = sig_series[-1]
    hist = macd_line - signal_line if signal_line is not None else None
    return round(macd_line, 3), round(signal_line, 3) if signal_line else None, round(hist, 3) if hist is not None else None


def pct_change(closes, periods):
    if len(closes) <= periods:
        return None
    return round((closes[-1] - closes[-1 - periods]) / closes[-1 - periods] * 100, 2)


def avg_daily_move_pct(closes, window=20):
    """Average absolute day-to-day % change over the window -- a simple,
    intuitive volatility proxy (higher = moves more, relevant for day trading)."""
    if not closes or len(closes) < 2:
        return None
    w = closes[-(window + 1):] if len(closes) > window else closes
    moves = [abs(w[i] - w[i - 1]) / w[i - 1] * 100 for i in range(1, len(w)) if w[i - 1]]
    if not moves:
        return None
    return round(mean(moves), 2)


def recent_range(closes, window=20):
    """(low, high) over the trailing window -- used as near-term support/resistance."""
    if not closes:
        return None, None
    w = closes[-window:] if len(closes) >= window else closes
    return min(w), max(w)


def range_position(price, low_52w, high_52w):
    """Where the current price sits within its 52-week range, 0-100."""
    if not low_52w or not high_52w or high_52w <= low_52w:
        return None
    pos = (price - low_52w) / (high_52w - low_52w) * 100
    return round(max(0, min(100, pos)), 1)


def score_technical(closes, price_52w_low=None, price_52w_high=None):
    """
    closes: list of daily closes, oldest -> newest, most recent = current price.
    Returns a dict with sub-indicators, a 0-100 technical_score, and notes
    explaining what drove the score.
    """
    if not closes or len(closes) < 15:
        return {
            "technical_score": None,
            "notes": ["Not enough price history fetched to compute technicals."],
        }

    price = closes[-1]
    notes = []
    points = []  # list of (weight, score_0_100, label)

    r = rsi(closes, 14)
    if r is not None:
        if r < 30:
            rsi_score = 85 + (30 - r)  # deeper oversold = stronger bullish tilt
            notes.append(f"RSI {r} — oversold, often a bullish reversal signal.")
        elif r > 70:
            rsi_score = 15 - (r - 70)
            notes.append(f"RSI {r} — overbought, often precedes a pullback.")
        else:
            # neutral zone: map 30-70 onto a mild score around 50
            rsi_score = 50 + (50 - r) * 0.4
            notes.append(f"RSI {r} — neutral momentum.")
        rsi_score = max(0, min(100, rsi_score))
        points.append((0.30, rsi_score, "RSI"))

    m_line, m_signal, m_hist = macd(closes)
    if m_hist is not None:
        if m_hist > 0 and m_line > 0:
            macd_score = 70
            notes.append("MACD bullish: line above signal and above zero.")
        elif m_hist > 0:
            macd_score = 60
            notes.append("MACD turning up: histogram positive.")
        elif m_hist < 0 and m_line < 0:
            macd_score = 30
            notes.append("MACD bearish: line below signal and below zero.")
        else:
            macd_score = 40
            notes.append("MACD turning down: histogram negative.")
        points.append((0.25, macd_score, "MACD"))

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50) if len(closes) >= 50 else None
    if sma20:
        above20 = price > sma20
        trend_score = 60 if above20 else 40
        if sma50:
            golden = sma20 > sma50
            trend_score += 15 if golden else -15
            notes.append(
                f"Price is {'above' if above20 else 'below'} its 20-day average, "
                f"and the 20-day is {'above' if golden else 'below'} the 50-day "
                f"({'bullish' if golden else 'bearish'} trend alignment)."
            )
        else:
            notes.append(f"Price is {'above' if above20 else 'below'} its 20-day average.")
        trend_score = max(0, min(100, trend_score))
        points.append((0.25, trend_score, "Trend/MAs"))

    mom10 = pct_change(closes, 10)
    if mom10 is not None:
        mom_score = 50 + max(-30, min(30, mom10 * 3))
        notes.append(f"10-day momentum: {mom10:+.2f}%.")
        points.append((0.10, mom_score, "Momentum"))

    rp = range_position(price, price_52w_low, price_52w_high)
    if rp is not None:
        # near the low of the 52w range scores higher (potential value/bounce),
        # near the high scores lower (extended) -- mild weight, contrarian tilt
        range_score = 100 - rp
        range_score = 40 + (range_score - 50) * 0.3  # dampen, center near 40-60
        notes.append(f"Trading at {rp}% of its 52-week range (0%=52w low, 100%=52w high).")
        points.append((0.10, range_score, "52w range position"))

    if not points:
        return {"technical_score": None, "notes": ["Insufficient data for technical scoring."]}

    total_weight = sum(w for w, _, _ in points)
    technical_score = round(sum(w * s for w, s, _ in points) / total_weight, 1)

    volatility = avg_daily_move_pct(closes, 20)
    low20, high20 = recent_range(closes, 20)

    return {
        "technical_score": technical_score,
        "rsi": r,
        "macd_line": m_line,
        "macd_signal": m_signal,
        "macd_hist": m_hist,
        "sma20": round(sma20, 2) if sma20 else None,
        "sma50": round(sma50, 2) if sma50 else None,
        "momentum_10d_pct": mom10,
        "range_position_pct": rp,
        "volatility_pct": volatility,
        "recent_low_20d": round(low20, 2) if low20 else None,
        "recent_high_20d": round(high20, 2) if high20 else None,
        "notes": notes,
    }
