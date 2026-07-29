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

# ---------------------------------------------------------------------------
# Deposit signal — READ THIS BEFORE CHANGING THE METRIC NAME
#
# Adjust DOES carry a deposit event for this app. An earlier investigation
# concluded it did not, and that conclusion was wrong: it probed the key names
# `deposit`, `deposit_completed` and `first_deposit`, all of which correctly
# return HTTP 400 "Unsupported metric ... event doesn't exist or was renamed"
# because no event by those names is configured.
#
# The configured event is literally named "usd deposit", WITH A SPACE, so the
# report-service metric key is "usd deposit_m0_conversions_cohort". The
# underscore form `usd_deposit_m0_conversions_cohort` is rejected with that same
# "doesn't exist" error, which is exactly how the original probe was misled.
# Do not "tidy" the space into an underscore.
#
# Verified 2026-07-29 by probing 23 candidate deposit event names against the
# live API: "usd deposit" is the ONLY deposit event that exists, so this is the
# real deposit signal and not a USD-only slice of a larger set. The account also
# exposes "verified signups", "payment received", "payment sent" and
# "kyc completed" on the same naming pattern.
#
# `_m0_conversions_cohort` counts UNIQUE converters within month 0 of their
# install cohort, which is what a CPA wants: it aligns conversions with the
# install cohort the spend actually bought. The `_events` variant counts raw
# event fires (40,403 vs 3,343 over the same 27 days) including repeat deposits
# and users who installed long before the window, so it must NOT be used as a
# CPA numerator.
# ---------------------------------------------------------------------------
_DEPOSIT_EVENT = "usd deposit"
_DEPOSIT_METRIC = f"{_DEPOSIT_EVENT}_m0_conversions_cohort"

# Sheet/DataFrame column name for the above. The raw metric key contains a space
# and reads as an implementation detail, so it is renamed once, here.
_DEPOSIT_COLUMN = "deposits_m0"

# Retained users at D7, the denominator for Retention CPA. This is an absolute
# count, unlike retention_rate_d7 which is a ratio, so it can be divided into
# cost directly.
_RETAINED_METRIC = "retained_users_d7"

_NUMERIC: dict[str, list[str]] = {
    "channel_overview": ["installs", "impressions", "clicks", "ecpi"],
    "installs_by_campaign": ["installs", "cost", _RETAINED_METRIC, _DEPOSIT_COLUMN],
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
        """Per-campaign unit economics: installs, cost, D7 retained users, deposits.

        Carries the two denominators the campaign dashboard divides cost by:
        `retained_users_d7` and `deposits_m0` (see _DEPOSIT_METRIC above for why
        that key is spelled with a space upstream).

        Metrics are APPENDED to the existing installs/cost pair rather than
        replacing them, because this feeds the "Campaign Installs" sheet tab,
        which Looker Studio and the creative dashboard both read by column name.

        NOTE the caller's responsibility: the returned frame includes zero-cost
        organic rows, and a CPA computed over those is meaningless (55.7% of all
        deposits were organic when measured on 2026-07-29, so dividing paid spend
        by every deposit understates true paid CPA by ~123%). Filter to cost > 0
        before computing any cost-per-X. `build_campaigns` in
        pipelines/build_creative_dashboard.py does this.
        """
        rows = self._fetch(
            days=days,
            dimensions="channel,campaign_network",
            metrics=f"installs,cost,{_RETAINED_METRIC},{_DEPOSIT_METRIC}",
        )
        df = self._to_df(rows, _NUMERIC["installs_by_campaign"])
        if not df.empty and _DEPOSIT_METRIC in df.columns:
            df = df.rename(columns={_DEPOSIT_METRIC: _DEPOSIT_COLUMN})
            # rename happens after _to_df, so coerce the renamed column here
            df[_DEPOSIT_COLUMN] = pd.to_numeric(df[_DEPOSIT_COLUMN], errors="coerce")
        return df

    def get_installs_by_campaign_window(self, since_days: int, until_days: int) -> pd.DataFrame:
        """Installs + cost by channel/campaign for a custom window.

        Window is Adjust date_period "-{since_days}d:-{until_days}d" — e.g.
        (7, 1) = last 7 days (current week), (14, 8) = the prior 7-day week.
        Used for week-over-week campaign trend analysis.
        """
        rows = self._fetch(
            days=since_days,
            dimensions="channel,campaign_network",
            metrics="installs,cost",
            date_period=f"-{since_days}d:-{until_days}d",
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

    def _fetch(self, days: int, dimensions: str, metrics: str,
               date_period: str | None = None) -> list[dict]:
        params = {
            "date_period": date_period or f"-{days}d:-1d",
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
