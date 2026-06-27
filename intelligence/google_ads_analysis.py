"""Google Ads campaign analysis from the Adjust data in the Google Sheet.

Reads the Google Ads campaign rows from the Campaign Installs tab (installs +
cost per campaign) and the Google Ads channel row from Channel Overview
(impressions + clicks), computes efficiency metrics, and asks Claude
(claude-sonnet-4-6) which campaigns are performing, which aren't, and what
creative/keyword themes are working. Saves to docs/google_ads_analysis_<date>.txt.

DATA NOTE: the Campaign Installs tab carries only installs + cost per campaign —
no per-campaign clicks/impressions. So CPI is computed PER CAMPAIGN, while CTR
and install-rate are only available at the Google Ads CHANNEL level (from
Channel Overview). This is stated to Claude and in the output.

Run from repo root:  python intelligence/google_ads_analysis.py
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

from pipelines import sheets

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"


def _num(x) -> float:
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


def _classify_themes(name: str) -> dict[str, str]:
    """Tag a Google Ads campaign name by keyword-theme dimensions."""
    n = name.lower()
    brand = "brand" if "brand" in n else "non-brand"
    if "android" in n:
        platform = "Android"
    elif "ios" in n or "iphone" in n or "apple" in n:
        platform = "iOS"
    else:
        platform = "unspecified"
    if "exact" in n:
        match = "exact"
    elif "broad" in n:
        match = "broad"
    elif "phrase" in n:
        match = "phrase"
    else:
        match = "other/offer" if not ("brand" in n) else "brand-default"
    return {"brand": brand, "platform": platform, "match": match}


def _theme_clusters(camps: list[dict]) -> str:
    """Aggregate installs/cost/CPI by keyword-theme cluster (only paid campaigns)."""
    from collections import defaultdict
    dims = {"brand": defaultdict(lambda: [0, 0.0]),
            "platform": defaultdict(lambda: [0, 0.0]),
            "match": defaultdict(lambda: [0, 0.0])}
    for c in camps:
        if not c["cost"]:  # exclude $0-cost (disconnected) from efficiency clusters
            continue
        t = _classify_themes(c["campaign"])
        for dim, key in t.items():
            dims[dim][key][0] += c["installs"]
            dims[dim][key][1] += c["cost"]

    out = ["GOOGLE ADS — KEYWORD-THEME CLUSTERS (paid campaigns only; installs, cost, CPI):"]
    labels = {"brand": "Brand vs non-brand", "platform": "Platform (OS)",
              "match": "Match type / theme"}
    for dim in ("brand", "platform", "match"):
        out.append(f"  {labels[dim]}:")
        for key, (inst, cost) in sorted(dims[dim].items(), key=lambda kv: kv[1][0], reverse=True):
            cpi = cost / inst if inst else 0.0
            out.append(f"    {key}: {inst:,} installs, ${cost:,.0f}, CPI ${cpi:.2f}")
    return "\n".join(out)


def build_data_summary(campaign_rows: list[dict], channel_rows: list[dict]) -> str:
    # Per-campaign Google Ads (installs, cost, CPI).
    camps = []
    for r in campaign_rows:
        if str(r.get("channel", "")).strip() != "Google Ads":
            continue
        name = str(r.get("campaign_network", "")).strip()
        if not name or name.lower() == "unknown":
            continue
        installs = int(_num(r.get("installs")))
        cost = _num(r.get("cost"))
        cpi = cost / installs if installs else None
        camps.append({"campaign": name, "installs": installs, "cost": cost, "cpi": cpi})
    camps.sort(key=lambda c: c["installs"], reverse=True)

    lines = ["GOOGLE ADS — PER-CAMPAIGN (Adjust Campaign Installs: installs, cost, CPI):"]
    for c in camps:
        cost_str = f"${c['cost']:,.2f}" if c["cost"] > 0 else "$0 (no cost reported — spend integration likely disconnected)"
        cpi_str = f"${c['cpi']:.2f}" if c["cpi"] is not None else "n/a"
        theme = _classify_themes(c["campaign"])
        lines.append(f"  {c['campaign']}: {c['installs']:,} installs, {cost_str}, CPI {cpi_str} "
                     f"[{theme['brand']}, {theme['platform']}, {theme['match']}]")

    lines.append("")
    lines.append(_theme_clusters(camps))

    # Channel-level Google Ads (impressions, clicks → CTR + install rate).
    lines.append("")
    lines.append("GOOGLE ADS — CHANNEL LEVEL (Channel Overview; CTR/install-rate only "
                 "available here, not per-campaign):")
    for r in channel_rows:
        ch = str(r.get("channel", "")).strip()
        if "google ads" not in ch.lower():
            continue
        installs = int(_num(r.get("installs")))
        impr = int(_num(r.get("impressions")))
        clicks = int(_num(r.get("clicks")))
        ecpi = _num(r.get("ecpi"))
        ctr = (clicks / impr * 100) if impr else 0.0
        irate = (installs / clicks * 100) if clicks else 0.0
        lines.append(
            f"  {ch}: {installs:,} installs, {impr:,} impressions, {clicks:,} clicks, "
            f"eCPI ${ecpi:.2f}, CTR {ctr:.2f}%, install-rate {irate:.2f}% (clicks->install)"
        )
    return "\n".join(lines)


_PROMPT = """\
You are a paid-search analyst for Speed Wallet, a Bitcoin Lightning payments app \
(US + EU paid markets). Below is REAL Google Ads performance from Adjust. Note: \
the Campaign Installs tab has NO per-campaign clicks/impressions, so CPI is the \
only true per-campaign efficiency metric; CTR and install-rate are channel-level \
only. Campaign NAMES encode the keyword/creative theme (e.g. "Brand", "XAUT \
offers"), so infer themes from names + performance. Some campaigns show real \
installs at $0 cost — treat those as a disconnected spend integration, not free \
installs, and flag them.

Write a structured analysis in clean PLAIN TEXT (no markdown, asterisks, or bold \
markers — use dash/equals section headers). Cover, in order:
1. PERFORMING CAMPAIGNS — which Google Ads campaigns are working (volume + CPI), \
with the numbers.
2. UNDERPERFORMING / FLAGGED — weak CPI, low volume, or $0-cost reporting issues, \
with a one-line reason each.
3. KEYWORD-THEME EFFICIENCY — using the KEYWORD-THEME CLUSTERS, say which theme \
clusters are most/least efficient: brand vs non-brand, Android vs iOS, and match \
type (exact/broad/offer). Cite the per-cluster CPIs and call out the single most \
efficient and least efficient cluster.
4. CREATIVE ANGLES FROM CAMPAIGN NAMES — what the names imply about messaging/\
creative (e.g. brand-defense, offer/incentive-led like "XAUT offers", \
platform-specific), and which angles to double down on or test next.
5. THREE RECOMMENDATIONS — specific, numbered, grounded in the numbers.
Cite real numbers throughout. Keep it under 650 words.

--- GOOGLE ADS DATA ---
{data_summary}
--- END DATA ---"""


def generate_analysis(data_summary: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": _PROMPT.format(data_summary=data_summary)}],
    )
    return resp.content[0].text


def save_analysis(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"google_ads_analysis_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")
    print("Opening Google Sheet (live)...")
    ss = sheets._open(sid)
    print("Reading tabs: Campaign Installs, Channel Overview...")
    campaign_rows = _records(ss, "Campaign Installs")
    channel_rows = _records(ss, "Channel Overview")

    summary = build_data_summary(campaign_rows, channel_rows)
    print(f"Generating Google Ads analysis ({_MODEL})...")
    analysis = generate_analysis(summary)

    bar = "=" * 70
    full = (f"{bar}\nSPEED WALLET — GOOGLE ADS ANALYSIS\n"
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{bar}\n\n"
            + analysis)
    path = save_analysis(full)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print(full)
    return full


if __name__ == "__main__":
    try:
        run()
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
