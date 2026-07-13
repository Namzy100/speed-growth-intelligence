# Speed — Onboarding

Day-one guide for whoever inherits this project. For the *why* behind design
decisions (scoring model, scheduler history), read `CLAUDE.md`; this doc is how to
run, check, and debug the system.

## What this system is

A Python intelligence pipeline for Speed Wallet's growth team. It:
- discovers and **scores creators** (YouTube, TikTok, X, Instagram) as potential
  partners, and
- tracks **content trends** and **campaign performance** (Meta + Adjust),

persisting everything in **Supabase** + **Google Sheets**, and publishing
**self-contained HTML dashboards**. AI (Anthropic) is used for scoring
classification, trend enrichment, and dashboard insight cards.

Markets to keep in mind for any analysis: paid ads = US + EU; influencers = US,
Mexico, Brazil.

## The three main pipelines

1. **Creator scoring** (`creators/`) — fetch creators, score them, build the
   creator dashboard.
   - Fetchers: `youtube.py`, `apify_tiktok.py`, `apify_x.py` (all return the same
     creator dict shape → `scorer.py` → `database.py` (Supabase)).
   - Scoring: `scorer.py` — 4-dimension composite (audience_fit, engagement, reach,
     sponsorship-where-measured). See `CLAUDE.md` → "Creator scoring model".
   - Dashboard: `pipelines/build_creator_dashboard.py`.
   - Discovery batches: `creators/youtube_batch.py`, `creators/x_batch.py`.

2. **Trend tracking** (`intelligence/trend_pipeline.py`,
   `pipelines/build_trend_dashboard.py`) — scrapes trending YouTube/TikTok/Instagram
   content, enriches with Claude, tracks predict→ship→measure. Rebuilds weekly
   (Mondays).

3. **Campaign sync** (`pipelines/`) — `adjust.py` (installs/retention) and
   `meta.py` (spend/installs/creatives) → Google Sheets → `build_creative_dashboard.py`
   and the Looker Studio dashboard. Orchestrated by `pipelines/run_daily_sync.py`.

## First-day setup

```bash
cd /Users/namzysacc/Documents/Speed
source venv/bin/activate          # always activate first
python test_keys.py               # confirms .env keys are present
python test_connections.py        # confirms every API connection is live
```
Secrets live in `.env` (local) and in **GitHub Actions repo secrets** (for the
scheduled cloud runs). `.env` is gitignored — never commit it.

## Health check — is everything actually working?

**Scheduled automation (the daily sync):** it runs on **GitHub Actions**, not this
machine.
- GitHub → **Actions → daily-sync** → check the latest run is green. Auto-deploy
  commits land on `main` authored by `speed-sync-bot`.
- `git log --oneline | grep auto-deploy | head` — most recent should be recent
  (daily).

**Dashboards are current (live-served, cache-busted):**
```bash
for u in creative_dashboard creator_dashboard strategy_dashboard trend_dashboard; do
  echo -n "$u: "; curl -s "https://namzy100.github.io/speed-growth-intelligence/$u.html?cb=$(date +%s)" \
    | grep -oE "2026-[0-9-]+ [0-9:]+ UTC" | head -1
done
```
Timestamps should be today (trend is weekly, so up to ~7 days old is normal).
> Note: `CLAUDE.md` flags a Vercel-vs-GitHub-Pages hosting discrepancy — confirm
> which host is canonical.

**Data is flowing:** `python pipelines/meta.py` should return ~5 campaigns + ~58
ad-level rows with no auth error. Supabase creator count (`database.get_all_creators()`)
should be ~347.

**Run a sync by hand** (any time, safe): trigger GitHub → Actions → daily-sync →
Run workflow, or locally `DASHBOARD_AUTODEPLOY=1 python pipelines/run_daily_sync.py`.

## When something breaks — where to look

- **Scheduled run failed:** GitHub → Actions → daily-sync → open the red run → the
  "Run daily sync" step log. This is the first place to look for any daily-sync
  problem now (not local logs).
- **Local run logs:** `~/Library/Logs/speed/sync.log` (and `weekly_update.log`,
  `trend_pipeline.log`) — only populated by local/launchd runs, which are now the
  fallback, not primary. (Older stray `*.log` at the repo root are orphaned and
  gitignored.)
- **Meta auth failure:** `meta.py` now raises a clear message on a bad token
  ("token invalid or EXPIRED" / "lacks permission ... ads_read"). If you see that,
  the `META_ACCESS_TOKEN` (in `.env` and the GitHub secret) needs regenerating — see
  `CLAUDE.md` → Manual steps, and the System-User-token setup.
- **Dashboard stale but sync "passed":** check the sync actually reached the deploy
  step (a hung network call once blocked it mid-run). Re-run the sync; confirm a new
  `auto-deploy` commit + a fresh live timestamp.
- **Scoring looks wrong:** `creators/scorer.py` + the rescore scripts
  (`rescore_4dim.py`, `rescore_creator_type.py`). Re-scores are dry-run by default;
  `--write` persists.

## Outstanding manual steps (see CLAUDE.md for detail)

- **Finish the YouTube sponsorship backfill:** `python creators/refetch_youtube.py --write`
  until it reports 0 remaining (84/217 done as of 2026-07-13; ~90/day quota cap).
- **Populate X creators:** `python creators/x_batch.py --write` (only ~4 exist so far).
- **Retire the old Meta token** once the new never-expiring System User token holds
  across several scheduled runs (old one is still active as rollback).
- **Weekly report "Part 2"** (standardized 8-section email automation) is designed
  but not built; the old weekly-email launchd agent is disabled meanwhile.

## Key files map

| Path | What it is |
|------|------------|
| `pipelines/run_daily_sync.py` | Orchestrator run by the daily Actions job |
| `.github/workflows/daily-sync.yml` | The live cloud scheduler (+ its `README.md` for secrets) |
| `creators/scorer.py` | The 4-dimension creator scoring model |
| `creators/{youtube,apify_tiktok,apify_x}.py` | Per-platform fetchers (same output shape) |
| `creators/database.py` | Supabase read/write + schema/migration SQL |
| `pipelines/build_*_dashboard.py` | The dashboard builders |
| `pipelines/meta.py`, `pipelines/adjust.py` | Campaign data sources |
| `deploy/launchd/` | Deprecated local scheduler (fallback only) |
| `CLAUDE.md` | Conventions + the *why* behind the design |
