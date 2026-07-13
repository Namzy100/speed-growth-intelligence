# Scheduled workflows

## daily-sync.yml
Cloud replacement for the Mac/launchd daily sync. Runs `pipelines/run_daily_sync.py`
on GitHub's runners at **04:00 UTC daily** (= 08:00 in the +04 machine tz), plus a
manual **Run workflow** button (`workflow_dispatch`). The Monday trend rebuild
rides along (gated internally). It syncs Adjust/Meta, rebuilds the dashboards, and
auto-deploys them (commit + push).

Why this exists: the launchd agent never fired unattended (the pmset wake was only
a DarkWake, no active session), and a hung run with no timeout blocked all future
fires for ~2.5 days. A cloud cron with a 30-minute job timeout removes both
failure modes.

### Required repo secrets (add in GitHub → Settings → Secrets and variables → Actions)
Secret values are NEVER committed. Add each from the corresponding `.env` value:

| Secret | Source |
|--------|--------|
| `GOOGLE_SHEETS_CREDS_JSON` | the **contents** of the service-account JSON file (not the path) |
| `GOOGLE_SHEETS_ID` | `GOOGLE_SHEETS_ID` |
| `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` |
| `APIFY_API_KEY` | `APIFY_API_KEY` |
| `YOUTUBE_API_KEY` | `YOUTUBE_API_KEY` |
| `SUPABASE_URL` | `SUPABASE_URL` |
| `SUPABASE_KEY` | `SUPABASE_KEY` |
| `META_ACCESS_TOKEN` | `META_ACCESS_TOKEN` |
| `META_AD_ACCOUNT_ID` | **Optional.** Not in `.env`; `meta.py` defaults to `act_1771013173838856`. Add a secret only to target a different ad account. |
| `ADJUST_API_KEY` | `ADJUST_API_KEY` |
| `SLACK_WEBHOOK_URL` | `SLACK_WEBHOOK_URL` (optional; alerts) |

### Fire-test (do this once, before relying on the schedule)
1. Add all secrets above.
2. GitHub → Actions → **daily-sync** → **Run workflow** (this uses `workflow_dispatch`).
3. Confirm the run is green and that a new `chore: auto-deploy dashboard refresh`
   commit lands on `main` with a fresh dashboard timestamp.

Once a real run is confirmed green, the Mac launchd `com.speed.dailysync` and
`com.speed.trend` agents can be retired (`launchctl bootout`). Do NOT retire them
before the Actions run is proven.
