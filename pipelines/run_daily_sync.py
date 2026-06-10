"""Daily orchestration: pulls all data sources and syncs to Google Sheets."""

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

from pipelines.adjust import AdjustPipeline
from pipelines.sheets import create_sheet_if_missing, write_dataframe

_ADJUST_SHEET_MAP = {
    "channel_overview": "Channel Overview",
    "installs_by_campaign": "Campaign Installs",
    "retention": "Retention",
}


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

    success = True
    for key, sheet_name in _ADJUST_SHEET_MAP.items():
        df = data.get(key)
        if df is None or df.empty:
            _log(f"Adjust → '{sheet_name}': skipped (no data)")
            continue
        try:
            create_sheet_if_missing(spreadsheet_id, sheet_name)
            write_dataframe(df, spreadsheet_id, sheet_name)
            _log(f"Adjust → '{sheet_name}': {len(df)} rows written")
        except Exception as e:
            _log(f"Adjust → '{sheet_name}': FAILED — {e}")
            success = False

    return success


def _write_last_updated(spreadsheet_id: str) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    df = pd.DataFrame({"last_sync": [now_str]})
    create_sheet_if_missing(spreadsheet_id, "Last Updated")
    write_dataframe(df, spreadsheet_id, "Last Updated")
    _log(f"Last Updated: '{now_str}'")


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

    # Last Updated timestamp
    try:
        _write_last_updated(spreadsheet_id)
        results["Last Updated"] = True
    except Exception as e:
        _log(f"Last Updated: FAILED — {e}")
        results["Last Updated"] = False

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
