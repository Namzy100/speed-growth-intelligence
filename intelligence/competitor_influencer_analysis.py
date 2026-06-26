"""Synthesize competitor influencer / creative tactics from saved ad analyses.

Reads every competitor_analysis_*.json in data/processed/ (Robinhood, Crypto.com,
Kraken, Coinbase, Cash App, Strike across US/GB/DE/PT), aggregates each brand's
messaging analysis + dominant formats + sample ad copy, and asks Claude
(claude-sonnet-4-6) to synthesize: the influencer/creator tactics each competitor
uses, which messaging formats work, and what Speed can learn from or differentiate
against. Saves to docs/competitor_influencer_analysis_<date>.txt.

NOTE: the source data is ad COPY + format metadata (video/image/carousel,
days-running, CTAs) — not the visuals themselves. So whether ads use creator
faces / testimonials / UGC vs polished production is INFERRED from copy style and
format; Claude is told to flag where it is inferring.

Run from repo root:  python intelligence/competitor_influencer_analysis.py
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

_DATA_DIR = _ROOT / "data" / "processed"
_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"


def load_competitor_data() -> dict[str, dict]:
    """Aggregate saved competitor analyses by brand."""
    by_brand: dict[str, dict] = defaultdict(lambda: {
        "markets": set(), "dominant_formats": set(), "angles": set(),
        "ctas": set(), "framings": set(), "summaries": [], "ads": [],
    })
    for path in sorted(_DATA_DIR.glob("competitor_analysis_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        brand = str(data.get("competitor", "")).strip()
        if not brand:
            continue
        ma = data.get("messaging_analysis", {})
        if "parse_error" in ma:
            ma = {}
        b = by_brand[brand]
        b["markets"].add(str(data.get("country", "?")))
        if ma.get("dominant_format"):
            b["dominant_formats"].add(str(ma["dominant_format"]))
        for a in ma.get("messaging_angles", [])[:4]:
            b["angles"].add(str(a))
        for c in ma.get("top_ctas", [])[:3]:
            b["ctas"].add(str(c))
        if ma.get("buy_crypto_framing"):
            b["framings"].add(str(ma["buy_crypto_framing"]))
        if ma.get("summary"):
            b["summaries"].append(str(ma["summary"]))
        # Sample ad copy, longest-running first (proxy for what works).
        for ad in sorted(data.get("ads", []), key=lambda a: a.get("days_running", 0), reverse=True)[:4]:
            body = str(ad.get("body", "")).strip()
            if body:
                b["ads"].append((ad.get("format", "?"), ad.get("days_running", 0), body[:240]))
    return by_brand


def build_summary(by_brand: dict[str, dict]) -> str:
    blocks = []
    for brand, b in by_brand.items():
        ads_seen, ad_lines = set(), []
        for fmt, days, body in b["ads"]:
            if body in ads_seen:
                continue
            ads_seen.add(body)
            ad_lines.append(f"    [{fmt}, {days}d] {body}")
            if len(ad_lines) >= 5:
                break
        blocks.append(
            f"{brand} (markets: {', '.join(sorted(b['markets']))}):\n"
            f"  dominant_formats: {sorted(b['dominant_formats'])}\n"
            f"  buy_framings: {sorted(b['framings'])}\n"
            f"  messaging_angles: {sorted(b['angles'])}\n"
            f"  top_ctas: {sorted(b['ctas'])}\n"
            f"  sample longest-running ad copy:\n" + "\n".join(ad_lines)
        )
    return "\n\n".join(blocks)


_PROMPT = """\
You are a creative strategist for Speed Wallet, a Bitcoin Lightning payments app \
(segments: remittance/zero-fees, iGaming/instant-deposits, crypto-curious/\
simplicity). Below is aggregated Meta Ad Library analysis for several crypto/\
fintech competitors — their dominant ad formats, messaging angles, CTAs, buy \
framings, and sample longest-running ad copy.

IMPORTANT: you have ad COPY and format metadata, not the visuals. Infer creative \
approach (creator faces, testimonials, UGC-style vs polished/professional \
production) from copy tone, format, and CTAs — and explicitly flag where you are \
inferring rather than observing.

Write a structured synthesis in clean PLAIN TEXT (no markdown, asterisks, or bold \
markers — use dash/equals section headers). Cover, in order:
1. INFLUENCER / CREATOR TACTICS BY COMPETITOR — for each major brand, what \
creative approach they appear to use (faces/testimonials/UGC vs professional), \
grounded in their formats and copy.
2. MESSAGING FORMATS THAT WORK — short-form vs long-form, direct-response vs \
brand-building, and which formats dominate the longest-running ads.
3. WHAT SPEED SHOULD LEARN — tactics worth adopting.
4. WHERE SPEED SHOULD DIFFERENTIATE — uncontested angles/formats (tie to Speed's \
remittance / iGaming / crypto-curious segments).
Cite specific competitors and examples. Keep it under 600 words.

--- COMPETITOR AD DATA ---
{summary}
--- END DATA ---"""


def generate_analysis(summary: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": _PROMPT.format(summary=summary)}],
    )
    return resp.content[0].text


def save_analysis(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"competitor_influencer_analysis_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    print("Reading competitor analyses from data/processed/...")
    by_brand = load_competitor_data()
    if not by_brand:
        print("No competitor analysis JSONs found.")
        return ""
    print(f"  {len(by_brand)} brands: {', '.join(by_brand)}")

    summary = build_summary(by_brand)
    print(f"Synthesizing influencer/creative tactics ({_MODEL})...")
    analysis = generate_analysis(summary)

    bar = "=" * 70
    full = (f"{bar}\nSPEED WALLET — COMPETITOR INFLUENCER & CREATIVE SYNTHESIS\n"
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
