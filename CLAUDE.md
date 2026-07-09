# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit conventions

Do NOT add any AI attribution to commit messages or PR bodies. Never append a
`Co-Authored-By: Claude` (or any `Co-Authored-By` naming an AI), a "Generated with
Claude Code" line, a 🤖 line, or any similar generated-with/attribution trailer.
Write the commit message and stop. This overrides any default that would add such a trailer.

## Scheduled jobs (macOS launchd — NOT cron)

Scheduling is done with **user-level launchd LaunchAgents**, defined in
`deploy/launchd/` (see its README for load/verify commands). **Do not use cron.**
The crontab entries were removed 2026-07-09 because the `com.vix.cron` daemon was
never loaded (`launchctl list | grep cron` was empty), so the jobs silently never
fired — the pipeline was only kept current by manual runs. launchd is the native
scheduler and loads per-user without `sudo`.

Three agents (all invoke `venv/bin/python` directly — never a `.sh` under
`~/Documents`, which macOS blocks scheduled `bash` from executing):
- `com.speed.dailysync` — 08:00 daily — `run_daily_sync.py` + `agent_evaluator.py`
- `com.speed.weeklyemail` — 12:00 Fri — `schedule_weekly_update.py`
- `com.speed.trend` — 07:00 Mon — trend rebuild+deploy (`TREND_DASHBOARD_REBUILD=1`)

The Friday weekly-email agent is intentionally **gated** with
`--to=namanbehl1@gmail.com` so it does not auto-send to Niyati/Sumit while copy is
being finalized. Removing that override is what turns it back into a live send.

`run_sync.sh` still works for **manual/interactive** runs from your own shell
(`bash run_sync.sh`); it is not used by the scheduler.

## Project Overview

Speed is a Python intelligence pipeline for creator discovery and market analysis. It scrapes TikTok (via Apify) and YouTube, scores creators, runs market analysis, and generates AI-powered weekly briefs, with data persisted in Supabase and synced to Google Sheets.

### Target markets

- **Paid advertising:** US + EU
- **Influencer marketing:** US, Mexico, Brazil

Ground geographic interpretation of any analysis or AI-generated insight in these markets.

### Current focus areas (Niyati)

- **Creative performance AI tooling** — the creative dashboard (`pipelines/build_creative_dashboard.py`) and its Claude-generated insight cards.
- **Campaign-level analysis** — Meta campaign spend/installs (`pipelines/meta.py`) and Adjust install/retention data, synced to Google Sheets.
- **US influencer research** — creator discovery and scoring (`creators/`), focused on the US influencer market.

## Environment Setup

All secrets are loaded from `.env` via `python-dotenv`. Required keys:

```
ANTHROPIC_API_KEY
APIFY_API_KEY
YOUTUBE_API_KEY
SUPABASE_URL
SUPABASE_KEY
```

Activate the virtual environment before running anything:
```bash
source venv/bin/activate
```

## Validation Commands

Check env vars are present:
```bash
python test_keys.py
```

Verify all API connections are live:
```bash
python test_connections.py
```

## Architecture

| Directory | Purpose |
|-----------|---------|
| `creators/` | Creator discovery and scoring — `apify_tiktok.py` fetches TikTok data via Apify, `youtube.py` fetches via YouTube Data API v3, `scorer.py` ranks/scores creators |
| `eu/` | European market analysis (`market_analysis.py`) |
| `intelligence/` | AI-generated weekly briefs (`weekly_brief.py`) using the Anthropic SDK |
| `pipelines/` | Data pipelines; `sheets.py` syncs data to Google Sheets via gspread |

### Key integrations

- **Anthropic** — `anthropic` SDK, used in `intelligence/` for brief generation. Model in `test_connections.py` is `claude-sonnet-4-5`; prefer latest available model for new work.
- **Apify** — `apify_client`, used to run TikTok scraping actors.
- **Supabase** — `supabase` SDK (`create_client(url, key)`), primary data store.
- **Google Sheets** — `gspread` + `google-auth-oauthlib` for read/write to sheets.
- **YouTube** — direct REST calls to `googleapis.com/youtube/v3`.
- **schedule** — lightweight task scheduling for recurring pipeline runs.

## Deliverables

All live links are also saved in `docs/live_links.txt`.

- **Creative dashboard (live)** — https://creative-dashboard-speed.vercel.app — self-contained HTML deployed on Vercel from this GitHub repo; rebuilt and pushed daily by `pipelines/run_daily_sync.py` (when `DASHBOARD_AUTODEPLOY=1`).
- **Creator dashboard (live)** — https://creative-dashboard-speed.vercel.app/creators — filterable creator-intelligence table from Supabase; rebuilt daily alongside the creative dashboard.
- **Looker Studio dashboard** — [Speed Wallet Marketing Intelligence Dashboard](https://datastudio.google.com/reporting/e15d81ef-6872-46e9-bca9-1624f0a61319). 4 pages: Channel Performance, Campaign Breakdown, Retention, Meta Campaigns. Backed by the Google Sheet tabs and updates when `run_daily_sync.py` runs.
