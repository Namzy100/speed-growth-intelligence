"""Per-market acquisition channel playbook for Speed's top 3 EU markets.

For each of the top 3 EU markets from the EU market analysis (Germany, United
Kingdom, Portugal), Claude (claude-sonnet-4-6) recommends:
  - which acquisition channels to prioritize (Telegram, WhatsApp diaspora
    networks, creator content, paid search, paid social),
  - the best-fit messaging angle (fee argument for remittance corridors,
    iGaming utility, or general crypto framing),
  - which creator segments to target first (remittance / iGaming / crypto-curious).

Grounded in real per-country install data (Adjust "Country Installs" tab) and the
GB/DE/PT competitor ad analyses in data/processed/. Saves to
docs/eu_channel_strategy_<date>.txt.

Run from repo root:  python eu/channel_strategy.py
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

load_dotenv()

from pipelines import sheets  # reuse service-account auth + retry

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"

# Top 3 EU markets (from eu/market_analysis). competitor_cc maps to the GB/DE
# competitor scans; corridors note the dominant diaspora source ties (research).
TOP_MARKETS = [
    {"name": "Germany", "country": "Germany", "competitor_cc": "de",
     "corridors": "Indian (skilled migrants); smaller Nigerian and Brazilian"},
    {"name": "United Kingdom", "country": "United Kingdom", "competitor_cc": "gb",
     "corridors": "Indian and Nigerian (Commonwealth, English-language)"},
    {"name": "Portugal", "country": "Portugal", "competitor_cc": "pt",
     "corridors": "Brazilian (shared language, largest foreign community)"},
]
SOURCE_COUNTRIES = ["Nigeria", "Brazil", "India", "Mexico"]

# The menus the playbook must choose from (per the strategy brief).
CHANNELS = ("Telegram, WhatsApp diaspora networks, creator content, paid search, "
            "paid social")
ANGLES = ("fee argument (for remittance corridors), iGaming utility, "
          "general crypto framing")
CREATOR_SEGMENTS = "remittance, iGaming, crypto-curious"


def _num(x) -> float:
    if x is None:
        return 0.0
    s = str(x).replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


# ------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------

def read_country_installs() -> dict[str, int]:
    """{country_name_lower: installs} from the Adjust 'Country Installs' tab."""
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")
    ss = sheets._open(sid)
    ws = sheets._retry(lambda: ss.worksheet("Country Installs"))
    rows = sheets._retry(ws.get_all_records)
    return {str(r.get("country", "")).strip().lower(): int(_num(r.get("installs")))
            for r in rows if str(r.get("country", "")).strip()}


def read_competitor_context() -> dict[str, str]:
    """{cc: text block} of GB/DE competitor ad positioning from data/processed/."""
    data_dir = _ROOT / "data" / "processed"
    out: dict[str, str] = {}
    for cc in ("gb", "de", "pt"):
        blocks = []
        for path in sorted(data_dir.glob(f"competitor_analysis_*_{cc}.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            ma = data.get("messaging_analysis", {})
            if not ma or "parse_error" in ma:
                continue
            angles = ma.get("messaging_angles", [])[:3]
            blocks.append(
                f"  {data.get('competitor', '?')}: angles={angles}; "
                f"summary={ma.get('summary', '')}"
            )
        out[cc] = "\n".join(blocks) if blocks else "(no competitor scan available)"
    return out


# ------------------------------------------------------------------
# Prompt + Claude
# ------------------------------------------------------------------

def build_prompt(installs: dict[str, int], competitor: dict[str, str]) -> str:
    src_lines = "\n".join(
        f"  - {c}: {installs.get(c.lower(), 0):,} installs" for c in SOURCE_COUNTRIES
    )
    market_lines = []
    for m in TOP_MARKETS:
        comp = competitor.get(m["competitor_cc"], "(no competitor scan available)") \
            if m["competitor_cc"] else "(no competitor scan available for this market)"
        market_lines.append(
            f"{m['name']} — {installs.get(m['country'].lower(), 0):,} current installs.\n"
            f"  Dominant diaspora corridors: {m['corridors']}.\n"
            f"  Competitor ad positioning in this market:\n{comp}"
        )
    markets_block = "\n\n".join(market_lines)

    return (
        "You are a growth strategist for Speed Wallet, a Bitcoin Lightning "
        "payments app. Segments and hooks: remittance (zero fees on cross-border "
        "sends), iGaming (instant deposits/withdrawals), crypto-curious "
        "(simplicity/utility). Speed operates in the US and is now planning its "
        "first European market entries.\n\n"
        "Build a per-market acquisition CHANNEL STRATEGY PLAYBOOK for the three "
        "top EU markets below. For EACH market, choose from these menus:\n"
        f"  - Acquisition channels: {CHANNELS}\n"
        f"  - Messaging angles: {ANGLES}\n"
        f"  - Creator segments: {CREATOR_SEGMENTS}\n\n"
        "Speed's current installs in its diaspora SOURCE countries (real data — "
        "shows which corridors already have an engaged base to amplify):\n"
        f"{src_lines}\n\n"
        "TOP 3 EU MARKETS (real installs + corridors + competitor positioning):\n\n"
        f"{markets_block}\n\n"
        "Write the playbook in clean PLAIN TEXT. Do not use markdown, asterisks, "
        "or bold markers — use section headers made of dashes or equals signs. "
        "For EACH market, in this order, produce:\n"
        "1. PRIORITIZE CHANNELS — rank the channels for this market; emphasize the "
        "top 2-3 and say why (tie to the corridor and what competitors are/aren't "
        "doing). \n"
        "2. MESSAGING ANGLE — pick the single best-fit angle and justify it from "
        "the dominant corridor and competitor white space.\n"
        "3. CREATOR SEGMENTS TO TARGET FIRST — which creator segment(s) to recruit "
        "first and why.\n"
        "Where a market has no competitor scan, say so and reason from the "
        "corridor and install data instead. Cite the real install numbers. "
        "Be decisive and specific. Keep it under 750 words."
    )


def generate_playbook(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Save + entrypoint
# ------------------------------------------------------------------

def save_playbook(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"eu_channel_strategy_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    print("Reading Country Installs + GB/DE competitor analyses...")
    installs = read_country_installs()
    competitor = read_competitor_context()

    print(f"Generating EU channel strategy playbook ({_MODEL})...")
    playbook = generate_playbook(build_prompt(installs, competitor))

    bar = "=" * 70
    header = (f"{bar}\nSPEED WALLET — EU CHANNEL STRATEGY PLAYBOOK\n"
              f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{bar}\n\n")
    full = header + playbook
    path = save_playbook(full)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print(full)
    return full


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
