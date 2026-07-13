# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit conventions

Do NOT add any AI attribution to commit messages or PR bodies. Never append a
`Co-Authored-By: Claude` (or any `Co-Authored-By` naming an AI), a "Generated with
Claude Code" line, a 🤖 line, or any similar generated-with/attribution trailer.
Write the commit message and stop. This overrides any default that would add such a trailer.

## Scheduled jobs (GitHub Actions is primary; launchd is a disabled fallback)

The daily sync runs on **GitHub Actions**, not on any local machine. This is the
real scheduler as of 2026-07-13.

- **Workflow:** `.github/workflows/daily-sync.yml` (see `.github/workflows/README.md`).
- **Schedule:** cron `0 4 * * *` (04:00 UTC, ≈ 08:00 in the +04 machine tz) plus a
  manual **Run workflow** button (`workflow_dispatch`), with a 30-minute job timeout.
- **What it runs:** `pipelines/run_daily_sync.py` — Adjust + Meta sync, dashboard
  rebuild, and auto-deploy (commit + push as `speed-sync-bot`). The Monday trend
  rebuild rides along via `TREND_DASHBOARD_REBUILD=1` (the pipeline gates it to
  Mondays internally).
- **Secrets** live in GitHub → Settings → Secrets and variables → Actions, never
  committed. Exact list is in `.github/workflows/README.md`. `META_AD_ACCOUNT_ID`
  is optional (`meta.py` defaults to `act_1771013173838856`).

### Why it moved off the Mac (do NOT revert to local scheduling)
1. **cron** (original) — the `com.vix.cron` daemon was never loaded, so nothing
   fired. Removed 2026-07-09.
2. **launchd** (replacement) — user LaunchAgents only run in an active GUI session.
   The `pmset` 7:58 wake produced a **DarkWake**, not a FullWake, so the 08:00 job
   never got a live session and never fired unattended. Separately, a run that
   hung on a timeout-less network call stayed "running" for ~2.5 days and
   **blocked all future fires** (launchd will not relaunch a job it believes is
   still running). The cloud cron plus a hard job timeout removes both failure modes.

### launchd agents are DEPRECATED FALLBACK — do not re-enable as the primary system
`deploy/launchd/` still holds the agent plists, and `com.speed.dailysync` /
`com.speed.trend` may still be loaded on the Mac. They are **superseded by GitHub
Actions** and kept only as an emergency fallback — they carry the DarkWake
limitation above, so do not treat them as authoritative or re-enable them thinking
they are the live scheduler. `com.speed.weeklyemail` was booted out
(`launchctl bootout`) on 2026-07-13 and should stay off (its script is the old
persona-format weekly email, pending the not-yet-built "Part 2" rebuild). Once the
Actions runs are confirmed stable over several days, the launchd daily/trend agents
can be fully retired with `launchctl bootout`.

`run_sync.sh` still works for **manual/interactive** runs from your own shell
(`bash run_sync.sh`); it is not used by any scheduler.

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
META_ACCESS_TOKEN
GOOGLE_SHEETS_CREDS
GOOGLE_SHEETS_ID
ADJUST_API_KEY
```
(`META_AD_ACCOUNT_ID` is optional — `meta.py` defaults to `act_1771013173838856`.)
For the GitHub Actions run, the same values live as repo secrets — see
`.github/workflows/README.md`.

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
| `intelligence/` | 15+ Anthropic-powered analysis modules — e.g. `trend_pipeline.py` (trend scan/enrich), `agent_evaluator.py`, `weekly_brief.py`, `competitor_analysis.py`, `outreach_converter.py`, `spend_optimization.py`, `eu_gtm_plan.py` |
| `pipelines/` | Data pipelines; `sheets.py` syncs data to Google Sheets via gspread |

### Key integrations

- **Anthropic** — `anthropic` SDK, used in `intelligence/` for brief generation. Model in `test_connections.py` is `claude-sonnet-4-5`; prefer latest available model for new work.
- **Apify** — `apify_client`, used to run TikTok scraping actors.
- **Supabase** — `supabase` SDK (`create_client(url, key)`), primary data store.
- **Google Sheets** — `gspread` + `google-auth-oauthlib` for read/write to sheets.
- **YouTube** — direct REST calls to `googleapis.com/youtube/v3`.
- **schedule** — lightweight task scheduling for recurring pipeline runs.

## Creator scoring model (`creators/scorer.py`)

The composite score (0–100) is built from **4 real dimensions, equally weighted**,
each scored /20 and renormalised to /100 over the dimensions that carry real
signal for a given creator (rebuilt in the 2026-07 scoring audit):

1. **audience_fit** — segment/topic match to Speed's markets (niche tags +
   crypto/fintech content %).
2. **engagement** — genuine interaction quality (`engagement_quality`, from
   likes+comments/views), not view-gamed reach.
3. **reach** — pure follower size on a diminishing-returns curve ("Curve B":
   `(log10(followers) - 3) / 3 * 20` — 1k followers → 0, 1M → 20, saturating above
   1M). Deliberately decoupled from engagement/fit so it does not double-count them.
4. **sponsorship** — brand-deal history, **included only where actually measured**
   (`sponsorship_data_available`): TikTok `isSponsored`/`isAd`, YouTube
   `paidProductPlacementDetails`. Where not measured (X, or YouTube not yet
   re-fetched), it is excluded and its weight redistributed — never scored 0.

Why the model looks this way (so a cold reader understands the shape, not just the code):
- **content_alignment was DROPPED** from the composite — it read the same
  crypto/fintech keyword signal off the same text as audience_fit (double-counting).
  Still computed/stored for reference, just not scored.
- **deposit_relevance was DROPPED** — its inputs were a never-set 0.5 constant plus
  recycled dimensions, i.e. no independent signal. Reintroduce only with a real proxy.
- **reach** was previously reference-only (`acquisition_potential`); it is now a real
  composite dimension (still stored in the `acquisition_potential` column).
- **is_influencer** (the Individual vs Brand/Media call) runs through a cheap LLM
  classifier (`_classify_individual_brand`, Haiku), gated behind `use_llm_fallback`,
  with the keyword heuristic (`looks_like_media_name` / `looks_like_company`) as the
  deterministic fallback. Keyword matching alone could not separate surface-identical
  names (e.g. "Crypto Wall Street" the brand vs "Crypto Casey" the person).
- **scraped_data_available gate** — creators bulk-imported with placeholder stats
  (the Mimanshi Instagram/X set, which has no fetcher) have their fabricated
  audience_fit + engagement excluded; they score on reach only and are flagged
  "unscraped" in the dashboard. Their curator `fit` rating (1–5) is a ranking
  tiebreaker and a visible badge, not a composite input.
- **Colour bands** on the dashboard are percentile-based, recomputed each build
  (top 25% green, bottom 35% red, middle yellow — `_score_bands` in
  `pipelines/build_creator_dashboard.py`), not fixed cutoffs, because fixed
  thresholds go stale whenever the formula changes.

## Manual steps that are NOT automated (a successor will not discover these)

- **YouTube sponsorship re-fetch is not fully automated.** The 217 existing YouTube
  creators are being re-fetched with the real paid-placement flag in daily,
  quota-limited batches. Run `python creators/refetch_youtube.py --write` periodically
  until it reports **0 remaining** (~90 creators/day; resumable — done creators are
  skipped). As of 2026-07-13: **84 of 217** done. The daily Actions sync does NOT do
  this — it must be run by hand.
- **X (Twitter) creators are barely populated.** `creators/x_batch.py` works but has
  **not been run for a full sweep** — only ~4 X creators (the original Mimanshi
  handles) are in the DB. Run `python creators/x_batch.py --write` to discover + score
  X creators into the pipeline (dry-run by default; `--write` persists).
- **Meta token is now a never-expiring System User token** (set up 2026-07-13), in
  `.env` as `META_ACCESS_TOKEN` and in the GitHub Actions secret of the same name.
  The **old personal token is still active as rollback and has NOT been revoked** —
  retire it only after several successful scheduled Actions runs confirm the new
  token holds. `meta.py` needs only `ads_read`.

## Deliverables

Canonical host is **GitHub Pages**. All live links are also in `docs/live_links.txt`.

- **Creative dashboard (live)** — https://namzy100.github.io/speed-growth-intelligence — self-contained HTML rebuilt and pushed daily by `pipelines/run_daily_sync.py` (when `DASHBOARD_AUTODEPLOY=1`).
- **Creator dashboard (live)** — https://namzy100.github.io/speed-growth-intelligence/creators — filterable creator-intelligence table from Supabase; rebuilt daily alongside the creative dashboard.
- **Strategy dashboard (live)** — https://namzy100.github.io/speed-growth-intelligence/strategy — rebuilt daily.
- **Looker Studio dashboard** — [Speed Wallet Marketing Intelligence Dashboard](https://datastudio.google.com/reporting/e15d81ef-6872-46e9-bca9-1624f0a61319). 4 pages: Channel Performance, Campaign Breakdown, Retention, Meta Campaigns. Backed by the Google Sheet tabs and updates when `run_daily_sync.py` runs.

> Note: an old Vercel deployment (`creative-dashboard-speed.vercel.app`) exists in
> config (`vercel.json`, `VERCEL_DEPLOY_HOOK`) but was checked and found
> stale/unused as of 2026-07-13. GitHub Pages is the live host. Vercel was
> deliberately deprecated, not forgotten — do not re-add it as a deliverable without
> knowing that.
