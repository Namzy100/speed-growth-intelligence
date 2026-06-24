"""Creative scoring model for Speed's own active paid ad campaigns.

Reads the Meta Campaigns tab (spend, impressions, clicks, mobile_app_install)
and the Adjust Campaign Installs tab (channel/campaign installs + cost), computes
per-campaign efficiency metrics (CTR, CPI, install rate) and a 0-100 composite
"creative score", then uses Claude (claude-sonnet-4-6) for a structured creative
performance analysis. Saves to docs/creative_scoring_<date>.txt.

score_campaigns() is a PURE function (no network) so the creative dashboard can
reuse it to show a Creative Score column without making a Claude call.

Run from repo root:  python intelligence/creative_scoring.py
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

# Composite weights — CPI (cost efficiency) matters most for paid creative, then
# install rate (click->install quality), then CTR (creative hook strength).
_WEIGHTS = {"cpi": 0.45, "install_rate": 0.30, "ctr": 0.25}


# ------------------------------------------------------------------
# Parsing + scoring (pure, no network)
# ------------------------------------------------------------------

def _num(x) -> float:
    if x is None:
        return 0.0
    s = str(x).replace("$", "").replace(",", "").replace("USD", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def _normalize(values: list, higher_better: bool) -> list[float]:
    """Min-max scale to 0-100. None values -> 0 (worst). Flat metric -> 50."""
    valid = [v for v in values if v is not None]
    if not valid:
        return [0.0 for _ in values]
    lo, hi = min(valid), max(valid)
    out = []
    for v in values:
        if v is None:
            out.append(0.0)
        elif hi == lo:
            out.append(50.0)
        else:
            frac = (v - lo) / (hi - lo)
            out.append(round(100 * (frac if higher_better else 1 - frac), 1))
    return out


def score_campaigns(meta_rows: list[dict]) -> list[dict]:
    """Score Meta campaigns on CTR, CPI, and install rate into a composite 0-100.

    Pure function — takes the raw Meta Campaigns sheet rows and returns one dict
    per campaign with raw metrics, per-metric normalized scores, and the
    composite `creative_score`. Sorted by creative_score descending.
    """
    camps = []
    for r in meta_rows:
        name = str(r.get("campaign_name", "")).strip()
        if not name:
            continue
        spend = _num(r.get("spend"))
        impressions = int(_num(r.get("impressions")))
        clicks = int(_num(r.get("clicks")))
        installs = int(_num(r.get("mobile_app_install")))

        ctr = (clicks / impressions * 100) if impressions else 0.0
        install_rate = (installs / clicks * 100) if clicks else 0.0
        # CPI is None when there are no installs so it scores worst, not "free".
        cpi = (spend / installs) if installs else None

        camps.append({
            "campaign": name, "spend": round(spend, 2), "impressions": impressions,
            "clicks": clicks, "installs": installs,
            "ctr": round(ctr, 3), "cpi": (round(cpi, 2) if cpi is not None else None),
            "install_rate": round(install_rate, 2),
        })

    if not camps:
        return []

    ctr_scores = _normalize([c["ctr"] for c in camps], higher_better=True)
    cpi_scores = _normalize([c["cpi"] for c in camps], higher_better=False)
    ir_scores = _normalize([c["install_rate"] for c in camps], higher_better=True)

    for c, cs, ps, irs in zip(camps, ctr_scores, cpi_scores, ir_scores):
        c["ctr_score"] = cs
        c["cpi_score"] = ps
        c["install_rate_score"] = irs
        c["creative_score"] = round(
            _WEIGHTS["ctr"] * cs + _WEIGHTS["cpi"] * ps + _WEIGHTS["install_rate"] * irs, 1
        )

    camps.sort(key=lambda c: c["creative_score"], reverse=True)
    return camps


def creative_scores_by_name(meta_rows: list[dict]) -> dict[str, float]:
    """{campaign_name: creative_score} — convenience for the dashboard."""
    return {c["campaign"]: c["creative_score"] for c in score_campaigns(meta_rows)}


# ------------------------------------------------------------------
# Sheet reads
# ------------------------------------------------------------------

def _records(ss, tab: str) -> list[dict]:
    ws = sheets._retry(lambda: ss.worksheet(tab))
    return sheets._retry(ws.get_all_records)


def _adjust_campaign_context(adjust_rows: list[dict]) -> str:
    """Top campaigns by installs across channels (Adjust), with eCPI, for context."""
    camps = []
    for r in adjust_rows:
        name = str(r.get("campaign_network", "")).strip()
        if not name or name.lower() == "unknown":
            continue
        installs = int(_num(r.get("installs")))
        cost = _num(r.get("cost"))
        if installs == 0 and cost == 0:
            continue
        ecpi = cost / installs if installs else 0.0
        camps.append((str(r.get("channel", "")).strip(), name, installs, cost, ecpi))
    camps.sort(key=lambda c: c[2], reverse=True)

    lines = ["ADJUST CROSS-CHANNEL CONTEXT — top campaigns by installs (channel / campaign: installs, cost, eCPI):"]
    for channel, name, installs, cost, ecpi in camps[:10]:
        cost_str = f"${cost:,.2f}" if cost > 0 else "$0"
        ecpi_str = f"${ecpi:.2f}" if ecpi > 0 else "n/a"
        lines.append(f"  {channel} / {name}: {installs:,} installs, {cost_str}, eCPI {ecpi_str}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Format + Claude analysis
# ------------------------------------------------------------------

def format_scores_table(scored: list[dict]) -> str:
    lines = [
        "CREATIVE SCORES — Speed Meta campaigns (composite 0-100; "
        "weights CPI 45% / install-rate 30% / CTR 25%)",
        "-" * 92,
        f"{'SCORE':>6}  {'CTR':>6}  {'CPI':>9}  {'INST.RATE':>9}  {'SPEND':>10}  {'INSTALLS':>8}  CAMPAIGN",
    ]
    for c in scored:
        cpi = f"${c['cpi']:.2f}" if c["cpi"] is not None else "n/a"
        lines.append(
            f"{c['creative_score']:>6}  {c['ctr']:>5.2f}%  {cpi:>9}  "
            f"{c['install_rate']:>8.2f}%  ${c['spend']:>9,.0f}  {c['installs']:>8,}  {c['campaign']}"
        )
    return "\n".join(lines)


def generate_analysis(scored: list[dict], adjust_context: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    prompt = (
        "You are a paid-creative analyst for Speed Wallet, a Bitcoin Lightning "
        "payments app (US + EU paid markets; segments: remittance/zero-fees, "
        "iGaming/instant-deposits, crypto-curious/simplicity).\n\n"
        "Below are Speed's own active Meta ad campaigns, scored on a 0-100 "
        "composite creative score (CPI 45%, install-rate 30%, CTR 25%; each "
        "metric min-max normalized across these campaigns), plus cross-channel "
        "Adjust context.\n\n"
        f"{format_scores_table(scored)}\n\n"
        f"{adjust_context}\n\n"
        "Write a structured creative performance analysis in clean PLAIN TEXT. "
        "Do not use markdown, asterisks, or bold markers — use section headers "
        "made of dashes or equals signs. Cover, in order:\n"
        "1. TOP PERFORMERS — which campaigns score highest and why (cite their "
        "CTR / CPI / install-rate).\n"
        "2. UNDERPERFORMERS — which score lowest and the specific metric dragging "
        "them down.\n"
        "3. CREATIVE & TARGETING PATTERNS — what the metric spread suggests about "
        "which creative angles, formats, or audiences are working vs not.\n"
        "4. THREE RECOMMENDATIONS — specific, numbered actions grounded in the data.\n"
        "Cite real numbers throughout. Keep it under 500 words."
    )

    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Save + entrypoint
# ------------------------------------------------------------------

def save_output(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"creative_scoring_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    print("Opening Google Sheet (live)...")
    ss = sheets._open(sid)
    print("Reading tabs: Meta Campaigns, Campaign Installs...")
    meta_rows = _records(ss, "Meta Campaigns")
    adjust_rows = _records(ss, "Campaign Installs")

    scored = score_campaigns(meta_rows)
    if not scored:
        print("No Meta campaigns to score.")
        return ""
    table = format_scores_table(scored)
    adjust_context = _adjust_campaign_context(adjust_rows)

    print(f"Scoring {len(scored)} campaigns; generating analysis ({_MODEL})...")
    analysis = generate_analysis(scored, adjust_context)

    bar = "=" * 70
    header = (
        f"{bar}\n"
        "SPEED WALLET — CREATIVE PERFORMANCE SCORING\n"
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"{bar}\n\n"
    )
    full = header + table + "\n\n" + analysis
    path = save_output(full)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print(full)
    return full


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
