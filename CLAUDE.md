# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
