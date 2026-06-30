"""Weekly intelligence brief generator for Speed Wallet marketing team."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
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
(hook: instant deposits/withdrawals), and crypto-curious mainstream users (hook: simplicity). \
Speed's primary markets are the US and EU for paid advertising, and the US, Mexico, and \
Brazil for influencer marketing — ground geographic interpretations in these markets.

Write a weekly performance brief for the marketing lead based on the data below. \
Cover exactly these points in clean flowing prose — no headers, no bullet points, \
no markdown formatting:

1. The top 3 findings from this week's data.
2. Which channel is performing most efficiently right now and why.
3. Which channel needs attention or a budget review and why.
4. One specific recommended action with a clear rationale.
5. D1 retention trend — improving, declining, or flat — and what it signals.
6. A short Competitor Context paragraph (2-3 sentences) summarizing what \
Robinhood and Crypto.com are currently emphasizing in their ads, drawn ONLY from \
the COMPETITOR ANALYSIS section below. If that section is empty or absent, omit \
this paragraph entirely.
7. A short EU Market Context paragraph (2-3 sentences) summarizing the top 3 \
recommended EU markets for Speed to enter first and why, drawn ONLY from the EU \
MARKET ANALYSIS section below. If that section is empty or absent, omit this \
paragraph entirely.

Keep it under 550 words. Write for a marketing lead, not a data scientist. \
Be direct and reference actual numbers from the data.

When interpreting the data, follow these rules:
- Treat every channel as distinct. "Google Ads" (paid user acquisition), \
"Google Organic Search" (unpaid app-store/search discovery), and "Organic" are \
SEPARATE channels. Never describe Google Ads installs as organic or as \
"misattributed as organic" — they are correctly attributed to the Google Ads channel.
- If a paid ad network (e.g. Google Ads, Apple Search Ads, Facebook) shows real \
installs but $0 cost and $0 eCPI, do NOT treat it as organic. Flag it as a \
cost/spend integration that is disconnected for that network, and recommend \
reconnecting it in the Adjust dashboard so spend and eCPI report correctly. \
Channels such as Organic, Website, Partnership, and Google Organic Search are \
genuinely unpaid and correctly show no cost.
- The retention data below already excludes immature cohorts (recent days whose \
DN window has not fully elapsed). Do not interpret a missing recent day or a \
low final data point as a retention drop or "collapse" — base the D1 trend only \
on the matured cohorts shown and the stated trend figure.

--- COMPETITOR ANALYSIS ---
{competitor_context}
--- END COMPETITOR ANALYSIS ---

--- EU MARKET ANALYSIS ---
{eu_context}
--- END EU MARKET ANALYSIS ---

--- DATA ---
{data_summary}
--- END DATA ---"""

# Competitor analyses to fold into the brief's Competitor Context paragraph.
# Reads the saved (US) analyses; absent files are skipped gracefully.
_COMPETITOR_FILES = [
    ("Robinhood", "competitor_analysis_robinhood.json"),
    ("Crypto.com", "competitor_analysis_crypto.com.json"),
]


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


def read_competitor_context() -> str:
    """Read saved competitor analyses into a compact block for the brief.

    Pulls each competitor's messaging angles, top CTAs, and summary from
    data/processed/. Returns an empty string if no usable files exist, in which
    case the prompt instructs the model to omit the Competitor Context paragraph.
    """
    data_dir = _ROOT / "data" / "processed"
    blocks = []
    for name, fname in _COMPETITOR_FILES:
        path = data_dir / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ma = data.get("messaging_analysis", {})
        if not ma or "parse_error" in ma:
            continue
        angles = ma.get("messaging_angles", [])[:4]
        ctas = ma.get("top_ctas", [])[:3]
        summary = ma.get("summary", "")
        blocks.append(
            f"{name} (country={data.get('country', '?')}, "
            f"{data.get('total_ads', '?')} ads):\n"
            f"  messaging_angles: {angles}\n"
            f"  top_ctas: {ctas}\n"
            f"  summary: {summary}"
        )
    return "\n\n".join(blocks)


def read_eu_context() -> str:
    """Read the latest EU market analysis and extract its top-3 recommendations.

    Finds the newest docs/eu_market_analysis_<date>.txt by filename (the
    YYYY_MM_DD suffix sorts chronologically), then returns the ranked "top 3
    markets" section for the brief's EU Market Context paragraph. Returns an
    empty string if no analysis file exists, so the prompt omits the paragraph.
    """
    docs_dir = _ROOT / "docs"
    files = sorted(docs_dir.glob("eu_market_analysis_*.txt"))
    if not files:
        return ""
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Prefer the ranked top-3 section; fall back to a capped tail if not found.
    upper = text.upper()
    start = upper.find("TOP 3")
    end = upper.find("SUGGESTED FIRST MOVE", start if start != -1 else 0)
    if start != -1:
        excerpt = text[start:end] if end != -1 else text[start:start + 1800]
    else:
        excerpt = text[-1800:]
    return f"(source: {latest.name})\n{excerpt.strip()}"


def read_sheets_data(spreadsheet_id: str) -> dict[str, pd.DataFrame]:
    """Read the three Adjust report tabs plus the Meta Campaigns tab.

    The Meta Campaigns tab (paid Facebook/Instagram spend & installs) is optional —
    it is skipped cleanly if absent so an older sheet still produces a brief.
    """
    spreadsheet = _sheets_client().open_by_key(spreadsheet_id)
    tabs = {
        "channel_overview": "Channel Overview",
        "installs_by_campaign": "Campaign Installs",
        "retention": "Retention",
    }
    out = {
        key: pd.DataFrame(spreadsheet.worksheet(name).get_all_records())
        for key, name in tabs.items()
    }
    try:
        out["meta_campaigns"] = pd.DataFrame(
            spreadsheet.worksheet("Meta Campaigns").get_all_records()
        )
    except gspread.WorksheetNotFound:
        out["meta_campaigns"] = pd.DataFrame()
    return out


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

    meta = data.get("meta_campaigns", pd.DataFrame())
    if not meta.empty:
        parts.append(_fmt_meta_campaigns(meta))

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
        cost_str = f"eCPI ${ecpi:.2f}" if ecpi > 0 else "$0 cost / $0 eCPI reported"
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


def _fmt_meta_campaigns(df: pd.DataFrame) -> str:
    """Summarise paid Meta (Facebook/Instagram) campaign spend & installs.

    Speed runs Meta as paid acquisition in the US + EU. Reports blended totals
    plus the top campaigns by spend with per-campaign eCPI and CTR.
    """
    df = df.copy()
    for col in ("spend", "impressions", "clicks", "mobile_app_install"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df = df.sort_values("spend", ascending=False)

    total_spend = float(df["spend"].sum()) if "spend" in df.columns else 0.0
    total_inst = int(df["mobile_app_install"].sum()) if "mobile_app_install" in df.columns else 0
    blended = total_spend / total_inst if total_inst > 0 else 0.0

    lines = [
        f"META ADS CAMPAIGNS — last 30 days (paid US/EU; total spend "
        f"${total_spend:,.2f}, {total_inst:,} app installs, blended eCPI "
        f"${blended:.2f})"
    ]
    for _, row in df.head(10).iterrows():
        spend = float(row.get("spend", 0))
        if spend == 0:
            continue
        inst = int(row.get("mobile_app_install", 0))
        ecpi = spend / inst if inst > 0 else 0
        ecpi_str = f"eCPI ${ecpi:.2f}" if inst > 0 else "no installs attributed"
        impr = int(row.get("impressions", 0))
        clicks = int(row.get("clicks", 0))
        ctr = (clicks / impr * 100) if impr > 0 else 0
        name = row.get("campaign_name", "") or "(unnamed)"
        lines.append(
            f"  {name}: ${spend:,.2f} spend, {inst:,} installs ({ecpi_str}), "
            f"{impr:,} impr, {clicks:,} clicks, CTR {ctr:.2f}%"
        )
    return "\n".join(lines)


def _fmt_retention(df: pd.DataFrame) -> str:
    df = df.copy()
    for col in df.columns:
        if col.startswith("retention_rate_"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "day" in df.columns:
        df = df.sort_values("day", ascending=False)

    # A DN retention figure is only valid once N full days have elapsed since the
    # cohort's install day. Cohorts where cohort_day + N >= today are still maturing
    # and read artificially low — including them produces false "collapse" alarms.
    today = datetime.now(timezone.utc).date()

    lines = ["D1 RETENTION BY DATE (most recent first; immature cohorts excluded)"]
    d1_values = []
    immature = 0
    for _, row in df.iterrows():
        day = row.get("day", "")
        try:
            cohort_date = datetime.strptime(str(day), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            cohort_date = None

        # D1 matures only after the day following the cohort has fully elapsed.
        if cohort_date is not None and cohort_date + timedelta(days=1) >= today:
            immature += 1
            continue

        d1 = float(row.get("retention_rate_d1", 0))
        if d1 == 0:
            continue
        d7 = float(row.get("retention_rate_d7", 0))
        d7_str = f", D7={d7:.1%}" if d7 > 0 else ""
        lines.append(f"  {day}: D1={d1:.1%}{d7_str}")
        d1_values.append(d1)

    if immature:
        lines.append(
            f"  ({immature} most recent cohort day(s) excluded — D1 not yet matured)"
        )

    if len(d1_values) >= 4:
        recent = d1_values[:7]
        older = d1_values[7:14]
        if older:
            recent_avg = sum(recent) / len(recent)
            older_avg = sum(older) / len(older)
            delta = recent_avg - older_avg
            trend = "improving" if delta > 0.01 else "declining" if delta < -0.01 else "flat"
            lines.append(
                f"  Trend (recent {len(recent)}d avg {recent_avg:.1%} vs prior "
                f"{len(older)}d avg {older_avg:.1%}): {trend} (Δ {delta:+.1%})"
            )

    return "\n".join(lines)


# ------------------------------------------------------------------
# Generate brief via Claude
# ------------------------------------------------------------------

def generate_brief(data_summary: str, competitor_context: str = "",
                   eu_context: str = "") -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    prompt = _PROMPT.format(
        data_summary=data_summary,
        competitor_context=competitor_context or "(no competitor data available)",
        eu_context=eu_context or "(no EU market analysis available)",
    )
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        # Headroom for the full brief: 5 analysis points plus the optional
        # Competitor Context and EU Market Context paragraphs. 1024 truncated
        # the EU paragraph mid-sentence when both context blocks were present.
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
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

    competitor_context = read_competitor_context()
    if competitor_context:
        print(f"Competitor context loaded ({competitor_context.count('country=')} competitor(s)).")
    else:
        print("No competitor analyses found — Competitor Context will be omitted.")

    eu_context = read_eu_context()
    if eu_context:
        print(f"EU market analysis loaded ({eu_context.splitlines()[0]}).")
    else:
        print("No EU market analysis found — EU Market Context will be omitted.")

    print("Calling Claude API (claude-sonnet-4-5)...")
    brief = generate_brief(summary, competitor_context, eu_context)

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
