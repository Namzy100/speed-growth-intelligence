"""Google Sheets writer for Looker Studio dashboards."""

import os
import time
from pathlib import Path
from typing import Callable, TypeVar

import gspread
import pandas as pd
from dotenv import load_dotenv
from gspread.exceptions import APIError, SpreadsheetNotFound
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

load_dotenv()

# Single source of truth for the Adjust report → sheet-tab mapping.
_ADJUST_SHEET_MAP = {
    "channel_overview": "Channel Overview",
    "installs_by_campaign": "Campaign Installs",
    "retention": "Retention",
}

# Single source of truth for the Meta report → sheet-tab mapping.
_META_SHEET_MAP = {
    "campaign_performance": "Meta Campaigns",
}

# Retry policy for transient Sheets API failures (5xx / 429 / network / timeout).
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.0          # exponential: 1s, 2s, 4s between retries

_T = TypeVar("_T")


def _is_transient_api_error(e: APIError) -> bool:
    """True for Sheets APIErrors worth retrying: 5xx server errors and 429 quota."""
    response = getattr(e, "response", None)
    code = getattr(response, "status_code", None)
    return code is not None and (code >= 500 or code == 429)


def _retry(func: Callable[[], _T]) -> _T:
    """Run a gspread call, retrying transient failures with exponential backoff.

    Retries on 5xx/429 APIErrors and on connection/timeout errors. Non-transient
    errors (e.g. 403/404) are raised immediately without retry.
    """
    for attempt in range(_MAX_ATTEMPTS):
        is_last = attempt == _MAX_ATTEMPTS - 1
        try:
            return func()
        except APIError as e:
            if not _is_transient_api_error(e) or is_last:
                raise
        except (RequestsConnectionError, RequestsTimeout):
            if is_last:
                raise
        time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise RuntimeError("unreachable: retry loop exhausted")  # pragma: no cover


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
        return _retry(lambda: _client().open_by_key(spreadsheet_id))
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
    existing = {ws.title for ws in _retry(spreadsheet.worksheets)}
    if sheet_name not in existing:
        return _retry(lambda: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=26))
    return _retry(lambda: spreadsheet.worksheet(sheet_name))


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
    worksheet = _retry(lambda: _open(spreadsheet_id).worksheet(sheet_name))
    values = _df_to_values(df)
    _retry(worksheet.clear)
    _retry(lambda: worksheet.update(values, value_input_option="USER_ENTERED"))


def write_all_adjust_data(
    adjust_data_dict: dict[str, pd.DataFrame],
    spreadsheet_id: str,
    log: Callable[[str], None] = print,
) -> bool:
    """Write each DataFrame from AdjustPipeline.get_all() to its own sheet tab.

    Tab names: Channel Overview, Campaign Installs, Retention. Tabs are created
    if they don't already exist. This is the single place the Adjust → sheets
    write loop lives; callers (e.g. run_daily_sync) should use it rather than
    reimplementing the loop.

    Individual tab failures are logged and do not abort the remaining tabs.

    Args:
        adjust_data_dict:  Dict returned by AdjustPipeline.get_all().
        spreadsheet_id:    The Google Sheets file ID.
        log:               Callable used for progress/error lines (defaults to print;
                           pass a timestamped logger from the orchestrator).

    Returns:
        True if every non-empty report was written, False if any tab failed.
    """
    return _write_reports(
        adjust_data_dict, _ADJUST_SHEET_MAP, "Adjust", spreadsheet_id, log
    )


def write_all_meta_data(
    meta_data_dict: dict[str, pd.DataFrame],
    spreadsheet_id: str,
    log: Callable[[str], None] = print,
) -> bool:
    """Write each DataFrame from MetaPipeline.get_all() to its own sheet tab.

    Tab name: Meta Campaigns. Tabs are created if they don't already exist.
    Mirrors write_all_adjust_data; callers should use it rather than
    reimplementing the loop.

    Args:
        meta_data_dict:    Dict returned by MetaPipeline.get_all().
        spreadsheet_id:    The Google Sheets file ID.
        log:               Callable used for progress/error lines (defaults to print;
                           pass a timestamped logger from the orchestrator).

    Returns:
        True if every non-empty report was written, False if any tab failed.
    """
    return _write_reports(
        meta_data_dict, _META_SHEET_MAP, "Meta", spreadsheet_id, log
    )


def write_country_installs(
    df: pd.DataFrame,
    spreadsheet_id: str,
    log: Callable[[str], None] = print,
) -> bool:
    """Write the Adjust installs-by-country DataFrame to the 'Country Installs' tab.

    Created if missing. Returns True on success (or a no-op skip when the frame
    is empty), False if the write failed.
    """
    if df is None or df.empty:
        log("Adjust → 'Country Installs': skipped (no data)")
        return True
    try:
        create_sheet_if_missing(spreadsheet_id, "Country Installs")
        write_dataframe(df, spreadsheet_id, "Country Installs")
        log(f"Adjust → 'Country Installs': {len(df)} rows written")
        return True
    except Exception as e:
        log(f"Adjust → 'Country Installs': FAILED — {e}")
        return False


def _write_reports(
    data_dict: dict[str, pd.DataFrame],
    sheet_map: dict[str, str],
    source_label: str,
    spreadsheet_id: str,
    log: Callable[[str], None],
) -> bool:
    """Write a {report_key: DataFrame} dict to its mapped sheet tabs.

    Shared write loop for the Adjust and Meta sources. Individual tab failures
    are logged and do not abort the remaining tabs.

    Returns True if every non-empty report was written, False if any tab failed.
    """
    success = True
    for key, sheet_name in sheet_map.items():
        df = data_dict.get(key)
        if df is None or df.empty:
            log(f"{source_label} → '{sheet_name}': skipped (no data)")
            continue
        try:
            create_sheet_if_missing(spreadsheet_id, sheet_name)
            write_dataframe(df, spreadsheet_id, sheet_name)
            log(f"{source_label} → '{sheet_name}': {len(df)} rows written")
        except Exception as e:
            log(f"{source_label} → '{sheet_name}': FAILED — {e}")
            success = False
    return success


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
