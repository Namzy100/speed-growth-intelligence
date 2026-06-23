"""Campaign-level analysis from Adjust Campaign Installs + Meta Campaigns tabs.

Reads both campaign tabs from the Google Sheet, computes cost efficiency, and
asks Claude (claude-sonnet-4-6) for a structured analysis: top 5 campaigns by
installs, underperformers (high spend / low installs), and a specific
recommended action per underperformer. Saves to docs/campaign_analysis_<date>.txt
and prints to terminal.

Run from repo root:  python intelligence/campaign_analysis.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from pipelines import sheets  # reuse service-account auth + retry

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"


# ------------------------------------------------------------------
# Sheet helpers
# ------------------------------------------------------------------

def _num(x) -> float:
    """Tolerant numeric parse: strips $, commas, currency words; 0.0 on failure."""
    if x is None:
        return 0.0
    s = str(x).replace("$", "").replace(",", "").replace("USD", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def _records(ss, tab: str) -> list[dict]:
    ws = sheets._retry(lambda: ss.worksheet(tab))
    return sheets._retry(ws.get_all_records)


# ------------------------------------------------------------------
# Build the data summary fed to Claude
# ------------------------------------------------------------------

def build_data_summary(adjust_rows: list[dict], meta_rows: list[dict]) -> str:
    parts = []

    # Adjust Campaign Installs — installs + cost by channel/campaign.
    adj = []
    for r in adjust_rows:
        campaign = str(r.get("campaign_network", "")).strip()
        if not campaign or campaign.lower() == "unknown":
            continue
        installs = int(_num(r.get("installs")))
        cost = _num(r.get("cost"))
        if installs == 0 and cost == 0:
            continue
        ecpi = cost / installs if installs > 0 else 0.0
        adj.append({
            "channel": str(r.get("channel", "")).strip(),
            "campaign": campaign, "installs": installs, "cost": cost, "ecpi": ecpi,
        })
    adj.sort(key=lambda c: c["installs"], reverse=True)

    lines = ["ADJUST — INSTALLS BY CAMPAIGN (channel / campaign: installs, cost, eCPI)"]
    for c in adj[:25]:
        cost_str = f"${c['cost']:,.2f}" if c["cost"] > 0 else "$0 (no cost reported)"
        ecpi_str = f"${c['ecpi']:.2f}" if c["ecpi"] > 0 else "n/a"
        lines.append(f"  {c['channel']} / {c['campaign']}: {c['installs']:,} installs, "
                     f"{cost_str}, eCPI {ecpi_str}")
    parts.append("\n".join(lines))

    # Meta Campaigns — spend, clicks, app installs by campaign.
    meta = []
    for r in meta_rows:
        name = str(r.get("campaign_name", "")).strip()
        if not name:
            continue
        spend = _num(r.get("spend"))
        installs = int(_num(r.get("mobile_app_install")))
        clicks = int(_num(r.get("clicks")))
        impressions = int(_num(r.get("impressions")))
        cpi = spend / installs if installs > 0 else 0.0
        meta.append({"campaign": name, "spend": spend, "installs": installs,
                     "clicks": clicks, "impressions": impressions, "cpi": cpi})
    meta.sort(key=lambda c: c["spend"], reverse=True)

    total_spend = sum(c["spend"] for c in meta)
    total_meta_installs = sum(c["installs"] for c in meta)
    lines = [f"META — CAMPAIGN PERFORMANCE (total spend ${total_spend:,.2f}, "
             f"{total_meta_installs:,} app installs)"]
    for c in meta:
        cpi_str = f"${c['cpi']:.2f}" if c["cpi"] > 0 else "n/a (0 installs)"
        lines.append(f"  {c['campaign']}: ${c['spend']:,.2f} spend, {c['installs']:,} installs "
                     f"(cost/install {cpi_str}), {c['clicks']:,} clicks, {c['impressions']:,} impressions")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ------------------------------------------------------------------
# Claude analysis
# ------------------------------------------------------------------

_PROMPT = """\
You are a performance-marketing analyst for Speed Wallet, a Bitcoin Lightning \
payments app. Speed's primary markets are the US and EU for paid advertising, \
and the US, Mexico, and Brazil for influencer marketing.

Below is REAL campaign-level data pulled live from the dashboard: Adjust \
(SDK-measured installs and cost by campaign) and Meta (ad spend, clicks, and \
mobile-app-install counts by campaign). Note the two sources use different \
attribution — Adjust is cross-channel SDK; Meta is platform-reported — so treat \
their install counts as separate views, not additive.

Produce a structured campaign-level analysis in clean plain text with these \
three numbered sections and clear headers (no markdown tables, no code blocks):

1. TOP 5 CAMPAIGNS BY INSTALLS — list the five highest-install campaigns, each \
with its install count and cost efficiency (eCPI or cost-per-install). Note \
which source each comes from.

2. UNDERPERFORMING CAMPAIGNS — campaigns with high spend but low installs \
(poor cost-per-install). List each with its spend, installs, and CPI, and say \
briefly why it is underperforming.

3. RECOMMENDED ACTION PER UNDERPERFORMER — for EACH underperformer named in \
section 2, give one specific, concrete action (pause, cut budget by X%, shift \
to a proven campaign, refresh creative, fix tracking, etc.) with a one-line \
rationale grounded in its numbers.

Cite real numbers throughout. Be specific and direct. Keep it under 500 words.

--- CAMPAIGN DATA ---
{data_summary}
--- END DATA ---"""


def generate_analysis(data_summary: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": _PROMPT.format(data_summary=data_summary)}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Save + entrypoint
# ------------------------------------------------------------------

def save_analysis(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"campaign_analysis_{today}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def run() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    print("Opening Google Sheet (live)...")
    ss = sheets._open(sid)
    print("Reading tabs: Campaign Installs (Adjust), Meta Campaigns...")
    adjust_rows = _records(ss, "Campaign Installs")
    meta_rows = _records(ss, "Meta Campaigns")
    print(f"  Adjust campaign rows: {len(adjust_rows)} | Meta campaign rows: {len(meta_rows)}")

    summary = build_data_summary(adjust_rows, meta_rows)

    print(f"Generating campaign analysis ({_MODEL})...")
    analysis = generate_analysis(summary)

    path = save_analysis(analysis)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print("=" * 70)
    print(analysis)
    print("=" * 70)
    return analysis


if __name__ == "__main__":
    try:
        run()
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
