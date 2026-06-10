"""Google Sheets writer for Looker Studio dashboards."""

import os
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv
from gspread.exceptions import SpreadsheetNotFound

load_dotenv()

_ADJUST_SHEET_MAP = {
    "channel_overview": "Channel Overview",
    "installs_by_campaign": "Campaign Installs",
    "retention": "Retention",
}


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

def _client() -> gspread.Client:
    creds_path = os.getenv("GOOGLE_SHEETS_CREDS")
    if not creds_path:
        raise EnvironmentError(
            "GOOGLE_SHEETS_CREDS must be set in .env — "
            "set it to the path of your Google service account JSON file."
        )
    if not Path(creds_path).is_file():
        raise FileNotFoundError(
            f"Credentials file not found: {creds_path}\n"
            "Download a service account JSON key from the Google Cloud Console "
            "and set GOOGLE_SHEETS_CREDS to its path."
        )
    return gspread.service_account(filename=creds_path)


def _open(spreadsheet_id: str) -> gspread.Spreadsheet:
    try:
        return _client().open_by_key(spreadsheet_id)
    except SpreadsheetNotFound:
        raise SpreadsheetNotFound(
            f"Spreadsheet '{spreadsheet_id}' not found. "
            "Check the ID and make sure the service account has editor access "
            "(share the sheet with the service account email address)."
        )
    except PermissionError as e:
        raise PermissionError(
            "Google Sheets API returned 403. Either:\n"
            "  1. The API is not enabled — visit the Google Cloud Console and enable "
            "'Google Sheets API' (and 'Google Drive API') for your project.\n"
            "  2. The service account has not been granted access to this spreadsheet."
        ) from e


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def create_sheet_if_missing(spreadsheet_id: str, sheet_name: str) -> gspread.Worksheet:
    """Add a new sheet tab if it doesn't already exist. Returns the worksheet.

    Args:
        spreadsheet_id: The Google Sheets file ID (from the URL).
        sheet_name:     The tab name to create.
    """
    spreadsheet = _open(spreadsheet_id)
    existing = {ws.title for ws in spreadsheet.worksheets()}
    if sheet_name not in existing:
        return spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=26)
    return spreadsheet.worksheet(sheet_name)


def write_dataframe(
    df: pd.DataFrame,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    """Write a DataFrame to a sheet, replacing all existing content.

    Column names become the header row. All existing content is cleared first.
    Values are written with USER_ENTERED input so dates and numbers are
    recognized natively by Google Sheets and Looker Studio.

    Args:
        df:              The DataFrame to write.
        spreadsheet_id:  The Google Sheets file ID.
        sheet_name:      The tab name to write to (must already exist).
    """
    worksheet = _open(spreadsheet_id).worksheet(sheet_name)
    values = _df_to_values(df)
    worksheet.clear()
    worksheet.update(values, value_input_option="USER_ENTERED")


def write_all_adjust_data(
    adjust_data_dict: dict[str, pd.DataFrame],
    spreadsheet_id: str,
) -> None:
    """Write each DataFrame from AdjustPipeline.get_all() to its own sheet tab.

    Tab names: Channel Overview, Campaign Installs, Retention.
    Tabs are created if they don't already exist.

    Args:
        adjust_data_dict:  Dict returned by AdjustPipeline.get_all().
        spreadsheet_id:    The Google Sheets file ID.
    """
    for key, sheet_name in _ADJUST_SHEET_MAP.items():
        df = adjust_data_dict.get(key)
        if df is None or df.empty:
            print(f"  Skipped: '{sheet_name}' (no data)")
            continue
        create_sheet_if_missing(spreadsheet_id, sheet_name)
        write_dataframe(df, spreadsheet_id, sheet_name)
        print(f"  Written: '{sheet_name}' ({len(df)} rows, {len(df.columns)} columns)")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _df_to_values(df: pd.DataFrame) -> list[list]:
    """Convert DataFrame to list-of-lists for gspread, replacing NaN with None."""
    headers = df.columns.tolist()
    # astype(object) lets us store None; .where() replaces NaN with None
    clean = df.astype(object).where(pd.notna(df), None)
    return [headers] + clean.values.tolist()


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    spreadsheet_id = os.getenv("TEST_SPREADSHEET_ID")
    if not spreadsheet_id:
        print(
            "Set TEST_SPREADSHEET_ID in .env to the ID of a Google Sheet that the\n"
            "service account has editor access to, then re-run."
        )
        sys.exit(1)

    SAMPLE = pd.DataFrame(
        {
            "channel": ["Organic", "Google Ads", "Facebook"],
            "installs": [120, 85, 43],
            "ecpi": [0.0, 4.12, 5.67],
            "date": ["2026-06-01", "2026-06-01", "2026-06-01"],
        }
    )

    sheet_name = "_sheets_test"
    print(f"Writing sample DataFrame to '{sheet_name}' in spreadsheet {spreadsheet_id}...")

    try:
        create_sheet_if_missing(spreadsheet_id, sheet_name)
        write_dataframe(SAMPLE, spreadsheet_id, sheet_name)
    except (EnvironmentError, FileNotFoundError, PermissionError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
    except SpreadsheetNotFound as e:
        print(f"Sheets error: {e}")
        sys.exit(1)

    print(f"Done. Open the sheet and verify '{sheet_name}' has 3 data rows + 1 header.")
