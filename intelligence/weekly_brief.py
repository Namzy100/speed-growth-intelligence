"""Weekly intelligence brief generator for Speed Wallet marketing team."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import gspread
import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

_DOCS_DIR = _ROOT / "docs" / "weekly_briefs"

_PROMPT = """\
You are a marketing analyst for Speed Wallet, a Bitcoin Lightning payment app. \
Speed targets three segments: remittance senders (hook: zero fees), iGaming users \
(hook: instant deposits/withdrawals), and crypto-curious mainstream users (hook: simplicity).

Write a weekly performance brief for the marketing lead based on the data below. \
Cover exactly these five points in clean flowing prose — no headers, no bullet points, \
no markdown formatting:

1. The top 3 findings from this week's data.
2. Which channel is performing most efficiently right now and why.
3. Which channel needs attention or a budget review and why.
4. One specific recommended action with a clear rationale.
5. D1 retention trend — improving, declining, or flat — and what it signals.

Keep it under 400 words. Write for a marketing lead, not a data scientist. \
Be direct and reference actual numbers from the data.

--- DATA ---
{data_summary}
--- END DATA ---"""


# ------------------------------------------------------------------
# Read from Google Sheets
# ------------------------------------------------------------------

def _sheets_client() -> gspread.Client:
    creds_path = os.getenv("GOOGLE_SHEETS_CREDS")
    if not creds_path:
        raise EnvironmentError("GOOGLE_SHEETS_CREDS must be set in .env")
    if not Path(creds_path).is_file():
        raise FileNotFoundError(f"Credentials file not found: {creds_path}")
    return gspread.service_account(filename=creds_path)


def read_sheets_data(spreadsheet_id: str) -> dict[str, pd.DataFrame]:
    """Read the three Adjust report tabs from the spreadsheet."""
    spreadsheet = _sheets_client().open_by_key(spreadsheet_id)
    tabs = {
        "channel_overview": "Channel Overview",
        "installs_by_campaign": "Campaign Installs",
        "retention": "Retention",
    }
    return {
        key: pd.DataFrame(spreadsheet.worksheet(name).get_all_records())
        for key, name in tabs.items()
    }


# ------------------------------------------------------------------
# Format data for Claude
# ------------------------------------------------------------------

def build_data_summary(data: dict[str, pd.DataFrame]) -> str:
    parts = []

    co = data.get("channel_overview", pd.DataFrame())
    if not co.empty:
        parts.append(_fmt_channel_overview(co))

    ci = data.get("installs_by_campaign", pd.DataFrame())
    if not ci.empty:
        parts.append(_fmt_campaign_installs(ci))

    ret = data.get("retention", pd.DataFrame())
    if not ret.empty:
        parts.append(_fmt_retention(ret))

    return "\n\n".join(parts)


def _fmt_channel_overview(df: pd.DataFrame) -> str:
    df = df.copy()
    for col in ("installs", "impressions", "clicks", "ecpi"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df = df.sort_values("installs", ascending=False)

    total = int(df["installs"].sum())
    lines = [f"CHANNEL OVERVIEW — last 30 days (total installs: {total:,})"]
    for _, row in df.iterrows():
        installs = int(row.get("installs", 0))
        if installs == 0:
            continue
        ecpi = float(row.get("ecpi", 0))
        cost_str = f"eCPI ${ecpi:.2f}" if ecpi > 0 else "organic / no paid cost"
        impr = int(row.get("impressions", 0))
        clicks = int(row.get("clicks", 0))
        reach = f", {impr:,} impressions, {clicks:,} clicks" if impr > 0 else ""
        lines.append(f"  {row['channel']}: {installs:,} installs{reach} ({cost_str})")
    return "\n".join(lines)


def _fmt_campaign_installs(df: pd.DataFrame) -> str:
    df = df.copy()
    for col in ("installs", "cost"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df = df.sort_values("installs", ascending=False).head(10)

    lines = ["TOP 10 CAMPAIGNS BY INSTALLS"]
    for _, row in df.iterrows():
        installs = int(row.get("installs", 0))
        if installs == 0:
            continue
        cost = float(row.get("cost", 0))
        ecpi = cost / installs if installs > 0 else 0
        cost_str = f"cost ${cost:,.2f} (eCPI ${ecpi:.2f})" if cost > 0 else "no paid cost"
        channel = row.get("channel", "")
        campaign = row.get("campaign_network", "")
        lines.append(f"  {channel} / {campaign}: {installs:,} installs, {cost_str}")
    return "\n".join(lines)


def _fmt_retention(df: pd.DataFrame) -> str:
    df = df.copy()
    for col in df.columns:
        if col.startswith("retention_rate_"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "day" in df.columns:
        df = df.sort_values("day", ascending=False)

    lines = ["D1 RETENTION BY DATE (most recent first)"]
    d1_values = []
    for _, row in df.iterrows():
        d1 = float(row.get("retention_rate_d1", 0))
        if d1 == 0:
            continue
        day = row.get("day", "")
        d7 = float(row.get("retention_rate_d7", 0))
        d7_str = f", D7={d7:.1%}" if d7 > 0 else ""
        lines.append(f"  {day}: D1={d1:.1%}{d7_str}")
        d1_values.append(d1)

    if len(d1_values) >= 3:
        recent = d1_values[:7]
        older = d1_values[7:]
        if older:
            delta = recent[-1] - older[0]
            trend = "improving" if delta > 0.01 else "declining" if delta < -0.01 else "flat"
            lines.append(f"  Trend (last 7 days vs prior): {trend} (Δ {delta:+.1%})")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Generate brief via Claude
# ------------------------------------------------------------------

def generate_brief(data_summary: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": _PROMPT.format(data_summary=data_summary)}],
    )
    return response.content[0].text


# ------------------------------------------------------------------
# Save brief
# ------------------------------------------------------------------

def save_brief(brief: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"brief_{today}.txt"
    path.write_text(brief, encoding="utf-8")
    return path


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def run() -> str:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    print("Reading data from Google Sheets...")
    data = read_sheets_data(spreadsheet_id)
    for key, df in data.items():
        print(f"  {key}: {len(df)} rows")

    print("\nBuilding data summary...")
    summary = build_data_summary(data)

    print("Calling Claude API (claude-sonnet-4-5)...")
    brief = generate_brief(summary)

    path = save_brief(brief)
    print(f"\nSaved: {path.relative_to(_ROOT)}")
    print(f"\n{'=' * 60}")
    print(brief)
    print(f"{'=' * 60}")
    print(f"\n~{len(brief.split())} words")

    return brief


if __name__ == "__main__":
    try:
        run()
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
