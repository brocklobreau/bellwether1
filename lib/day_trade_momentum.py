"""
Day-trade momentum: vs-yesterday delta + today's intraday trend, for the Day
Trade tab. Built purely from results/history/ snapshots that save_run()
already writes every run -- no network, no new data collection needed.

This answers a different question than lib.day_trade_track_record (which
grades PAST "Prime setup" calls against what happened after them): this
module is about RIGHT NOW -- is today's move building or fading over the
course of the trading day, and how does the current price/score compare to
where it closed out the prior trading day? Two different lenses, same
underlying snapshot history.

Calendar "day" here means UTC calendar date, which lines up cleanly with how
the scheduled run actually fires (13:55-20:55 UTC, one weekday at a time) --
a run never straddles midnight UTC in practice.
"""
from lib.track_record import _load_snapshots


def _day_key(ts):
    return ts[:10]  # YYYY-MM-DD


def build_momentum():
    snapshots = _load_snapshots()  # [(ts, snapshot), ...] chronological
    if not snapshots:
        return {}

    seen_days = []
    for ts, _snap in snapshots:
        dk = _day_key(ts)
        if not seen_days or seen_days[-1] != dk:
            seen_days.append(dk)

    today_key = seen_days[-1]
    prior_key = seen_days[-2] if len(seen_days) >= 2 else None

    by_ticker = {}  # ticker -> {"name", "today": [(ts, price, score)], "prior_last": (ts, price, score) | None}

    for ts, snap in snapshots:
        dk = _day_key(ts)
        if dk != today_key and dk != prior_key:
            continue
        pool = list(snap.get("watchlist_results", [])) + list(snap.get("discovered_candidates", []))
        for r in pool:
            ticker = r.get("ticker")
            price = r.get("price")
            if not ticker or price is None:
                continue
            score = r.get("day_trade_score")
            entry = by_ticker.setdefault(ticker, {"name": r.get("name"), "today": [], "prior_last": None})
            if dk == today_key:
                entry["today"].append((ts, price, score))
            else:
                entry["prior_last"] = (ts, price, score)  # snapshots are chronological -- last write wins = prior day's final snapshot

    result = {}
    for ticker, data in by_ticker.items():
        today_points = sorted(data["today"], key=lambda p: p[0])
        prior_last = data["prior_last"]
        out = {
            "name": data["name"],
            "has_prior_day": prior_last is not None,
            "intraday_points": today_points,
            "vs_prior_close_price_pct": None,
            "vs_prior_close_score": None,
            "intraday_price_change_pct": None,
            "intraday_score_change": None,
        }
        if len(today_points) >= 2:
            first_price, first_score = today_points[0][1], today_points[0][2]
            last_price, last_score = today_points[-1][1], today_points[-1][2]
            if first_price:
                out["intraday_price_change_pct"] = round((last_price - first_price) / first_price * 100, 2)
            if first_score is not None and last_score is not None:
                out["intraday_score_change"] = round(last_score - first_score, 1)
        if prior_last and today_points:
            prior_price, prior_score = prior_last[1], prior_last[2]
            last_price, last_score = today_points[-1][1], today_points[-1][2]
            if prior_price:
                out["vs_prior_close_price_pct"] = round((last_price - prior_price) / prior_price * 100, 2)
            if prior_score is not None and last_score is not None:
                out["vs_prior_close_score"] = round(last_score - prior_score, 1)
        result[ticker] = out
    return result
