#!/usr/bin/env python3
"""
Whale + mispricing alert scanner -> Whop forums.
$0 stack: Polymarket + Kalshi public APIs (no keys), GitHub Actions cron, Whop GraphQL.

Runs every 10 minutes:
  1. Pulls recent whale trades from Polymarket (primary) and Kalshi (best-effort).
  2. Detects: mega prints, whale clusters, new-whale wallets, momentum, cross-venue mispricing.
  3. Posts alerts to your Whop forum via the Whop GraphQL API.
  4. Logs every alert to alerts_log.jsonl for the weekly track-record grader.

Usage:
  python scanner.py                 # normal run
  python scanner.py --dry-run       # print alerts, don't post
  python scanner.py --test-post     # post "bot connected" to verify Whop wiring
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone


# ---------------------------------------------------------------- config ---

def env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except Exception:
        return default


WHOP_API_KEY    = env("WHOP_API_KEY")
WHOP_USER_ID    = env("WHOP_USER_ID")            # auto-discovered via viewer query if empty
WHOP_COMPANY_ID = env("WHOP_COMPANY_ID")         # optional
ALERTS_EXP_ID   = env("WHOP_ALERTS_EXP_ID")      # forum experience id, exp_...
TRACK_EXP_ID    = env("WHOP_TRACK_EXP_ID")       # optional second forum for track record

POLY_FLOOR      = env("POLY_WHALE_FLOOR_USD", 5000, float)
POLY_MEGA       = env("POLY_MEGA_FLOOR_USD", 10000, float)
KALSHI_FLOOR    = env("KALSHI_WHALE_FLOOR_USD", 5000, float)
CLUSTER_MIN     = env("CLUSTER_WINDOW_MIN", 20, int)
CLUSTER_N       = env("CLUSTER_COUNT", 3, int)
CLUSTER_FLOOR   = env("CLUSTER_FLOOR_USD", 5000, float)
MOM_PCT         = env("MOMENTUM_PCT", 4.0, float)
MOM_MIN         = env("MOMENTUM_WINDOW_MIN", 60, int)
MISPRICING_C    = env("MISPRICING_MIN_CENTS", 4.0, float)

STATE_PATH = env("STATE_PATH", "state.json")
MAP_PATH   = env("MARKETS_MAP_PATH", "markets_map.json")
LOG_PATH   = env("ALERTS_LOG_PATH", "alerts_log.jsonl")
DRY_RUN    = env("DRY_RUN", "0") == "1"

WHOP_GQL     = "https://api.whop.com/public-graphql"
POLY_TRADES  = "https://data-api.polymarket.com/v2/trades"
POLY_GAMMA   = "https://gamma-api.polymarket.com/markets"
KALSHI_HOSTS = ["https://external-api.kalshi.com", "https://api.elections.kalshi.com"]

UA = {"User-Agent": "whale-alert-bot/1.0 (+github-actions)"}


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- http -----

def http_get(url, params=None, timeout=25, retries=3):
    """GET with exponential backoff on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = __import__("requests").get(url, params=params, headers=UA, timeout=timeout)
        except Exception as e:
            log(f"GET failed ({e}), retry {attempt + 1}/{retries}: {url}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            log(f"GET {r.status_code}, backing off {wait}s: {url}")
            time.sleep(wait)
            continue
        log(f"GET {r.status_code} (no retry): {url} :: {r.text[:200]}")
        return None
    return None


# ---------------------------------------------------------------- state ----

def load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("poly_last_ts", int(time.time()) - 600)
    s.setdefault("kalshi_last_ts", int(time.time()) - 600)
    s.setdefault("seen_ids", [])
    s.setdefault("seen_wallets", [])
    s.setdefault("market_px", {})   # market_key -> [ts, price]  (momentum memory)
    return s


def save_state(s):
    s["seen_ids"] = s["seen_ids"][-5000:]
    s["seen_wallets"] = s["seen_wallets"][-2000:]
    # prune old market prices (keep 3h)
    cutoff = time.time() - 3 * 3600
    s["market_px"] = {k: v for k, v in s["market_px"].items() if v[0] > cutoff}
    with open(STATE_PATH, "w") as f:
        json.dump(s, f)


# ---------------------------------------------------------------- sources --

def fetch_poly_trades(since_ts):
    """Polymarket public whale feed. Size is in SHARES; usd = size * price."""
    out = []
    data = http_get(POLY_TRADES, {"filter_type": "CASH",
                                  "filter_amount": int(POLY_FLOOR),
                                  "limit": 1000})
    if not data:
        return out
    for t in data.get("data", []):
        try:
            ts = int(t.get("timestamp", 0))
            if ts <= since_ts:
                continue
            usd = float(t.get("size", 0)) * float(t.get("price", 0))
            if usd < POLY_FLOOR:
                continue
            out.append({
                "venue": "Polymarket",
                "id": t.get("transaction_hash") or f"{t.get('proxy_wallet')}:{ts}",
                "market": t.get("title", "?"),
                "market_key": t.get("slug") or t.get("condition_id"),
                "outcome": t.get("outcome", "?"),
                "side": t.get("side", "?"),            # BUY / SELL
                "usd": round(usd, 2),
                "price": float(t.get("price", 0)),
                "ts": ts,
                "wallet": t.get("proxy_wallet", ""),
                "slug": t.get("slug", ""),
                "token_id": t.get("token_id", ""),
            })
        except Exception:
            continue
    return out


def _kalshi_get(path, params):
    for host in KALSHI_HOSTS:
        data = http_get(host + path, params)
        if data is not None:
            return data
    return None


def _to_epoch(v):
    try:
        return int(v)
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def fetch_kalshi_trades(since_ts):
    """Kalshi public trades feed. Best-effort: returns [] if blocked (403)."""
    out = []
    data = _kalshi_get("/trade-api/v2/markets/trades",
                       {"min_ts": since_ts, "limit": 1000})
    if not data:
        log("Kalshi unreachable from this runner (likely WAF 403) - continuing Polymarket-only.")
        return out
    for t in data.get("trades", []):
        try:
            ts = _to_epoch(t.get("created_time", 0))
            if ts <= since_ts:
                continue
            count = float(t.get("count_fp", 0) or t.get("count", 0))
            yes_px = float(t.get("yes_price_dollars", 0) or 0)
            side = (t.get("taker_outcome_side") or t.get("taker_side") or "?").lower()
            px = yes_px if side == "yes" else (1 - yes_px if side == "no" else yes_px)
            usd = count * px
            if usd < KALSHI_FLOOR and not t.get("is_block_trade"):
                continue
            out.append({
                "venue": "Kalshi",
                "id": t.get("trade_id") or f"{t.get('ticker')}:{ts}:{count}",
                "market": t.get("ticker", "?"),
                "market_key": t.get("ticker", "?"),
                "outcome": side.upper(),
                "side": "BUY",
                "usd": round(usd, 2),
                "price": round(px, 4),
                "ts": ts,
                "wallet": "",
                "ticker": t.get("ticker", ""),
                "block": bool(t.get("is_block_trade")),
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- detect ---

def detect(trades, state):
    """Returns (alerts, fresh_trades). alerts are dicts ready for formatting."""
    alerts = []
    fresh = [t for t in trades if t["id"] not in state["seen_ids"]]
    for t in fresh:
        state["seen_ids"].append(t["id"])

    # 1) mega prints
    for t in fresh:
        if t["usd"] >= POLY_MEGA and t["venue"] == "Polymarket":
            alerts.append(("MEGA", t, None))
        elif t["usd"] >= POLY_MEGA and t["venue"] == "Kalshi":
            alerts.append(("MEGA", t, None))

    # 2) clusters: N+ trades, same market+side, within window, each >= floor
    by_key = defaultdict(list)
    for t in fresh:
        if t["usd"] >= CLUSTER_FLOOR:
            by_key[(t["venue"], t["market_key"], t["side"], t["outcome"])].append(t)
    for key, ts_ in by_key.items():
        ts_ = sorted(ts_, key=lambda x: x["ts"])
        i = 0
        while i < len(ts_):
            j = i
            while j < len(ts_) and ts_[j]["ts"] - ts_[i]["ts"] <= CLUSTER_MIN * 60:
                j += 1
            window = ts_[i:j]
            if len(window) >= CLUSTER_N:
                total = sum(x["usd"] for x in window)
                alerts.append(("CLUSTER", window[0],
                               f"{len(window)} prints, ${total:,.0f} total in {CLUSTER_MIN}min"))
                i = j
            else:
                i += 1

    # 3) new whale wallets (Polymarket only - Kalshi feed has no wallet field)
    for t in fresh:
        w = t.get("wallet")
        if w and w not in state["seen_wallets"] and t["usd"] >= POLY_FLOOR:
            state["seen_wallets"].append(w)
            t["new_whale"] = True

    # 4) momentum: compare vs price remembered from >= MOM_MIN ago.
    # Baseline refreshes only after a comparison, giving a true lookback window.
    now = time.time()
    for t in fresh:
        k = f"{t['venue']}:{t['market_key']}:{t['outcome']}"
        old = state["market_px"].get(k)
        if old and now - old[0] >= MOM_MIN * 60 and old[1] > 0:
            drift = (t["price"] - old[1]) / old[1] * 100
            toward = (drift > 0 and t["side"] == "BUY") or (drift < 0 and t["side"] == "SELL")
            if abs(drift) >= MOM_PCT and toward:
                t["momentum"] = f"{drift:+.1f}% in {(now - old[0]) / 60:.0f}min"
            state["market_px"][k] = [now, t["price"]]  # refresh baseline
        elif not old:
            state["market_px"][k] = [now, t["price"]]  # establish baseline

    # 5) cross-venue mispricing (best-effort, needs markets_map.json)
    for m in load_map():
        try:
            k = _kalshi_get(f"/trade-api/v2/markets/{m['kalshi_ticker']}", None)
            g = http_get(POLY_GAMMA, {"slug": m["poly_slug"], "limit": 1})
            if not k or not g:
                continue
            mk = k.get("market", k)
            kpx = float(mk.get("last_price_dollars") or mk.get("yes_ask_dollars") or 0)
            gm = (g if isinstance(g, list) else g.get("data", []))[0]
            op = gm.get("outcomePrices")
            ppx = float(json.loads(op)[0]) if isinstance(op, str) else float(op[0])
            diff_c = abs(kpx - ppx) * 100
            if diff_c >= MISPRICING_C:
                alerts.append(("MISPRICING", {
                    "venue": "Cross-venue", "market": m["label"],
                    "outcome": "YES", "side": "CHECK", "usd": 0,
                    "price": round(kpx, 3), "ts": int(now),
                    "id": f"mis-{m['kalshi_ticker']}-{int(now // 3600)}",
                    "poly_px": round(ppx, 3), "diff_c": round(diff_c, 1),
                }, None))
        except Exception as e:
            log("mispricing check failed:", m.get("label"), e)
    return alerts, fresh


def load_map():
    try:
        with open(MAP_PATH) as f:
            return json.load(f)
    except Exception:
        return []


# ---------------------------------------------------------------- format ---

def fmt_usd(x):
    return f"${x:,.0f}"


def format_alert(kind, t, note):
    ts = datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%H:%M UTC")
    venue = t["venue"]
    head = {"MEGA": "🐋 MEGA WHALE", "CLUSTER": "🐋 WHALE CLUSTER",
            "MISPRICING": "⚖️ MARKET DISAGREEMENT"}[kind]
    lines = [f"{head} — {venue}", "",
             f"Market: {t['market']}",
             f"Side: {t['side']} {t['outcome']} @ {t['price']:.2f}"]
    if kind == "MISPRICING":
        lines += [f"Kalshi: {t['price']:.2f}  vs  Polymarket: {t['poly_px']:.2f}",
                  f"Gap: {t['diff_c']}¢  (check both books before acting — not arb)"]
    else:
        lines.append(f"Size: {fmt_usd(t['usd'])}")
        if t.get("block"):
            lines.append("Type: BLOCK TRADE")
    if note:
        lines.append(f"Note: {note}")
    if t.get("new_whale"):
        lines.append("🆕 First-seen whale wallet")
    if t.get("momentum"):
        lines.append(f"🔥 Momentum: {t['momentum']} toward this side")
    lines += ["", f"Detected {ts}", "",
              "Not financial advice. 21+. Trade responsibly."]
    title = f"{head}: {t['market'][:70]}"
    return title, "\n".join(lines)


# ---------------------------------------------------------------- whop -----

def whop_headers():
    h = {"Authorization": f"Bearer {WHOP_API_KEY}",
         "Content-Type": "application/json"}
    if WHOP_USER_ID:
        h["x-on-behalf-of"] = WHOP_USER_ID
    if WHOP_COMPANY_ID:
        h["x-company-id"] = WHOP_COMPANY_ID
    return h


def whop_api(query, variables):
    import requests
    r = requests.post(WHOP_GQL, headers=whop_headers(),
                      json={"query": query, "variables": variables}, timeout=25)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}


def ensure_user_id():
    """Discover our own user id via the viewer query if WHOP_USER_ID is unset."""
    global WHOP_USER_ID
    if WHOP_USER_ID:
        return True
    code, data = whop_api("query { viewer { user { id username } } }", {})
    try:
        u = data["data"]["viewer"]["user"]
        WHOP_USER_ID = u["id"]
        log(f"Posting as Whop user: {u.get('username')} ({WHOP_USER_ID})")
        return True
    except Exception:
        log("Could not discover Whop user id:", code, str(data)[:300])
        return False


def whop_post(title, content, exp_id=None):
    exp_id = exp_id or ALERTS_EXP_ID
    if not exp_id:
        log("No forum experience id configured - skipping post.")
        return False
    mutation = """mutation CreateForumPost($input: CreateForumPostInput!) {
      createForumPost(input: $input) { id }
    }"""
    variables = {"input": {"forumExperienceId": exp_id,
                           "title": title, "content": content}}
    code, data = whop_api(mutation, variables)
    if code == 200 and data.get("data", {}).get("createForumPost"):
        log("Posted to Whop:", title[:60])
        return True
    log("Whop post FAILED:", code, json.dumps(data)[:500])
    return False


# ---------------------------------------------------------------- log ------

def log_alert(kind, t, note):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({
                "ts": t["ts"], "alert_ts": int(time.time()),
                "kind": kind, "venue": t["venue"], "market": t["market"],
                "market_key": t.get("market_key"), "outcome": t["outcome"],
                "side": t["side"], "usd": t["usd"], "price": t["price"],
                "token_id": t.get("token_id", ""), "ticker": t.get("ticker", ""),
                "slug": t.get("slug", ""), "note": note or "",
            }) + "\n")
    except Exception as e:
        log("log write failed:", e)


# ---------------------------------------------------------------- main -----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-post", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run or DRY_RUN

    if not WHOP_API_KEY:
        log("FATAL: WHOP_API_KEY not set.")
        sys.exit(1)
    if not ensure_user_id():
        log("FATAL: set WHOP_USER_ID env var (see README).")
        sys.exit(1)

    if args.test_post:
        ok = whop_post("✅ Bot connected",
                       "The whale-alert bot is wired up. Alerts will appear here automatically.\n\n"
                       "Not financial advice. 21+.")
        sys.exit(0 if ok else 1)

    state = load_state()
    log(f"Scanning since poly_ts={state['poly_last_ts']}, kalshi_ts={state['kalshi_last_ts']}")

    poly = fetch_poly_trades(state["poly_last_ts"])
    kalshi = fetch_kalshi_trades(state["kalshi_last_ts"])
    log(f"Fetched {len(poly)} Polymarket + {len(kalshi)} Kalshi trades above floor.")

    alerts, fresh = detect(poly + kalshi, state)

    if poly:
        state["poly_last_ts"] = max(t["ts"] for t in poly)
    if kalshi:
        state["kalshi_last_ts"] = max(t["ts"] for t in kalshi)

    posted = 0
    for kind, t, note in alerts:
        if t["id"] in [a for a in state.get("posted_ids", [])]:
            continue
        title, body = format_alert(kind, t, note)
        if dry:
            log("[DRY]", title)
            print(body, "\n---")
        else:
            if whop_post(title, body):
                posted += 1
                state.setdefault("posted_ids", []).append(t["id"])
        log_alert(kind, t, note)

    state["posted_ids"] = state.get("posted_ids", [])[-2000:]
    save_state(state)
    log(f"Done. {len(fresh)} new trades, {len(alerts)} alerts, {posted} posted.")


if __name__ == "__main__":
    main()
