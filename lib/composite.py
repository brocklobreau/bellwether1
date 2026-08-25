"""
Combine technical / fundamental / sentiment sub-scores into one signal.
Pure math + string assembly, no network.
"""

DEFAULT_WEIGHTS = {"technical": 0.30, "fundamental": 0.35, "sentiment": 0.20, "insider": 0.15}

BUY_THRESHOLD = 63
SELL_THRESHOLD = 40


def build_signal(technical: dict, fundamental: dict, sentiment: dict, insider: dict = None, weights=None):
    weights = weights or DEFAULT_WEIGHTS
    insider = insider or {}

    parts = []
    if technical.get("technical_score") is not None:
        parts.append((weights["technical"], technical["technical_score"]))
    if fundamental.get("fundamental_score") is not None:
        parts.append((weights["fundamental"], fundamental["fundamental_score"]))
    if sentiment.get("sentiment_score") is not None:
        parts.append((weights["sentiment"], sentiment["sentiment_score"]))
    if insider.get("insider_score") is not None:
        parts.append((weights["insider"], insider["insider_score"]))

    if not parts:
        return {
            "composite_score": None,
            "signal": "NO DATA",
            "confidence": 0,
            "rationale": ["Not enough data collected to produce a signal."],
        }

    total_w = sum(w for w, _ in parts)
    composite = round(sum(w * s for w, s in parts) / total_w, 1)

    if composite >= BUY_THRESHOLD:
        signal = "BUY"
    elif composite <= SELL_THRESHOLD:
        signal = "SELL"
    else:
        signal = "HOLD"

    confidence = round(min(100, abs(composite - 50) * 2), 0)

    rationale = []
    rationale.extend(technical.get("notes", []))
    rationale.extend(fundamental.get("notes", []))
    rationale.extend(sentiment.get("notes", []))
    rationale.extend(insider.get("notes", []))

    return {
        "composite_score": composite,
        "signal": signal,
        "confidence": confidence,
        "technical_score": technical.get("technical_score"),
        "fundamental_score": fundamental.get("fundamental_score"),
        "sentiment_score": sentiment.get("sentiment_score"),
        "insider_score": insider.get("insider_score"),
        "rationale": rationale,
    }


def detect_noteworthy(previous: dict, current: dict, price_move_threshold_pct=3.0):
    """
    Compare this run's result for one ticker against the last run's, to
    decide whether it's worth a push notification.
    previous/current: dicts with at least 'signal', 'price', and
    'sentiment' (containing big_news / flagged_headlines).
    Returns (is_noteworthy: bool, reasons: list[str]).
    """
    reasons = []

    if not previous:
        return False, reasons

    prev_signal = previous.get("signal")
    cur_signal = current.get("signal")
    if prev_signal and cur_signal and prev_signal != cur_signal:
        reasons.append(f"Signal changed: {prev_signal} -> {cur_signal}.")

    prev_price = previous.get("price")
    cur_price = current.get("price")
    if prev_price and cur_price:
        move = (cur_price - prev_price) / prev_price * 100
        if abs(move) >= price_move_threshold_pct:
            reasons.append(f"Price moved {move:+.2f}% since last check (${prev_price:.2f} -> ${cur_price:.2f}).")

    if current.get("sentiment", {}).get("big_news"):
        flagged = current["sentiment"].get("flagged_headlines", [])
        if flagged:
            reasons.append("Notable headline: " + flagged[0])

    return (len(reasons) > 0), reasons
