# Speed scheduler — launchd LaunchAgents

These three user-level launchd agents replace the old `crontab` entries, which
had **silently stopped firing**: the `com.vix.cron` daemon was never loaded
(`launchctl list | grep cron` was empty), so no scheduled job ran — the pipeline
was only ever kept current by manual runs. The crontab entries have been removed
(better no scheduler than one that looks configured but does nothing).

launchd is the native macOS scheduler and is more reliable than cron here. These
are **user agents** (`gui/<uid>`), so loading them needs **no `sudo`** — but they
only fire while you are logged into your GUI session. If the Mac is asleep at the
scheduled time, the job runs at next wake (launchd coalesces missed calendar
fires); if it's powered off, that fire is skipped.

| Agent | Schedule | What it runs |
|-------|----------|--------------|
| `com.speed.dailysync`   | 08:00 daily        | `run_daily_sync.py` then `agent_evaluator.py` (`DASHBOARD_AUTODEPLOY=1`) → `~/Library/Logs/speed/sync.log` |
| `com.speed.weeklyemail` | 12:00 Fridays      | `schedule_weekly_update.py --to=namanbehl1@gmail.com` (test-gated) → `~/Library/Logs/speed/weekly_update.log` |
| `com.speed.trend`       | 07:00 Mondays      | `run_daily_sync.py` with `TREND_DASHBOARD_REBUILD=1` → rebuilds + deploys the trend dashboard → `~/Library/Logs/speed/trend_pipeline.log` |

> **Why logs live in `~/Library/Logs/speed/`, not next to the code in `~/Documents`:**
> launchd (the daemon) opens each agent's `StandardOutPath`/`StandardErrorPath`
> *itself* at spawn time. `~/Documents` is a TCC-protected folder that the launchd
> daemon can't open, so a log path there makes `bootstrap` fail with
> **`Bootstrap failed: 5: Input/output error`** and the job exits **`78` (EX_CONFIG)**
> with no output. (The job *itself*, once running, reads/writes `~/Documents` fine —
> only launchd's own file-open at spawn is blocked.) Do not move the logs back into
> `~/Documents`.

Secrets are NOT in these plists — the pipeline loads API keys from `.env` via
`python-dotenv`. Only non-secret flags (`DASHBOARD_AUTODEPLOY`,
`TREND_DASHBOARD_REBUILD`) are set here.

> The `--to=namanbehl1@gmail.com` gate on the weekly email is intentional — it
> keeps the job off Niyati/Sumit while copy is finalised. Removing it makes it a
> live send.

## Install / load (run in your own terminal — needs your GUI session)

```bash
cd /Users/namzysacc/Documents/Speed

# 0. Log dir must exist (launchd creates the log FILE but not the parent dir),
#    and clear any half-registered prior attempt (ignore "not found" errors).
mkdir -p ~/Library/Logs/speed
for L in com.speed.dailysync com.speed.weeklyemail com.speed.trend; do
  launchctl bootout gui/$(id -u)/$L 2>/dev/null
done

# 1. Copy all three into your LaunchAgents directory
cp deploy/launchd/com.speed.dailysync.plist   ~/Library/LaunchAgents/
cp deploy/launchd/com.speed.weeklyemail.plist ~/Library/LaunchAgents/
cp deploy/launchd/com.speed.trend.plist       ~/Library/LaunchAgents/

# 2. Load each into your GUI session (idempotent bootstrap)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.speed.dailysync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.speed.weeklyemail.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.speed.trend.plist

# 3. Fire one once now, as a test (kickstart -k restarts if already running)
launchctl kickstart -k gui/$(id -u)/com.speed.dailysync

# 4. Watch the log — confirm a REAL completed run, e.g.
#    "[...] Sync complete — N/N steps succeeded"  (not just that it started)
tail -f ~/Library/Logs/speed/sync.log
```

## Prove they're actually loaded and scheduled (survives past a manual kick)

```bash
launchctl list | grep speed
```

You should see all three labels. The columns are `PID  Status  Label`:
- `PID` is `-` when idle (normal — it only has a PID while actively running).
- `Status` `0` means the last run exited cleanly.

To see the next scheduled fire time for one:

```bash
launchctl print gui/$(id -u)/com.speed.dailysync | grep -A2 -i 'next\|runs'
```

## Reload after editing a plist

```bash
launchctl bootout   gui/$(id -u)/com.speed.dailysync
cp deploy/launchd/com.speed.dailysync.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.speed.dailysync.plist
```

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.speed.dailysync
launchctl bootout gui/$(id -u)/com.speed.weeklyemail
launchctl bootout gui/$(id -u)/com.speed.trend
rm ~/Library/LaunchAgents/com.speed.{dailysync,weeklyemail,trend}.plist
```
