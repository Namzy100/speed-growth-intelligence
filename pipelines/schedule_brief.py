"""Daemon: generate the weekly marketing brief every Monday at 09:00 and post it
to Slack via an incoming webhook.

Uses the `schedule` library (already in requirements.txt). Keeps running in a
keep-alive loop; run it under a process manager (systemd, pm2, nohup, etc.) so it
survives restarts.

Env:
    SLACK_WEBHOOK_URL  — Slack incoming webhook. If unset, the brief is still
                         generated/saved but not posted (a reminder is printed).

Usage:
    python pipelines/schedule_brief.py          # start the daemon
    python pipelines/schedule_brief.py --now     # generate + post once, then exit
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import schedule
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from intelligence import weekly_brief

_RUN_DAY = "monday"
_RUN_TIME = "09:00"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def post_to_slack(text: str) -> bool:
    """Post text to Slack via SLACK_WEBHOOK_URL. Returns True if posted.

    If the webhook isn't configured, logs a reminder and returns False (the brief
    has still been generated and saved to docs/weekly_briefs/ by weekly_brief).
    """
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        _log("⚠️  SLACK_WEBHOOK_URL not set in .env — brief generated/saved but NOT "
             "posted to Slack. Add SLACK_WEBHOOK_URL to .env to enable posting.")
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=15)
        resp.raise_for_status()
        _log("Posted weekly brief to Slack.")
        return True
    except requests.RequestException as e:
        _log(f"Slack post FAILED: {e}")
        return False


def job() -> None:
    """Generate the weekly brief and post it to Slack."""
    _log("Generating weekly brief...")
    try:
        brief = weekly_brief.run()
    except Exception as e:  # noqa: BLE001 — never let one failure kill the daemon
        _log(f"Weekly brief generation FAILED: {e}")
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    message = f"*Speed Wallet — Weekly Marketing Brief ({today})*\n\n{brief}"
    post_to_slack(message)


def main() -> None:
    run_once = "--now" in sys.argv

    if not os.getenv("SLACK_WEBHOOK_URL"):
        _log("Reminder: SLACK_WEBHOOK_URL is not set in .env. The brief will still "
             "generate and save, but will not post to Slack until you add it.")

    if run_once:
        _log("Running once (--now), then exiting.")
        job()
        return

    schedule.every().monday.at(_RUN_TIME).do(job)
    _log(f"Scheduler started — weekly brief every {_RUN_DAY.title()} at {_RUN_TIME} "
         "(local server time). Press Ctrl-C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("Scheduler stopped.")
