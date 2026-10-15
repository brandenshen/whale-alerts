#!/usr/bin/env python3
"""
Weekly track-record grader. Reads alerts_log.jsonl, checks how each alert's
market price moved after the alert, and posts a public summary to the
Whop track-record forum.

Honest metric: price DRIFT toward/away from the signaled side, not win rate.
A whale buying YES at 0.60 whose market sits at 0.72 a week later drifted +12c
toward the signal. That's the number members can verify themselves.

Runs weekly via .github/workflows/grade.yml (Mondays).
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

WHOP_API_KEY    = os.environ.get("WHOP_API_KEY")
WHOP_USER_ID    = os.environ.get("WHOP_USER_ID")
WHOP_COMPANY_ID = os.environ.get("WHOP_COMPANY_ID")
TRACK_EXP_ID    = os.environ.get("WHOP_TRACK_EXP_ID")
LOG_PATH        = os.environ.get("ALERTS_LOG_PATH", "alerts_log.jsonl")
DAYS            = int(os.environ.get("GRADE_LOOKBACK_DAYS", "7"))

WHOP_REST_POST = "https://api.whop.com/api/v1/forum_posts"
CLOB_BOOK  = "https://clob.polymarket.com/book"
KALSHI     = "https://external-api.kalshi.com/trade-api/v2"
UA = {"User-Agent": "whale-alert-bot/1.0 (+github-actions)"}


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def current_price(a):
    """Best-effort current price for the alerted outcome. Returns 0-1 float or None."""
    try:
        if a["venue"] == "Polymarket" and a.get("token_id"):
            r = requests.get(CLOB_BOOK, params={"token_id": a["token_id"]},
                             headers=UA, timeout=20)
            if r.status_code == 200:
                return float(r.json().get("last_trade_price") or 0) or None
        if a["venue"] == "Kalshi" and a.get("ticker"):
            r = requests.get(f"{KALSHI}/markets/{a['ticker']}", headers=UA, timeout=20)
            if r.status_code == 200:
                m = r.json().get("market", {})
                return float(m.get("last_price_dollars")
                             or m.get("yes_ask_dollars") or 0) or None
    except Exception as e:
        log("price check failed:", e)
    return None


def drift_cents(a, now_px):
    """Positive = market moved TOWARD the signaled side (in cents)."""
    then = a["price"]
    if a["venue"] == "Kalshi" and a["outcome"] == "NO":
        return round((then - now_px) * 100, 1)   # NO wants price down
    if a["side"] == "SELL":
        return round((then - now_px) * 100, 1)
    return round((now_px - then) * 100, 1)


def whop_post(title, content):
    """Post via Whop REST API v1 (public GraphQL is closed to new integrations).
    Needs an App API key with the forum:post:create scope."""
    if not TRACK_EXP_ID:
        log("WHOP_TRACK_EXP_ID not set - printing summary instead.")
        print(title, "\n", content)
        return
    try:
        r = requests.post(
            "https://api.whop.com/api/v1/forum_posts",
            headers={"Authorization": f"Bearer {WHOP_API_KEY}",
                     "Content-Type": "application/json"},
            json={"experience_id": TRACK_EXP_ID, "title": title,
                  "content": content},
            timeout=25)
    except Exception as e:
        log("Track-record post request failed:", e)
        return
    log("Track-record post status:", r.status_code, r.text[:200])


def main():
    cutoff = time.time() - DAYS * 86400
    alerts = []
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    a = json.loads(line)
                    if a.get("alert_ts", 0) >= cutoff and a.get("kind") != "MISPRICING":
                        alerts.append(a)
                except Exception:
                    pass
    except FileNotFoundError:
        log("No alerts log yet.")
        return

    if not alerts:
        log("No alerts in window - nothing to grade.")
        return

    drifts = []
    wins = losses = 0
    best = worst = None
    for a in alerts:
        now = current_price(a)
        if now is None:
            continue
        d = drift_cents(a, now)
        drifts.append(d)
        (wins := wins + 1) if d > 0 else (losses := losses + 1)
        if best is None or d > best[0]:
            best = (d, a)
        if worst is None or d < worst[0]:
            worst = (d, a)

    n = len(drifts)
    avg = sum(drifts) / n if n else 0
    week = datetime.now(timezone.utc).strftime("%b %d")
    title = f"📊 Track record — week of {week}"
    lines = [f"Alerts graded: {n}",
             f"Drifted toward signal: {wins} | against: {losses}",
             f"Average drift: {avg:+.1f}¢ per alert", ""]
    if best:
        lines.append(f"Best: {best[1]['market'][:60]} ({best[0]:+.1f}¢)")
    if worst:
        lines.append(f"Worst: {worst[1]['market'][:60]} ({worst[0]:+.1f}¢)")
    lines += ["", "Drift = how far the market moved toward the alerted side, in cents.",
              "Full alert history is in 🐋 Whale Alerts. Not financial advice. 21+."]
    whop_post(title, "\n".join(lines))
    log(f"Graded {n} alerts: avg drift {avg:+.1f}c")


if __name__ == "__main__":
    main()
