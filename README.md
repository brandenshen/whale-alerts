# Whale Alert Bot — Setup Guide

Everything here is free. The bot runs on GitHub Actions (a free robot that runs
your code on a schedule) and posts alerts into your Whop forum automatically.
**The code does NOT go inside Whop** — Whop is your storefront + alert feed, GitHub
is the engine room. Total setup time: ~30 minutes.

## What you need

- A GitHub account (free)
- Your Whop API key + forum IDs (get these from `WHOP_SETUP.md` first)

## Step 1 — Create the repo

1. Go to github.com → sign up / log in.
2. Click **+** (top right) → **New repository**.
3. Name it `whale-alerts`, set it to **Public** (public repos get unlimited free
   run-minutes — private repos are capped and this bot would exceed the cap).
4. Click **Create repository**.

## Step 2 — Upload the bot files

1. In your new repo, click **Add file → Upload files**.
2. Drag in ALL of these files (keep the folder structure):
   - `scanner.py`, `grade.py`, `requirements.txt`, `.env.example`
   - `markets_map.json` ← first rename `markets_map.example.json` to this
     (or skip it — the bot runs fine without it; it only powers cross-venue checks)
   - the `.github/workflows/` folder with `scan.yml` and `grade.yml` inside
3. Click **Commit changes**.

> Tip: to upload the `.github` folder, zip everything on your computer first,
> upload the zip via **Add file → Upload files**, GitHub unzips it for you.

## Step 3 — Add your secrets

Secrets are how the bot logs into Whop without exposing your key.

1. In your repo: **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** for each of these:

| Secret name | Value | Where to find it |
|---|---|---|
| `WHOP_API_KEY` | your key | `WHOP_SETUP.md` Step 3 |
| `WHOP_ALERTS_EXP_ID` | `exp_...` | `WHOP_SETUP.md` Step 2 |
| `WHOP_TRACK_EXP_ID` | `exp_...` | `WHOP_SETUP.md` Step 2 (optional) |
| `WHOP_USER_ID` | `user_...` | optional — the bot discovers this itself |
| `WHOP_COMPANY_ID` | `biz_...` | optional — only if Whop asks for it |

## Step 4 — Verify the wiring (do this first!)

1. Go to **Actions** tab → **Whale scanner** → **Run workflow**.
2. Tick **"Send a test post to Whop"** → **Run workflow**.
3. Wait ~1 minute, refresh, click the run — it should end green ✅.
4. Check your Whop forum: you should see **"✅ Bot connected"**.

If the run is red, click it and read the log — it tells you exactly what failed
(see Troubleshooting below).

## Step 5 — Let it run

That's it. The schedule (`*/10 * * * *`) runs the scanner every 10 minutes
automatically. The weekly grader runs Mondays at 09:00 UTC. You never touch it
again except to tune.

Check in occasionally: **Actions** tab shows every run, green = healthy.

## Tuning (after week 1)

Too noisy? Raise the floors. Too quiet? Lower them. Edit these in `scan.yml`
under `env:` (or add them as secrets to override):

- `POLY_WHALE_FLOOR_USD` (default 5000) — min trade size to track
- `POLY_MEGA_FLOOR_USD` (default 10000) — instant-alert threshold
- `CLUSTER_COUNT` / `CLUSTER_WINDOW_MIN` — cluster sensitivity
- `MOMENTUM_PCT` — momentum tag threshold

After editing, commit — the next scheduled run picks it up.

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| "Kalshi unreachable (403), continuing Polymarket-only" | Kalshi's firewall blocks some datacenter IPs | Normal — bot still works on Polymarket. It retries both Kalshi hosts every run. |
| "Whop post FAILED: 401" | Bad API key | Re-check `WHOP_API_KEY` secret for typos/spaces |
| "Whop post FAILED" with field errors | Forum ID wrong or input shape changed | Verify `WHOP_ALERTS_EXP_ID` starts with `exp_` and is the *forum* experience, not the whop |
| "Could not discover Whop user id" | Key can't read viewer | Set `WHOP_USER_ID` secret manually (find it in your Whop dashboard URL) |
| No alerts for hours | Normal in quiet markets | Run with `--dry-run` via workflow dispatch to see what the scanner sees |
| Duplicate alerts | State file didn't persist | Check the "Persist bot state" step is green in the logs |

## Files

- `scanner.py` — the bot: fetch → detect → post → log
- `grade.py` — weekly public track-record grader
- `markets_map.json` — optional cross-venue pairs (Kalshi ticker ↔ Polymarket slug)
- `state.json` / `alerts_log.jsonl` — created automatically, committed back each run
