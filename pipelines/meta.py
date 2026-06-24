"""Meta (Facebook/Instagram) Ads pipeline for campaign spend and install reporting.

Pulls campaign-level insights from the Meta Graph Marketing API. Unlike the
meta-ads MCP server (which is only available inside an interactive Claude
session), this hits graph.facebook.com directly with a long-lived access token,
so it can run unattended from run_daily_sync. The Graph API exposes the full
`actions` array, which is where true `mobile_app_install` counts live — for
every campaign, not just those whose primary result is "Mobile app installs".

Required .env keys:
    META_ACCESS_TOKEN   — long-lived access token with ads_read on the account
    META_AD_ACCOUNT_ID  — optional; defaults to act_1771013173838856
"""

import os
import re
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()


def _redact_token(text: str) -> str:
    """Strip the access_token value from a URL/message so it never reaches logs."""
    return re.sub(r"(access_token=)[^&\s]+", r"\1<redacted>", text or "")


_API_VERSION = "v23.0"
_BASE_URL = f"https://graph.facebook.com/{_API_VERSION}"
_DEFAULT_ACCOUNT_ID = "act_1771013173838856"

# The Graph API returns the install count inside the `actions` array under one
# of these action_types depending on objective/attribution. We sum the first
# one present per campaign, preferring the platform-specific mobile install.
_INSTALL_ACTION_TYPES = ("mobile_app_install", "omni_app_install", "app_install")

_NUMERIC_COLS = ["spend", "impressions", "clicks", "mobile_app_install"]

# Retry policy for transient failures (network errors, timeouts, 5xx, 429).
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.0          # exponential: 1s, 2s, 4s between retries
_RATE_LIMIT_WAIT_SECONDS = 5.0       # fallback wait for 429 when no Retry-After


class MetaPipeline:
    """Pulls campaign-level insights from the Meta Graph Marketing API."""

    def __init__(self) -> None:
        token = os.getenv("META_ACCESS_TOKEN")
        if not token:
            raise EnvironmentError("META_ACCESS_TOKEN must be set in .env")
        self._token = token
        self._account_id = os.getenv("META_AD_ACCOUNT_ID", _DEFAULT_ACCOUNT_ID)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_campaign_performance(self, days: int = 30) -> pd.DataFrame:
        """Spend, impressions, clicks, and mobile app installs by campaign."""
        rows = self._fetch(
            days=days,
            fields="campaign_id,campaign_name,spend,impressions,clicks,actions",
        )
        return self._to_df(rows)

    def get_all(self, days: int = 30) -> dict[str, pd.DataFrame]:
        """Run all reports and return a dict of DataFrames (mirrors AdjustPipeline)."""
        return {
            "campaign_performance": self.get_campaign_performance(days),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch(self, days: int, fields: str) -> list[dict]:
        """Fetch insights, following pagination, with retry on transient errors."""
        url: str | None = f"{_BASE_URL}/{self._account_id}/insights"
        params: dict | None = {
            "level": "campaign",
            "fields": fields,
            "date_preset": _date_preset(days),
            "limit": 500,
            "access_token": self._token,
        }
        all_rows: list[dict] = []

        while url:
            page = self._fetch_page(url, params)
            all_rows.extend(page.get("data", []))
            # Cursor-based pagination: the `next` URL already carries all params
            # (including the access token), so subsequent calls pass none.
            url = page.get("paging", {}).get("next")
            params = None

        return all_rows

    def _fetch_page(self, url: str, params: dict | None) -> dict:
        for attempt in range(_MAX_ATTEMPTS):
            is_last = attempt == _MAX_ATTEMPTS - 1
            backoff = _BACKOFF_BASE_SECONDS * (2 ** attempt)

            # Network-level transient failures: timeouts and connection errors.
            # str(e) can embed the full request URL (incl. access_token), so the
            # message is redacted and the cause is not chained (`from None`).
            try:
                resp = requests.get(url, params=params, timeout=30)
            except (requests.Timeout, requests.ConnectionError) as e:
                if is_last:
                    raise RuntimeError(
                        f"Meta request failed after {_MAX_ATTEMPTS} attempts: "
                        f"{_redact_token(str(e))}"
                    ) from None
                time.sleep(backoff)
                continue

            # 429 rate limiting — honour Retry-After when present, else fixed wait.
            if resp.status_code == 429:
                if is_last:
                    raise RuntimeError(
                        f"Meta API rate limit exceeded after {_MAX_ATTEMPTS} attempts."
                    )
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else _RATE_LIMIT_WAIT_SECONDS
                time.sleep(wait)
                continue

            # 5xx server errors are transient — retry with exponential backoff.
            if resp.status_code >= 500:
                if is_last:
                    raise RuntimeError(
                        f"Meta API error {resp.status_code} after {_MAX_ATTEMPTS} "
                        f"attempts: {_redact_token(resp.url)}"
                    )
                time.sleep(backoff)
                continue

            # Non-retryable 4xx — raise with the access_token stripped from the
            # URL (raise_for_status would leak it in the HTTPError message).
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Meta API error {resp.status_code}: {_redact_token(resp.url)} "
                    f"— {_redact_token(resp.text[:300])}"
                )

            return resp.json()
        return {}

    @staticmethod
    def _to_df(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        records = []
        for row in rows:
            records.append(
                {
                    "campaign_id": row.get("campaign_id"),
                    "campaign_name": row.get("campaign_name"),
                    "spend": row.get("spend"),
                    "impressions": row.get("impressions"),
                    "clicks": row.get("clicks"),
                    "mobile_app_install": _extract_installs(row.get("actions")),
                }
            )

        df = pd.DataFrame(records)
        for col in _NUMERIC_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df


def _date_preset(days: int) -> str:
    """Map a day count to a Graph API date_preset, falling back to last_30d."""
    return {7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d"}.get(
        days, "last_30d"
    )


def _extract_installs(actions: list[dict] | None) -> int:
    """Pull the mobile app install count from a Graph API `actions` array.

    Returns the value of the first matching action_type present (preferring the
    platform-specific `mobile_app_install`), or 0 if none are reported.
    """
    if not actions:
        return 0
    by_type = {a.get("action_type"): a.get("value", 0) for a in actions}
    for action_type in _INSTALL_ACTION_TYPES:
        if action_type in by_type:
            try:
                return int(float(by_type[action_type]))
            except (TypeError, ValueError):
                return 0
    return 0


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    try:
        pipeline = MetaPipeline()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    print("Fetching Meta campaign data for the last 30 days...\n")

    try:
        reports = pipeline.get_all(days=30)
    except requests.HTTPError as e:
        print(f"API error: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"Request error: {e}")
        sys.exit(1)

    for name, df in reports.items():
        print(f"{'=' * 60}")
        print(f"Report : {name}")
        if df.empty:
            print("  (no data returned)")
        else:
            print(f"  Rows   : {len(df)}")
            print(f"  Columns: {list(df.columns)}")
            print(df.to_string(index=False))
        print()
