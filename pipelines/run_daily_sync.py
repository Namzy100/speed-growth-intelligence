"""Daily orchestration: syncs Adjust install/retention and Meta campaign data to
Google Sheets, then rebuilds the creative performance dashboard.

Scope: this orchestrator syncs the Adjust pipeline (channel overview, campaign
installs, retention) and the Meta pipeline (campaign-level spend, impressions,
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

    # Meta — refreshes alongside Adjust each day.
    results["Meta"] = _sync_meta(spreadsheet_id)

    # Last Updated timestamp
    try:
        _write_last_updated(spreadsheet_id)
        results["Last Updated"] = True
    except Exception as e:
        _log(f"Last Updated: FAILED — {e}")
        results["Last Updated"] = False

    # Creative dashboard — final step, rebuilt from the freshly-synced data.
    results["Dashboard"] = _rebuild_dashboard()

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
