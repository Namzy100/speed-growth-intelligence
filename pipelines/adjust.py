"""Adjust KPI Service pipeline for install and attribution reporting."""

import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

_BASE_URL = "https://automate.adjust.com/reports-service/report"

_RETENTION_METRICS = ",".join(
    [f"retention_rate_d{d}" for d in [1, 2, 3, 4, 5, 6, 7, 14]]
)

_NUMERIC: dict[str, list[str]] = {
    "channel_overview": ["installs", "impressions", "clicks", "ecpi"],
    "installs_by_campaign": ["installs", "cost"],
    "installs_by_country": ["installs"],
    "retention": [f"retention_rate_d{d}" for d in [1, 2, 3, 4, 5, 6, 7, 14]],
}

# Retry policy for transient failures (network errors, timeouts, 5xx, 429).
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.0          # exponential: 1s, 2s, 4s between retries
_RATE_LIMIT_WAIT_SECONDS = 5.0       # fallback wait for 429 when no Retry-After


class AdjustPipeline:
    """Pulls install and attribution data from the Adjust KPI Service API v1."""

    def __init__(self) -> None:
        api_key = os.getenv("ADJUST_API_KEY")
        if not api_key:
            raise EnvironmentError("ADJUST_API_KEY must be set in .env")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_channel_overview(self, days: int = 30) -> pd.DataFrame:
        """Channel Overview: installs, impressions, clicks, eCPI by channel."""
        rows = self._fetch(
            days=days,
            dimensions="channel",
            metrics="installs,impressions,clicks,ecpi",
        )
        return self._to_df(rows, _NUMERIC["channel_overview"])

    def get_installs_by_campaign(self, days: int = 30) -> pd.DataFrame:
        """Installs and cost by channel and campaign."""
        rows = self._fetch(
            days=days,
            dimensions="channel,campaign_network",
            metrics="installs,cost",
        )
        return self._to_df(rows, _NUMERIC["installs_by_campaign"])

    def get_installs_by_country(self, days: int = 30) -> pd.DataFrame:
        """Installs broken down by country (ISO code) for the last `days`."""
        rows = self._fetch(
            days=days,
            dimensions="country",
            metrics="installs",
        )
        return self._to_df(rows, _NUMERIC["installs_by_country"])

    def get_retention(self, days: int = 30) -> pd.DataFrame:
        """D1–D7 and D14 retention rates by day."""
        rows = self._fetch(
            days=days,
            dimensions="day",
            metrics=_RETENTION_METRICS,
        )
        return self._to_df(rows, _NUMERIC["retention"])

    def get_all(self, days: int = 30) -> dict[str, pd.DataFrame]:
        """Run all three reports and return a dict of DataFrames."""
        return {
            "channel_overview": self.get_channel_overview(days),
            "installs_by_campaign": self.get_installs_by_campaign(days),
            "retention": self.get_retention(days),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch(self, days: int, dimensions: str, metrics: str) -> list[dict]:
        params = {
            "date_period": f"-{days}d:-1d",
            "dimensions": dimensions,
            "metrics": metrics,
            "reattributed": "all",
            "attribution_source": "first",
            "attribution_type": "all",
            "sandbox": "false",
            "format": "json",
        }
        for attempt in range(_MAX_ATTEMPTS):
            is_last = attempt == _MAX_ATTEMPTS - 1
            backoff = _BACKOFF_BASE_SECONDS * (2 ** attempt)

            # Network-level transient failures: timeouts and connection errors.
            try:
                resp = requests.get(
                    _BASE_URL, headers=self._headers, params=params, timeout=30
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                if is_last:
                    raise RuntimeError(
                        f"Adjust request failed after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                time.sleep(backoff)
                continue

            # 429 rate limiting — handled distinctly from other transient errors.
            # Honour the Retry-After header when present, else use a fixed wait.
            if resp.status_code == 429:
                if is_last:
                    raise RuntimeError(
                        f"Adjust API rate limit exceeded after {_MAX_ATTEMPTS} attempts."
                    )
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else _RATE_LIMIT_WAIT_SECONDS
                time.sleep(wait)
                continue

            # 5xx server errors are transient — retry with exponential backoff.
            if resp.status_code >= 500:
                if is_last:
                    resp.raise_for_status()
                time.sleep(backoff)
                continue

            # Success or a non-retryable 4xx — raise on the latter, return on success.
            resp.raise_for_status()
            return resp.json().get("rows", [])
        return []

    @staticmethod
    def _to_df(rows: list[dict], numeric_cols: list[str]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    try:
        pipeline = AdjustPipeline()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    print("Fetching Adjust data for the last 7 days...\n")

    try:
        reports = pipeline.get_all(days=7)
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
            print(df.head().to_string(index=False))
        print()
