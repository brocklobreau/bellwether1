"""
Is Alpaca's news stream actually real-time, and actually free?

The entire "news site" idea rests on one number: how long after a story is
published can we see it. FMP, measured properly on 2026-09-15, was 10.6
minutes at its fastest. Newsquawk is sub-second. If Alpaca lands near FMP
there is no project; if it lands near zero there is.

This measures it the only way that is honest -- by holding a live socket
open and timestamping each headline the MOMENT it arrives, then comparing
that to the article's own created_at. Unlike polling a "latest" endpoint,
this cannot confuse "the feed is slow" with "nothing was published
recently", because arrival time is observed directly rather than inferred.

CREDENTIALS NEVER TOUCH THIS FILE OR THE REPOSITORY. They are read from the
ALPACA_KEY_ID / ALPACA_SECRET_KEY environment variables, which on Render are
set in the dashboard under Environment. A key pasted into a chat window or
committed to git is a key that has to be rotated; there is no reason to
create that problem for a measurement.

Use PAPER trading keys. They cannot move real money, and this probe only
ever reads.
"""
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_PATH = os.path.join(BASE, "results", "alpaca_probe.json")

NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"
NEWS_REST = "https://data.alpaca.markets/v1beta1/news"

LISTEN_SECONDS = 600          # latency needs one headline; VOLUME needs many.
                              # The first run answered latency (0.23s) off a single
                              # article in 150s -- decisive for speed, useless for
                              # throughput. A news site with a trickle of stories is
                              # an empty news site, so this window is long enough to
                              # count headlines per minute as well.
MAX_SAMPLES = 300
PROBE_VERSION = 2


def _creds():
    kid = os.environ.get("ALPACA_KEY_ID") or os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    return kid, sec


def _parse_rfc3339(v):
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rest_check(kid, sec, log):
    """Shape, sources and recency of the REST feed -- cheap, and tells us
    whether the credentials work at all before opening a socket."""
    import requests
    out = {"ok": False}
    try:
        r = requests.get(NEWS_REST, params={"limit": 50},
                         headers={"APCA-API-KEY-ID": kid,
                                  "APCA-API-SECRET-KEY": sec}, timeout=20)
        out["status"] = r.status_code
        if r.status_code == 403 or r.status_code == 401:
            out["error"] = "credentials rejected (401/403)"
            log(f"  REST: {r.status_code} -- credentials rejected. Check the key pair "
                f"and that they are PAPER keys from the same account.")
            return out
        if r.status_code != 200:
            out["error"] = r.text[:200]
            log(f"  REST: HTTP {r.status_code} -- {r.text[:120]}")
            return out
        body = r.json() or {}
        items = body.get("news") or []
        out["ok"] = True
        out["returned"] = len(items)
        now = datetime.now(timezone.utc)
        ages, sources = [], {}
        for a in items:
            dt = _parse_rfc3339(a.get("created_at"))
            if dt:
                ages.append((now - dt).total_seconds() / 60.0)
            s = a.get("source") or "?"
            sources[s] = sources.get(s, 0) + 1
        if items:
            out["fields"] = sorted(items[0].keys())
            out["has_symbols"] = bool(items[0].get("symbols"))
        if ages:
            ages.sort()
            out["freshest_min"] = round(ages[0], 2)
            out["median_min"] = round(ages[len(ages) // 2], 2)
        out["sources"] = sources
        log(f"  REST: OK, {len(items)} articles, freshest {out.get('freshest_min')} min old, "
            f"sources {sources}")
    except Exception as e:
        out["error"] = str(e)[:200]
        log(f"  REST: failed -- {str(e)[:140]}")
    return out


def _ws_check(kid, sec, log):
    """The real measurement: arrival time vs the article's own timestamp."""
    out = {"ok": False, "samples_sec": [], "articles": 0}
    try:
        from websocket import create_connection
    except ImportError:
        out["error"] = "websocket-client not installed"
        log("  WS: websocket-client is not installed -- add it to requirements.txt")
        return out

    ws = None
    try:
        ws = create_connection(NEWS_WS, timeout=20,
                               sslopt={"cert_reqs": ssl.CERT_REQUIRED})
        hello = ws.recv()
        out["hello"] = str(hello)[:200]

        ws.send(json.dumps({"action": "auth", "key": kid, "secret": sec}))
        auth = ws.recv()
        out["auth_reply"] = str(auth)[:200]
        if "authenticated" not in str(auth):
            out["error"] = "not authenticated"
            log(f"  WS: authentication failed -- {str(auth)[:160]}")
            return out

        ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        sub = ws.recv()
        out["subscribe_reply"] = str(sub)[:200]
        log(f"  WS: connected and subscribed to all news; listening {LISTEN_SECONDS}s...")

        ws.settimeout(10)
        deadline = time.time() + LISTEN_SECONDS
        samples, seen_symbols = [], 0
        timeouts, errors, other_types = 0, 0, {}
        while time.time() < deadline and len(samples) < MAX_SAMPLES:
            try:
                raw = ws.recv()
            except Exception as e:
                # A recv timeout means the feed is simply quiet and we keep
                # waiting. Anything ELSE means the socket is broken, and the
                # original version of this loop treated both the same -- so a
                # dead connection would spin silently for the whole window and
                # report "quiet feed", which is a completely different finding.
                name = type(e).__name__.lower()
                if "timeout" in name:
                    timeouts += 1
                    continue
                errors += 1
                out["recv_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                if errors >= 3:
                    log(f"  WS: socket errored {errors}x ({out['recv_error']}) -- stopping")
                    break
                continue
            arrived = datetime.now(timezone.utc)
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msgs, dict):
                msgs = [msgs]
            for m in msgs:
                if m.get("T") != "n":
                    t = str(m.get("T"))
                    other_types[t] = other_types.get(t, 0) + 1
                    continue
                out["articles"] += 1
                if m.get("symbols"):
                    seen_symbols += 1
                created = _parse_rfc3339(m.get("created_at"))
                if created:
                    lag = (arrived - created).total_seconds()
                    # Guard against clock skew rather than averaging it in.
                    if -60 <= lag <= 3600:
                        samples.append(round(lag, 2))
                if out["articles"] <= 3:
                    log(f"    sample headline: {str(m.get('headline'))[:88]!r} "
                        f"[{', '.join(m.get('symbols') or []) or 'no tickers'}] "
                        f"source={m.get('source')}")
        out["ok"] = True
        out["samples_sec"] = samples
        out["with_symbols"] = seen_symbols
        out["listen_seconds"] = LISTEN_SECONDS
        out["idle_timeouts"] = timeouts
        out["recv_errors"] = errors
        out["non_news_messages"] = other_types
        out["headlines_per_min"] = round(out["articles"] / (LISTEN_SECONDS / 60.0), 2)
    except Exception as e:
        out["error"] = str(e)[:200]
        log(f"  WS: failed -- {str(e)[:160]}")
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
    return out


def probe(log=print):
    kid, sec = _creds()
    if not kid or not sec:
        log("alpaca probe: ALPACA_KEY_ID / ALPACA_SECRET_KEY not set -- skipping. "
            "Add them in Render under Environment (never in the repo).")
        return None

    log("alpaca probe: checking whether the news stream is real-time and accessible")
    out = {"probed_at": datetime.now(timezone.utc).isoformat(),
           "probe_version": PROBE_VERSION}
    out["rest"] = _rest_check(kid, sec, log)
    out["ws"] = _ws_check(kid, sec, log)

    s = out["ws"].get("samples_sec") or []
    if s:
        srt = sorted(s)
        out["latency"] = {
            "n": len(srt),
            "fastest_sec": srt[0],
            "p10_sec": srt[int(len(srt) * 0.10)],
            "median_sec": srt[len(srt) // 2],
            "slowest_sec": srt[-1],
        }
        lat = out["latency"]
        log(f"alpaca probe VERDICT: {out['ws']['articles']} headlines in "
            f"{LISTEN_SECONDS}s; delivery latency fastest {lat['fastest_sec']}s, "
            f"median {lat['median_sec']}s "
            f"({out['ws'].get('with_symbols', 0)} carried ticker tags)")
        log(f"  For comparison, FMP measured 636s (10.6 min) at its fastest.")
        w = out["ws"]
        log(f"  VOLUME: {w.get('headlines_per_min')} headlines/min over "
            f"{w.get('listen_seconds')}s ({w['articles']} total, "
            f"{w.get('with_symbols', 0)} with tickers). "
            f"{w.get('idle_timeouts', 0)} idle waits, {w.get('recv_errors', 0)} socket errors, "
            f"other message types: {w.get('non_news_messages') or 'none'}")
    else:
        log("alpaca probe VERDICT: no headlines captured. Either the market is "
            "quiet right now, the subscription was rejected, or news is gated "
            "on this account -- check the auth/subscribe replies in the JSON.")

    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    return out


if __name__ == "__main__":
    probe()
