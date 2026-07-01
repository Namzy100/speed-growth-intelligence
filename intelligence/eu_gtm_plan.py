"""EU go-to-market plan for Speed Wallet — 5 markets, data-backed, actionable.

Reads real organic install data (Country Installs tab), the EU market-analysis
and channel-strategy memos, and the GB/DE/PT competitor JSONs, then uses Claude
to produce a per-market GTM plan: Month 1/2/3 actions, budget allocation,
expected CPI range (US benchmark adjusted), KPI targets, and corridor-specific
remittance messaging.

Output: docs/eu_gtm_plan_YYYY_MM_DD.txt

Run from repo root:  python intelligence/eu_gtm_plan.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from pipelines import sheets

_DOCS = _ROOT / "docs"
_PROCESSED = _ROOT / "data" / "processed"
_MODEL = "claude-sonnet-4-6"
_US_BENCHMARK_CPI = 3.17  # best Meta CPI (Payday Android Broad+)

# 5 target markets: install-tab country key, diaspora corridor, competitor
# country suffix for the analysis JSONs, and known regulatory frame.
_MARKETS = [
    {"name": "Germany", "country_keys": ["Germany"], "corridor": "Indian diaspora → India (plus Nigerian)",
     "comp_suffix": "de", "regulatory": "Inside EU; served by a single EMI license. iGaming legal since 2021 (GlüNeuRStV) with deposit limits."},
    {"name": "United Kingdom", "country_keys": ["United Kingdom"], "corridor": "Nigerian & Indian diaspora → Nigeria / India",
     "comp_suffix": "gb", "regulatory": "Non-EU (post-Brexit) — separate regulatory track from EMI. iGaming fully legal under UKGC."},
    {"name": "Portugal", "country_keys": ["Portugal"], "corridor": "Brazilian diaspora → Brazil",
     "comp_suffix": "pt", "regulatory": "Inside EU (EMI). iGaming regulated under SRIJ; smaller market."},
    {"name": "Spain", "country_keys": ["Spain"], "corridor": "Latin American diaspora (Colombian/Venezuelan/Ecuadorian) → LatAm",
     "comp_suffix": None, "regulatory": "Inside EU (EMI). iGaming regulated under DGOJ."},
    {"name": "Netherlands", "country_keys": ["Netherlands"], "corridor": "Surinamese & Turkish diaspora → Suriname / Türkiye",
     "comp_suffix": None, "regulatory": "Inside EU (EMI). iGaming regulated since Oct 2021 under the KSA (early-mover window)."},
]
_COMP_BRANDS = ["robinhood", "crypto.com", "kraken"]


# ------------------------------------------------------------------
# Data readers
# ------------------------------------------------------------------

def _country_installs(spreadsheet_id: str) -> dict[str, int]:
    ss = sheets._open(spreadsheet_id)
    rows = sheets._retry(lambda: ss.worksheet("Country Installs")).get_all_records()
    out = {}
    for r in rows:
        c = str(r.get("country", "")).strip()
        try:
            out[c] = int(str(r.get("installs", 0)).replace(",", "") or 0)
        except ValueError:
            out[c] = 0
    return out


def _read_latest(pattern: str) -> str:
    files = sorted(_DOCS.glob(pattern))
    return files[-1].read_text(encoding="utf-8") if files else ""


def _competitor_digest(suffix: str | None) -> str:
    """Compact competitor messaging summary for a market (by country suffix)."""
    if not suffix:
        return "(no market-specific competitor scan on file)"
    blocks = []
    for brand in _COMP_BRANDS:
        path = _PROCESSED / f"competitor_analysis_{brand}_{suffix}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ma = data.get("messaging_analysis", {}) or {}
        blocks.append(
            f"  {brand} ({data.get('total_ads', '?')} ads): "
            f"{(ma.get('summary', '') or '')[:240]}"
        )
    return "\n".join(blocks) or "(no competitor files found for this market)"


# ------------------------------------------------------------------
# Claude per-market plan
# ------------------------------------------------------------------

def _market_plan(client: Anthropic, market: dict, installs: int,
                 eu_memo: str, channel_memo: str) -> str:
    comp = _competitor_digest(market["comp_suffix"])
    prompt = (
        "You are Speed Wallet's EU growth lead. Speed is a Bitcoin Lightning payments "
        "app; its sharpest hook is ZERO-FEE, instant cross-border sends for diaspora "
        "remittance. Write a tight, EXECUTABLE go-to-market section for ONE market.\n\n"
        f"MARKET: {market['name']}\n"
        f"Real organic installs to date (Adjust, unprompted): {installs:,}\n"
        f"Primary diaspora corridor: {market['corridor']}\n"
        f"Regulatory frame: {market['regulatory']}\n"
        f"Competitor presence (from ad scans):\n{comp}\n\n"
        f"US paid benchmark CPI to anchor estimates: ${_US_BENCHMARK_CPI:.2f}.\n\n"
        "Reference context (EU memos):\n"
        f"{eu_memo[:1400]}\n---\n{channel_memo[:1400]}\n\n"
        "Produce EXACTLY these labeled parts, concrete and numeric, no markdown headers:\n"
        "1. SNAPSHOT — installs, corridor, competitor gap, regulatory note (2-3 lines).\n"
        "2. CORRIDOR MESSAGING — the specific zero-fee send message for this corridor, "
        "in the corridor's framing (name the countries; give one literal ad line).\n"
        "3. RECOMMENDED CHANNEL MIX — ranked channels with a one-line why each.\n"
        "4. MONTH 1 — 3-4 specific, executable actions.\n"
        "5. MONTH 2 — 3-4 specific actions that build on Month 1.\n"
        "6. MONTH 3 — 3-4 specific actions (scale / layer iGaming where legal).\n"
        "7. BUDGET ALLOCATION — a recommended monthly split (channels + %/$ ranges).\n"
        f"8. EXPECTED CPI RANGE — adjust the US ${_US_BENCHMARK_CPI:.2f} benchmark for this market; give a "
        "range and a one-line rationale.\n"
        "9. KPI TARGETS — concrete 90-day targets (installs, CPI, first-send rate).\n"
    )
    resp = client.messages.create(
        model=_MODEL, max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run() -> Path:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    print("Reading Country Installs...")
    installs = _country_installs(spreadsheet_id)
    eu_memo = _read_latest("eu_market_analysis_*.txt")
    channel_memo = _read_latest("eu_channel_strategy_*.txt")
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Rank markets by real installs for the summary table.
    ranked = sorted(
        _MARKETS,
        key=lambda m: sum(installs.get(k, 0) for k in m["country_keys"]),
        reverse=True,
    )

    sections = []
    for i, m in enumerate(ranked, 1):
        inst = sum(installs.get(k, 0) for k in m["country_keys"])
        print(f"[{i}/{len(ranked)}] Planning {m['name']} ({inst:,} installs)...")
        plan = _market_plan(client, m, inst, eu_memo, channel_memo)
        sections.append(
            f"{'=' * 70}\nMARKET #{i}: {m['name'].upper()}  ·  {inst:,} organic installs\n{'=' * 70}\n{plan}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    summary_rows = "\n".join(
        f"  #{i}  {m['name']:<16} {sum(installs.get(k,0) for k in m['country_keys']):>7,} installs   "
        f"corridor: {m['corridor']}"
        for i, m in enumerate(ranked, 1)
    )
    header = (
        "=" * 70 + "\nSPEED WALLET — EU GO-TO-MARKET PLAN (5 MARKETS)\n"
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
        f"install data live from Adjust · US benchmark CPI ${_US_BENCHMARK_CPI:.2f}\n"
        + "=" * 70 + "\n\n"
        "MARKET PRIORITY (by real organic install demand)\n" + "-" * 32 + "\n"
        + summary_rows + "\n\n\n"
    )
    out = _DOCS / f"eu_gtm_plan_{stamp}.txt"
    out.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"\nSaved: {out.relative_to(_ROOT)}")
    return out


if __name__ == "__main__":
    try:
        path = run()
        print("=" * 60)
        print(path.read_text(encoding="utf-8")[:2200])
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
