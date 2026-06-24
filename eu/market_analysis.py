"""Research-driven EU market prioritization for Speed Wallet.

Synthesizes which European markets Speed should enter first, by combining:
  - Current US traction scale from the Adjust Channel Overview tab (real data).
  - Top diaspora SOURCE countries Speed already serves (Nigeria, Brazil, India,
    Mexico) and their mapping to candidate EU markets via established diaspora
    ties (the research scaffold below).
  - iGaming regulatory status per candidate market (Speed's iGaming segment).
Claude (claude-sonnet-4-6) turns these inputs into a structured prioritization
memo recommending the top 3 EU markets to enter first.

IMPORTANT DATA NOTE: the Adjust Channel Overview tab is broken down by
acquisition CHANNEL (Organic, Google Ads, Apple, …), NOT by country. The
pipeline has no per-country install data, so the diaspora->market mapping relies
on the external research scaffold below (approximate, knowledge-based estimates),
not on observed per-country installs. Verify figures before acting on them.

Output: docs/eu_market_analysis_<date>.txt
Run from repo root:  python eu/market_analysis.py
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

# Diaspora SOURCE countries Speed already serves (remittance origin markets).
SOURCE_COUNTRIES = ["Nigeria", "Brazil", "India", "Mexico"]

# Candidate EU markets to evaluate for first entry.
TARGET_MARKETS = ["United Kingdom", "Germany", "Spain", "Portugal", "Netherlands"]

# Research scaffold — established diaspora ties + iGaming regulatory status per
# candidate market. Population figures are APPROXIMATE, knowledge-based estimates
# (not from a live dataset) and are passed to Claude as analytical context, not
# ground truth. They should be verified against current sources before any
# go-to-market decision.
MARKET_RESEARCH = {
    "United Kingdom": {
        "diaspora": (
            "Largest Indian-origin population in Europe (~1.5M+); large Nigerian "
            "community (~200k+ Nigeria-born, far more by heritage); some Brazilian. "
            "Commonwealth + English-language ties make UK->India and UK->Nigeria "
            "two of Europe's biggest remittance corridors."
        ),
        "igaming": (
            "Fully legal and regulated by the UK Gambling Commission (UKGC); "
            "large, mature, competitive online gambling market."
        ),
    },
    "Germany": {
        "diaspora": (
            "Largest EU economy and population; fast-growing Indian skilled-migrant "
            "community; smaller Nigerian and Brazilian populations. Strong overall "
            "remittance outflows but corridors less concentrated on Speed's sources."
        ),
        "igaming": (
            "Legal but restrictive since the 2021 Interstate Treaty (GlueNeuRStV): "
            "online slots/poker permitted with strict deposit limits and a national "
            "regulator (GGL)."
        ),
    },
    "Spain": {
        "diaspora": (
            "Large Latin American diaspora, but the Mexican community is modest "
            "(Colombian/Venezuelan/Ecuadorian/Peruvian dominate); Brazilian present. "
            "Spanish-language advantage for LatAm corridors generally."
        ),
        "igaming": (
            "Legal and regulated by the DGOJ; established licensed online gambling "
            "market with advertising restrictions."
        ),
    },
    "Portugal": {
        "diaspora": (
            "Brazilian nationals are the LARGEST foreign community (~400k+) with a "
            "shared language and deep cultural ties — making Portugal->Brazil one of "
            "the strongest, most concentrated remittance corridors for Speed's sources."
        ),
        "igaming": (
            "Legal and regulated by the SRIJ; growing licensed online gambling market."
        ),
    },
    "Netherlands": {
        "diaspora": (
            "Smaller populations from Speed's specific source countries; some Indian "
            "and Nigerian presence. High digital/fintech adoption but less corridor "
            "concentration."
        ),
        "igaming": (
            "Legal since the KOA Act took effect in October 2021; regulated by the "
            "Kansspelautoriteit (KSA) — a relatively new, opening market."
        ),
    },
}


# ------------------------------------------------------------------
# Real-data context (Adjust Channel Overview — channel-level, not per-country)
# ------------------------------------------------------------------

def _num(x) -> float:
    if x is None:
        return 0.0
    s = str(x).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def read_install_context() -> str:
    """Summarize current US install scale from Channel Overview (channel-level)."""
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")
    ss = sheets._open(sid)
    ws = sheets._retry(lambda: ss.worksheet("Channel Overview"))
    rows = sheets._retry(ws.get_all_records)

    channels = []
    for r in rows:
        name = str(r.get("channel", "")).strip()
        installs = int(_num(r.get("installs")))
        if name and installs > 0:
            channels.append((name, installs))
    channels.sort(key=lambda c: c[1], reverse=True)
    total = sum(c[1] for c in channels)

    lines = [
        f"Current US install scale (Adjust, last 30 days): {total:,} total installs "
        f"across {len(channels)} active channels.",
        "NOTE: this tab is broken down by acquisition CHANNEL, not by country — "
        "the pipeline has no per-country install data, so per-country demand below "
        "is inferred from diaspora research, not observed installs.",
        "Top channels by installs:",
    ]
    for name, installs in channels[:6]:
        lines.append(f"  - {name}: {installs:,}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Claude memo
# ------------------------------------------------------------------

def build_prompt(install_context: str) -> str:
    research_block = "\n\n".join(
        f"{market}:\n  Diaspora: {MARKET_RESEARCH[market]['diaspora']}\n"
        f"  iGaming: {MARKET_RESEARCH[market]['igaming']}"
        for market in TARGET_MARKETS
    )
    return (
        "You are a market strategist for Speed Wallet, a Bitcoin Lightning "
        "payments app. Speed's segments and hooks: remittance senders (zero "
        "fees on cross-border sends), iGaming users (instant deposits/"
        "withdrawals), and crypto-curious mainstream users (simplicity/utility). "
        "Speed already operates in the US; this analysis is about which EUROPEAN "
        "market to enter first.\n\n"
        f"Top diaspora SOURCE countries Speed serves: {', '.join(SOURCE_COUNTRIES)}.\n"
        f"Candidate EU markets: {', '.join(TARGET_MARKETS)}.\n\n"
        "CURRENT US TRACTION (real, from the dashboard):\n"
        f"{install_context}\n\n"
        "RESEARCH SCAFFOLD (approximate, knowledge-based — treat diaspora figures "
        "as estimates to be verified, not exact counts):\n"
        f"{research_block}\n\n"
        "Write a structured EU market prioritization memo in clean PLAIN TEXT. "
        "Do not use markdown formatting, asterisks, or bold markers — use clear "
        "section headers made of dashes or equals signs. Cover, in order:\n"
        "1. A 2-3 sentence executive summary.\n"
        "2. Per-market assessment for all five candidates: diaspora remittance "
        "demand (which source-country corridors it unlocks), iGaming regulatory "
        "fit for Speed's iGaming segment, and any operational note.\n"
        "3. TOP 3 EU MARKETS TO ENTER FIRST — ranked 1-3, each with a concrete "
        "rationale tying the market to Speed's specific segments and source "
        "corridors, and the single strongest reason it beats the markets below it.\n"
        "4. A suggested first move per recommended market (the wedge segment + "
        "angle to lead with).\n"
        "Be decisive and specific. Note explicitly that diaspora figures are "
        "estimates. Keep it under 650 words."
    )


def generate_analysis(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Save + entrypoint
# ------------------------------------------------------------------

def save_analysis(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"eu_market_analysis_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    print("Reading current US install context (Adjust Channel Overview)...")
    install_context = read_install_context()
    print("  (channel-level only — no per-country data in the pipeline)")

    print(f"Generating EU market prioritization memo ({_MODEL})...")
    memo = generate_analysis(build_prompt(install_context))

    path = save_analysis(memo)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print("=" * 70)
    print(memo)
    print("=" * 70)
    return memo


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
