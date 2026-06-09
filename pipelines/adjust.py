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
    "retention": [f"retention_rate_d{d}" for d in [1, 2, 3, 4, 5, 6, 7, 14]],
}


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
        for attempt in range(3):
            resp = requests.get(
                _BASE_URL, headers=self._headers, params=params, timeout=30
            )
            if resp.status_code == 429:
                if attempt < 2:
                    time.sleep(5)
                    continue
                raise RuntimeError("Adjust API rate limit exceeded after 3 attempts.")
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
