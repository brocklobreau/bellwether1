"""
Lightweight lexicon-based sentiment scoring for news headlines. No external
NLP library (none are installable in this sandbox), just a curated
finance-flavored word list. Good enough to separate clearly-positive from
clearly-negative headlines and flag magnitude words for the "big news"
notification trigger -- not a substitute for reading the actual article.
"""
import re

POSITIVE_WORDS = {
    "beat", "beats", "beating", "surge", "surges", "surged", "soar", "soars",
    "soared", "rally", "rallies", "record", "upgrade", "upgraded", "outperform",
    "strong", "growth", "profit", "profits", "gain", "gains", "gained",
    "jump", "jumps", "jumped", "rise", "rises", "rising", "bullish",
    "buyback", "expansion", "partnership", "approval", "approved", "breakthrough",
    "raises", "raised", "guidance raised", "positive", "win", "wins", "winning",
    "best", "top", "boost", "boosts", "boosted", "exceeds", "exceeded",
}

NEGATIVE_WORDS = {
    "miss", "misses", "missed", "plunge", "plunges", "plunged", "crash",
    "crashes", "crashed", "downgrade", "downgraded", "underperform", "weak",
    "loss", "losses", "drop", "drops", "dropped", "fall", "falls", "falling",
    "bearish", "layoffs", "lawsuit", "investigation", "probe", "recall",
    "warns", "warning", "cuts", "cut", "slashed", "slump", "slumps", "slumped",
    "delay", "delayed", "concern", "concerns", "fraud", "scandal", "resign",
    "resigns", "resigned", "bankruptcy", "default", "fine", "fined", "sued",
    "worst", "decline", "declines", "declined", "slowdown",
}

# Words that suggest a headline is a bigger deal than routine daily noise --
# used only to help decide whether to fire a notification, not for the score.
MAGNITUDE_WORDS = {
    "record", "crash", "plunge", "plunges", "plunged", "soar", "soars", "soared",
    "surge", "surges", "surged", "bankruptcy", "fraud", "investigation", "probe",
    "recall", "lawsuit", "acquisition", "acquires", "merger", "resigns", "resign",
    "ceo", "fed", "rate", "guidance", "halted", "halt", "scandal", "breakthrough",
}


def score_headline(headline: str):
    words = set(re.findall(r"[a-z']+", headline.lower()))
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    magnitude = len(words & MAGNITUDE_WORDS) > 0
    if pos == 0 and neg == 0:
        return 0, magnitude
    return (pos - neg) / max(pos + neg, 1), magnitude


def score_sentiment(headlines):
    """
    headlines: list of strings (most recent first is fine, order doesn't
    matter for the score, but keep them for the notes/rationale).
    Returns dict with sentiment_score (0-100, 50=neutral), and whether any
    headline looks like "big news" worth a notification.
    """
    if not headlines:
        return {"sentiment_score": 50.0, "big_news": False, "notes": ["No recent news found."]}

    raw_scores = []
    big_news = False
    flagged = []
    for h in headlines:
        s, mag = score_headline(h)
        raw_scores.append(s)
        if mag:
            big_news = True
            flagged.append(h)

    avg = sum(raw_scores) / len(raw_scores)
    sentiment_score = round(50 + avg * 50, 1)  # map -1..1 -> 0..100

    notes = [f"Scored {len(headlines)} recent headlines; net tone "
             f"{'positive' if avg > 0.15 else 'negative' if avg < -0.15 else 'mixed/neutral'}."]
    if flagged:
        notes.append("Potentially significant headline(s): " + "; ".join(flagged[:3]))

    return {
        "sentiment_score": sentiment_score,
        "big_news": big_news,
        "flagged_headlines": flagged[:3],
        "headline_count": len(headlines),
        "notes": notes,
    }
