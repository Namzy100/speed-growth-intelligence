"""Competitor ad analysis for crypto wallet apps using Meta Ad Library + Claude."""

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anthropic import Anthropic
from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

_DATA_DIR = _ROOT / "data" / "processed"

# apify/facebook-ads-scraper scrapes the public Meta Ad Library — no auth required.
_AD_LIBRARY_ACTOR = "apify/facebook-ads-scraper"
_AD_LIBRARY_BASE = "https://www.facebook.com/ads/library/"

# How many ads to pull per competitor. The actor fetches in batches of 10;
# 50 gives a solid sample while keeping run time under ~90s.
_MAX_ADS = 50
_RUN_TIMEOUT = timedelta(seconds=120)


# ------------------------------------------------------------------
# Fetch from Meta Ad Library via Apify
# ------------------------------------------------------------------

def fetch_competitor_ads(competitor_name: str, platform: str = "all") -> list[dict]:
    """Fetch active ads for a competitor from Meta Ad Library.

    Args:
        competitor_name: Brand name to search (e.g. "Coinbase", "Cash App").
        platform:        "all", "facebook", or "instagram".

    Returns:
        List of normalised ad dicts with keys:
        body, title, cta, format, days_running, is_active, platforms, page_name.

    Raises:
        EnvironmentError: If APIFY_API_KEY is missing.
        RuntimeError:     If the actor run fails (not a timeout — timeouts are expected).
    """
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise EnvironmentError("APIFY_API_KEY must be set in .env")

    url = _build_ad_library_url(competitor_name, platform)
    client = ApifyClient(api_key)

    run = client.actor(_AD_LIBRARY_ACTOR).call(
        run_input={"startUrls": [{"url": url}]},
        max_items=_MAX_ADS,
        run_timeout=_RUN_TIMEOUT,
        memory_mbytes=512,
    )

    if run is None:
        raise RuntimeError(f"Apify actor run returned None for '{competitor_name}'.")

    # TIMED-OUT is expected — the actor paginates indefinitely; we stop it early.
    if run.status not in ("SUCCEEDED", "TIMED-OUT"):
        raise RuntimeError(f"Apify actor ended with status: {run.status}")

    raw_items = list(client.dataset(run.default_dataset_id).iterate_items())

    ads = []
    for item in raw_items:
        parsed = _parse_ad_item(item)
        if parsed:
            ads.append(parsed)

    return ads


def _build_ad_library_url(competitor_name: str, platform: str) -> str:
    params: dict[str, str] = {
        "active_status": "all",
        "ad_type": "all",
        "country": "US",
        "q": competitor_name,
        "search_type": "keyword_unordered",
    }
    if platform in ("facebook", "instagram", "messenger"):
        params["publisher_platforms[]"] = platform
    return _AD_LIBRARY_BASE + "?" + urllib.parse.urlencode(params)


def _parse_ad_item(item: dict) -> dict | None:
    """Normalise a raw Apify item into a clean ad dict. Returns None if no body copy."""
    snap = item.get("snapshot", {}) or {}

    # Ad copy — body can be a dict or a plain string depending on the ad type
    body = snap.get("body", "")
    body_text = body.get("text", "") if isinstance(body, dict) else str(body or "")

    # Some ads put copy in cards or extra text blocks
    for card in snap.get("cards", []):
        card_body = card.get("body", "")
        card_text = card_body.get("text", "") if isinstance(card_body, dict) else str(card_body or "")
        if card_text:
            body_text = (body_text + " " + card_text).strip()

    body_text = body_text.strip()
    if not body_text:
        return None

    # Creative format
    if snap.get("cards"):
        fmt = "carousel"
    elif snap.get("videos"):
        fmt = "video"
    else:
        fmt = (snap.get("displayFormat") or "image").lower()

    # Days running
    days_running = _calc_days_running(item)

    return {
        "body": body_text,
        "title": (snap.get("title") or "").strip(),
        "cta": (snap.get("ctaText") or snap.get("ctaType") or "").strip(),
        "format": fmt,
        "days_running": days_running,
        "is_active": bool(item.get("isActive")),
        "platforms": item.get("publisherPlatform") or [],
        "page_name": (snap.get("pageName") or item.get("pageName") or "").strip(),
    }


def _calc_days_running(item: dict) -> int:
    """Return how many days the ad has been running. 0 if date info is missing."""
    start = item.get("startDate")
    end = item.get("endDate")
    is_active = item.get("isActive", False)

    if start:
        # startDate can be seconds or milliseconds since epoch
        if start > 1e10:
            start = start / 1000
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
        if is_active or not end:
            end_dt = datetime.now(timezone.utc)
        else:
            if end > 1e10:
                end = end / 1000
            end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
        return max(0, (end_dt - start_dt).days)

    # Fallback: parse formatted date string
    start_str = item.get("startDateFormatted", "")
    if start_str:
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - start_dt).days)
        except ValueError:
            pass
    return 0


# ------------------------------------------------------------------
# Claude analysis
# ------------------------------------------------------------------

def analyze_messaging_themes(ads_list: list[dict]) -> dict:
    """Identify messaging patterns across a list of ads using Claude.

    Args:
        ads_list: List of ad dicts as returned by fetch_competitor_ads(),
                  or manually constructed with at minimum a "body" key.

    Returns:
        Dict with keys: messaging_angles, top_ctas, buy_crypto_framing,
        fees_messaging, simplicity_messaging, security_messaging,
        dominant_format, longest_running_themes, summary.
    """
    if not ads_list:
        return {"error": "No ads provided"}

    ads_block = _format_ads_for_prompt(ads_list)

    prompt = f"""\
You are analyzing competitor ads for a Bitcoin Lightning wallet app called Speed.
Review the following ads and return a JSON object with exactly these keys:

{{
  "messaging_angles": ["list of the most common themes or angles found across ads"],
  "top_ctas": ["CTAs ranked by frequency, e.g. 'Get started', 'Download', 'Learn more'"],
  "buy_crypto_framing": "direct | soft | mixed — describe how directly they push buying crypto",
  "fees_messaging": "how fees are positioned (or avoided) in these ads",
  "simplicity_messaging": "how ease-of-use is (or isn't) highlighted",
  "security_messaging": "how trust or security is (or isn't) emphasized",
  "dominant_format": "video | image | carousel | mixed",
  "longest_running_themes": ["themes seen in ads running 30+ days — proxy for what is working"],
  "summary": "2-3 sentence plain-English summary of this competitor's overall ad strategy"
}}

Return only valid JSON. No explanation outside the JSON block.

--- ADS ---
{ads_block}
--- END ADS ---"""

    return _call_claude_for_json(prompt, max_tokens=1024)


def compare_to_speed(competitor_analysis: dict, speed_ad_examples: list[str]) -> dict:
    """Identify gaps and opportunities by comparing competitor ads to Speed's current copy.

    Args:
        competitor_analysis: Dict returned by analyze_messaging_themes().
        speed_ad_examples:   List of Speed's current ad copy strings.

    Returns:
        Dict with keys: gaps, speed_advantages, angles_to_test,
        ctas_to_test, avoid, recommendations.
    """
    if not speed_ad_examples:
        speed_block = "(no Speed ad examples provided)"
    else:
        speed_block = "\n".join(f"- {ex}" for ex in speed_ad_examples)

    competitor_block = json.dumps(competitor_analysis, indent=2)

    prompt = f"""\
You are a creative strategist for Speed Wallet, a Bitcoin Lightning payment app.
Speed's three target segments: remittance senders (hook: zero fees), iGaming users \
(hook: instant deposits), crypto-curious mainstream users (hook: simplicity + utility).

Below is an analysis of a competitor's Meta ad strategy and examples of Speed's current ad copy.
Return a JSON object with exactly these keys:

{{
  "gaps": ["things the competitor does in ads that Speed currently does not"],
  "speed_advantages": ["messaging angles where Speed has a clear differentiator the competitor lacks"],
  "angles_to_test": ["specific messaging angles Speed should test based on what works for the competitor"],
  "ctas_to_test": ["CTAs worth testing, drawn from competitor patterns"],
  "avoid": ["competitor approaches that look weak, misleading, or misaligned with Speed's brand"],
  "recommendations": "3-4 specific, actionable sentences for Speed's creative team"
}}

Return only valid JSON.

--- COMPETITOR ANALYSIS ---
{competitor_block}
--- END COMPETITOR ANALYSIS ---

--- SPEED'S CURRENT AD COPY ---
{speed_block}
--- END SPEED AD COPY ---"""

    return _call_claude_for_json(prompt, max_tokens=1024)


def _format_ads_for_prompt(ads: list[dict]) -> str:
    lines = []
    for i, ad in enumerate(ads, 1):
        body = ad.get("body", "")
        fmt = ad.get("format", "unknown")
        cta = ad.get("cta", "")
        days = ad.get("days_running", 0)
        active = "active" if ad.get("is_active") else "inactive"
        header = f"Ad {i} | {fmt} | {days}d running ({active}) | CTA: '{cta}'"
        lines.append(f"{header}\n  Copy: {body[:300]}")
    return "\n\n".join(lines)


def _call_claude_for_json(prompt: str, max_tokens: int = 1024) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    # Parse JSON — handle model wrapping it in a markdown code block
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"raw": text, "parse_error": "Could not parse JSON from Claude response"}


# ------------------------------------------------------------------
# Save output
# ------------------------------------------------------------------

def save_analysis(analysis: dict, competitor_name: str) -> Path:
    """Save the analysis dict to data/processed/competitor_analysis_{name}.json."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    slug = competitor_name.lower().replace(" ", "_")
    path = _DATA_DIR / f"competitor_analysis_{slug}.json"
    path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------

def run_analysis(
    competitor_name: str,
    platform: str = "all",
    speed_ad_examples: list[str] | None = None,
) -> dict:
    """Full pipeline: fetch ads → analyse themes → compare to Speed → save.

    Args:
        competitor_name:   Brand to search in Meta Ad Library.
        platform:          "all", "facebook", or "instagram".
        speed_ad_examples: Speed's current ad copy strings for gap analysis.
                           Pass None or [] to skip the comparison step.

    Returns:
        Full analysis dict saved to data/processed/.
    """
    print(f"Fetching Meta Ad Library ads for '{competitor_name}'...")
    ads = fetch_competitor_ads(competitor_name, platform)
    print(f"  {len(ads)} ads with copy fetched")

    if not ads:
        return {"error": "No ads with copy found", "competitor": competitor_name}

    print("Analysing messaging themes with Claude...")
    themes = analyze_messaging_themes(ads)

    comparison: dict = {}
    if speed_ad_examples:
        print("Comparing to Speed's ad copy...")
        comparison = compare_to_speed(themes, speed_ad_examples)

    analysis = {
        "competitor": competitor_name,
        "platform": platform,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_ads": len(ads),
        "ads": ads,
        "messaging_analysis": themes,
        **({"speed_comparison": comparison} if comparison else {}),
    }

    path = save_analysis(analysis, competitor_name)
    print(f"Saved: {path.relative_to(_ROOT)}")
    return analysis


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    # Sample Speed ad copy — replace or extend with real examples.
    SPEED_ADS = [
        "Send Bitcoin instantly. Zero fees. Download Speed.",
        "Stack sats on every purchase with Speed Stacks rewards.",
        "The Lightning wallet built for real life. Speed — instant payments, zero fees.",
        "Send money home without the fees. Speed uses Bitcoin Lightning to cut out the middlemen.",
    ]

    result = run_analysis(
        competitor_name="Coinbase",
        platform="all",
        speed_ad_examples=SPEED_ADS,
    )

    print("\n" + "=" * 60)
    print("MESSAGING ANALYSIS")
    print("=" * 60)
    ma = result.get("messaging_analysis", {})
    if "parse_error" not in ma:
        print(f"Dominant format : {ma.get('dominant_format')}")
        print(f"Buy crypto framing: {ma.get('buy_crypto_framing')}")
        print(f"\nTop CTAs:")
        for cta in ma.get("top_ctas", []):
            print(f"  • {cta}")
        print(f"\nMessaging angles:")
        for angle in ma.get("messaging_angles", []):
            print(f"  • {angle}")
        print(f"\nLongest-running themes (proxy for what works):")
        for theme in ma.get("longest_running_themes", []):
            print(f"  • {theme}")
        print(f"\nSummary: {ma.get('summary')}")
    else:
        print(ma)

    sc = result.get("speed_comparison", {})
    if sc and "parse_error" not in sc:
        print("\n" + "=" * 60)
        print("SPEED GAP ANALYSIS")
        print("=" * 60)
        print("Gaps (what competitor does that Speed doesn't):")
        for g in sc.get("gaps", []):
            print(f"  • {g}")
        print("\nAngles to test:")
        for a in sc.get("angles_to_test", []):
            print(f"  • {a}")
        print(f"\nRecommendations: {sc.get('recommendations')}")
