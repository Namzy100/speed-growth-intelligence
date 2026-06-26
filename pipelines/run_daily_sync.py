"""Daily orchestration: syncs Adjust install/retention and Meta campaign data to
Google Sheets, then rebuilds the creative performance dashboard.

Scope: this orchestrator syncs the Adjust pipeline (channel overview, campaign
installs, installs by country, retention) and the Meta pipeline (campaign-level spend, impressions,
clicks, mobile app installs), writes a "Last Updated" timestamp, and — as a
final step — rebuilds the self-contained creative dashboard HTML
(docs/creative_dashboard.html) from the freshly-synced sheet data, so it stays
current on every run. The other Speed data sources are standalone and are NOT
yet wired into this orchestrator:

  - creators/      — TikTok/YouTube creator discovery + scoring (persisted to
                     Supabase, run on their own)
  - eu/            — European market analysis (currently unimplemented)
  - intelligence/  — weekly_brief.py and competitor_analysis.py (run manually)

Add them here as separate steps in run() when they are ready for automation.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Allow running directly from any working directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from pipelines import build_creative_dashboard
from pipelines.adjust import AdjustPipeline
from pipelines.meta import MetaPipeline
from pipelines.sheets import (
    create_sheet_if_missing,
    write_all_adjust_data,
    write_all_meta_data,
    write_country_installs,
    write_dataframe,
)


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------------------------------------------
# Pipeline steps
# ------------------------------------------------------------------

def _sync_adjust(spreadsheet_id: str) -> bool:
    """Pull Adjust data and write each report to its sheet tab.

    Returns True if all writes succeeded, False if any step failed.
    Individual sheet failures are logged but do not abort the others.
    """
    _log("Adjust: pulling last 30 days...")
    try:
        data = AdjustPipeline().get_all(days=30)
    except Exception as e:
        _log(f"Adjust: pull FAILED — {e}")
        return False

    # Single source of truth for the write loop lives in sheets.py.
    return write_all_adjust_data(data, spreadsheet_id, log=_log)


def _sync_country_installs(spreadsheet_id: str) -> bool:
    """Pull Adjust installs broken down by country and write the Country Installs tab.

    Returns True if the write succeeded (or was an empty no-op), False on failure.
    """
    _log("Adjust (country): pulling installs by country, last 30 days...")
    try:
        df = AdjustPipeline().get_installs_by_country(days=30)
    except Exception as e:
        _log(f"Adjust (country): pull FAILED — {e}")
        return False

    return write_country_installs(df, spreadsheet_id, log=_log)


def _sync_meta(spreadsheet_id: str) -> bool:
    """Pull Meta campaign data and write it to the Meta Campaigns tab.

    Returns True if all writes succeeded, False if any step failed.
    Individual sheet failures are logged but do not abort the others.
    """
    _log("Meta: pulling last 30 days...")
    try:
        data = MetaPipeline().get_all(days=30)
    except Exception as e:
        _log(f"Meta: pull FAILED — {e}")
        return False

    # Single source of truth for the write loop lives in sheets.py.
    return write_all_meta_data(data, spreadsheet_id, log=_log)


def _write_last_updated(spreadsheet_id: str) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    df = pd.DataFrame({"last_sync": [now_str]})
    create_sheet_if_missing(spreadsheet_id, "Last Updated")
    write_dataframe(df, spreadsheet_id, "Last Updated")
    _log(f"Last Updated: '{now_str}'")


def _rebuild_dashboard() -> bool:
    """Rebuild the creative dashboard HTML from the freshly-synced sheet data.

    Runs last so it reflects the Adjust writes and the new Last Updated stamp.
    Pulls the sheet again and regenerates docs/creative_dashboard.html.
    """
    _log("Dashboard: rebuilding docs/creative_dashboard.html from latest data...")
    try:
        build_creative_dashboard.main()
        _log("Dashboard: rebuilt successfully")
        return True
    except Exception as e:
        _log(f"Dashboard: rebuild FAILED — {e}")
        return False


def _deploy_dashboard() -> bool:
    """Push the rebuilt dashboard HTML to GitHub so Vercel auto-deploys it.

    Gated behind DASHBOARD_AUTODEPLOY (set it to a truthy value in the scheduled
    environment to enable). Commits ONLY the dashboard HTML files, and only if
    they actually changed, then pushes. Best-effort: a git/push failure is logged
    but never aborts the sync. Requires git push credentials in the environment.
    """
    if not os.getenv("DASHBOARD_AUTODEPLOY"):
        _log("Auto-deploy: skipped (set DASHBOARD_AUTODEPLOY=1 to push dashboards "
             "to GitHub for Vercel).")
        return True

    files = ["docs/creative_dashboard.html", "docs/creator_dashboard.html"]
    try:
        subprocess.run(["git", "add", *files], cwd=_ROOT, check=True)
        # Nothing staged → no change to deploy.
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=_ROOT).returncode == 0:
            _log("Auto-deploy: no dashboard changes to push.")
            return True
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(
            ["git", "commit", "-m", f"chore: auto-deploy dashboard refresh {stamp}"],
            cwd=_ROOT, check=True,
        )
        subprocess.run(["git", "push"], cwd=_ROOT, check=True)
        _log("Auto-deploy: pushed dashboard update — Vercel will redeploy.")
        return True
    except Exception as e:  # noqa: BLE001 — deploy must never break the data sync
        _log(f"Auto-deploy: FAILED (non-fatal) — {e}")
        return False


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def run() -> None:
    _log("Starting daily sync")

    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        _log("FATAL: GOOGLE_SHEETS_ID must be set in .env")
        sys.exit(1)

    results: dict[str, bool] = {}

    # Adjust
    results["Adjust"] = _sync_adjust(spreadsheet_id)

    # Adjust installs-by-country — feeds the EU market analysis.
    results["Country Installs"] = _sync_country_installs(spreadsheet_id)

    # Meta — refreshes alongside Adjust each day.
    results["Meta"] = _sync_meta(spreadsheet_id)

    # Last Updated timestamp
    try:
        _write_last_updated(spreadsheet_id)
        results["Last Updated"] = True
    except Exception as e:
        _log(f"Last Updated: FAILED — {e}")
        results["Last Updated"] = False

    # Creative dashboard — rebuilt from the freshly-synced data.
    results["Dashboard"] = _rebuild_dashboard()

    # Auto-deploy: push the refreshed dashboard to GitHub → Vercel (opt-in).
    results["Deploy"] = _deploy_dashboard()

    # Summary
    succeeded = sum(results.values())
    total = len(results)
    _log(f"Sync complete — {succeeded}/{total} steps succeeded")
    if succeeded < total:
        failed = [name for name, ok in results.items() if not ok]
        _log(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    run()
