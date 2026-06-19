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

# Default Ad Library country. No specific target market is documented in
# CLAUDE.md or docs/ (only a generic "EU focus"), so we default to GB (UK) as a
# plausible first market for a fintech wallet. THIS IS AN UNCONFIRMED ASSUMPTION
# — override with --country and confirm the real target market.
_DEFAULT_COUNTRY = "GB"

# Optional registry of known advertiser Page IDs. Searching by Page ID is the
# most precise way to pull a competitor's ads — it returns only that page's ads
# and eliminates keyword noise entirely. Populate from the Meta Ad Library URL
# (the `view_all_page_id=` value on a competitor's page). Left empty by design:
# a wrong ID silently returns the wrong page, so we don't guess.
_KNOWN_PAGE_IDS: dict[str, str] = {
    # "Coinbase": "...",
    # "Cash App": "...",
    # "Strike": "...",
    # "Speed Wallet": "...",
}

# Fintech / payments vocabulary used to keep keyword-search results on-topic.
# An ad survives the relevance filter only if it comes from the exact advertiser
# page OR its copy contains one of these terms. The exact-page match already
# anchors the real advertiser's brand ads, so this list is tuned for PRECISION:
# crypto-specific tokens and money-movement phrases that don't appear in games,
# fashion, or auto ads. Deliberately EXCLUDES ambiguous singletons — bare
# "lightning" (matched a Dubai fashion ad: "every look is a lightning strike"),
# "bank", "exchange", "invest", "wallet", "deposit", "cash"/"money" — which leak
# noise from unrelated verticals.
_FINTECH_TERMS = (
    "bitcoin", "btc", "crypto", "cryptocurrency", "blockchain", "stablecoin",
    "stable coin", "usdt", "usdc", "satoshi", " sats ", "remittance",
    "send money", "money transfer", "transfer money", "buy bitcoin",
    "spend bitcoin", "crypto wallet", "bitcoin wallet", "digital wallet",
    "mobile wallet", "lightning network", "lightning wallet", "lightning payment",
    "peer-to-peer payment", "debit card", "no fees", "zero fees", "low fees",
    "instant transfer",
)


# ------------------------------------------------------------------
# Fetch from Meta Ad Library via Apify
# ------------------------------------------------------------------

def fetch_competitor_ads(
    competitor_name: str,
    platform: str = "all",
    country: str = _DEFAULT_COUNTRY,
    page_id: str | None = None,
) -> list[dict]:
    """Fetch active ads for a competitor from Meta Ad Library.

    Args:
        competitor_name: Brand name to search (e.g. "Coinbase", "Cash App").
        platform:        "all", "facebook", or "instagram".
        country:         ISO country code for the Ad Library (e.g. "GB", "US").
        page_id:         Optional exact advertiser Page ID. If given, only that
                         page's ads are pulled (most precise, no keyword noise).
                         Falls back to _KNOWN_PAGE_IDS[competitor_name] if present.

    Returns:
        List of normalised ad dicts, with off-topic results removed by
        _is_relevant(). Keys: body, title, cta, format, days_running, is_active,
        platforms, page_name.

    Raises:
        EnvironmentError: If APIFY_API_KEY is missing.
        RuntimeError:     If the actor run fails (not a timeout — timeouts are expected).
    """
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise EnvironmentError("APIFY_API_KEY must be set in .env")

    page_id = page_id or _KNOWN_PAGE_IDS.get(competitor_name)
    url = _build_ad_library_url(competitor_name, platform, country, page_id)
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
    discarded = 0
    for item in raw_items:
        parsed = _parse_ad_item(item)
        if not parsed:
            continue
        if not _is_relevant(parsed, competitor_name):
            discarded += 1
            continue
        ads.append(parsed)

    if discarded:
        print(
            f"  Relevance filter: discarded {discarded} off-topic ad(s) "
            f"(kept {len(ads)})"
        )

    return ads


def fetch_speed_ads(
    country: str = _DEFAULT_COUNTRY,
    query: str = "Speed Wallet",
    page_id: str | None = None,
    limit: int = 15,
) -> list[str]:
    """Pull Speed Wallet's own live ad copy from Meta Ad Library.

    Uses the same fetch + relevance mechanism as competitors instead of a
    hardcoded placeholder list. Returns up to `limit` ad-copy strings.
    """
    ads = fetch_competitor_ads(
        query, platform="all", country=country, page_id=page_id
    )
    return [a["body"] for a in ads if a.get("body")][:limit]


def _build_ad_library_url(
    competitor_name: str,
    platform: str,
    country: str,
    page_id: str | None = None,
) -> str:
    params: dict[str, str] = {
        "active_status": "all",
        "ad_type": "all",
        "country": country,
    }
    if page_id:
        # Most precise: pull only this advertiser page's ads — no keyword noise.
        params["view_all_page_id"] = str(page_id)
    else:
        # Exact-phrase match is tighter than keyword_unordered, but the common-word
        # problem (e.g. "Strike") is still cleaned up downstream by _is_relevant().
        params["q"] = competitor_name
        params["search_type"] = "keyword_exact_phrase"
    if platform in ("facebook", "instagram", "messenger"):
        params["publisher_platforms[]"] = platform
    return _AD_LIBRARY_BASE + "?" + urllib.parse.urlencode(params)


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for tolerant name comparison."""
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _is_relevant(ad: dict, competitor_name: str) -> bool:
    """Keep an ad only if it is plausibly from the target advertiser.

    Relevant when EITHER:
      - the ad's page name EXACTLY matches the competitor (so e.g.
        "Critical Strike"/"Football Strike" do NOT match "Strike"), OR
      - the copy contains a genuine fintech/payments term that is NOT merely
        the competitor's own brand name.

    The brand name is stripped from the copy before the keyword check so that
    incidental mentions in third-party sponsorship/event ads don't count — e.g.
    a Moët & Chandon ad naming the "Crypto.com Miami Grand Prix" no longer
    passes just because "Crypto.com" contains the token "crypto".
    """
    page = _normalize(ad.get("page_name", ""))
    comp = _normalize(competitor_name)
    if bool(page) and bool(comp) and page == comp:
        return True

    # Not from the exact advertiser page: require a real fintech signal.
    text = f"{ad.get('body', '')} {ad.get('title', '')}".lower()

    # Remove the competitor's brand name (punctuation-tolerant) so an incidental
    # mention can't, by itself, satisfy the keyword filter.
    brand_core = re.sub(r"[^a-z0-9]+", " ", competitor_name.lower()).strip()
    if brand_core:
        brand_pattern = r"[^a-z0-9]*".join(re.escape(tok) for tok in brand_core.split())
        text = re.sub(brand_pattern, " ", text)

    return any(term in text for term in _FINTECH_TERMS)


def _parse_ad_item(item: dict) -> dict | None:
    """Normalise a raw Apify item into a clean ad dict. Returns None if no body copy."""
    snap = item.get("snapshot", {}) or {}

    # Ad copy — body can be a dict or a plain string depending on the ad type.
    body = snap.get("body", "")
    main_text = body.get("text", "") if isinstance(body, dict) else str(body or "")

    # Carousels repeat the same copy across every card, which previously
    # triplicated the body (e.g. "Invest with Robinhood" x3). Collect the main
    # body plus each card body, then keep only unique phrases (order-preserving).
    fragments = [main_text]
    for card in snap.get("cards", []):
        card_body = card.get("body", "")
        card_text = card_body.get("text", "") if isinstance(card_body, dict) else str(card_body or "")
        fragments.append(card_text)

    seen: set[str] = set()
    unique_fragments: list[str] = []
    for frag in fragments:
        frag = (frag or "").strip()
        if frag and frag not in seen:
            seen.add(frag)
            unique_fragments.append(frag)

    body_text = " ".join(unique_fragments)
    # Strip unrendered template tokens like "{{product.brand}}", then tidy whitespace.
    body_text = re.sub(r"\{\{.*?\}\}", " ", body_text)
    body_text = re.sub(r"\s+", " ", body_text).strip()
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
    country: str = _DEFAULT_COUNTRY,
    page_id: str | None = None,
    speed_ad_examples: list[str] | None = None,
) -> dict:
    """Full pipeline: fetch ads → analyse themes → compare to Speed → save.

    Args:
        competitor_name:   Brand to search in Meta Ad Library.
        platform:          "all", "facebook", or "instagram".
        country:           ISO country code for the Ad Library (e.g. "GB").
        page_id:           Optional exact advertiser Page ID (most precise).
        speed_ad_examples: Speed's current ad copy strings for gap analysis.
                           Pass None or [] to skip the comparison step.

    Returns:
        Full analysis dict saved to data/processed/.
    """
    print(f"Fetching Meta Ad Library ads for '{competitor_name}' (country={country})...")
    ads = fetch_competitor_ads(competitor_name, platform, country, page_id)
    print(f"  {len(ads)} relevant ads with copy retained")

    if not ads:
        return {
            "error": "No relevant ads with copy found",
            "competitor": competitor_name,
            "country": country,
            "hint": (
                "Keyword search returned no on-topic ads. Supply the exact "
                "advertiser Page ID via --page-id (or _KNOWN_PAGE_IDS) for a "
                "precise pull."
            ),
        }

    print("Analysing messaging themes with Claude...")
    themes = analyze_messaging_themes(ads)

    comparison: dict = {}
    if speed_ad_examples:
        print("Comparing to Speed's ad copy...")
        comparison = compare_to_speed(themes, speed_ad_examples)

    analysis = {
        "competitor": competitor_name,
        "platform": platform,
        "country": country,
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
# Combined cross-competitor summary
# ------------------------------------------------------------------

def write_combined_summary(competitor_names: list[str]) -> Path:
    """Read saved analyses and write a cross-competitor markdown brief.

    Reads data/processed/competitor_analysis_{slug}.json for each name,
    calls Claude to compare patterns, and saves the result to
    data/processed/competitor_analysis_combined.md.

    Args:
        competitor_names: List of competitor names whose JSON files already exist.

    Returns:
        Path to the saved markdown file.
    """
    analyses: dict[str, dict] = {}
    for name in competitor_names:
        slug = name.lower().replace(" ", "_")
        path = _DATA_DIR / f"competitor_analysis_{slug}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"No saved analysis for '{name}'. Run run_analysis('{name}') first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        analyses[name] = data.get("messaging_analysis", {})

    competitor_blocks = "\n\n".join(
        f"### {name}\n{json.dumps(ma, indent=2)}"
        for name, ma in analyses.items()
    )

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""\
You are a creative strategist for Speed Wallet, a Bitcoin Lightning payment app.
Below are messaging analyses from Meta Ad Library for {len(analyses)} competitors: \
{', '.join(analyses.keys())}.

Write a strategic brief in GitHub-flavoured Markdown. Use exactly this structure:

# Competitor Ad Analysis — Combined Summary
*{fetched_at}*

## Patterns common across all competitors
[Paragraph covering messaging themes, formats, and tactics all three share]

## What makes each competitor distinct
[One paragraph per competitor — their unique angle, tone, or creative approach]

## The biggest gap Speed can exploit
[One focused paragraph on the most defensible, unclaimed messaging angle \
across all three. Be specific — name the angle, why it's unclaimed, and how \
Speed's product (zero fees, Lightning, Speed Stacks, remittance/iGaming/crypto-curious \
segments) can own it.]

Keep it under 600 words total. Write for a marketing strategist, not a data scientist.

--- COMPETITOR ANALYSES ---
{competitor_blocks}
--- END ---"""

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    markdown = response.content[0].text.strip()

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _DATA_DIR / "competitor_analysis_combined.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyse a competitor's Meta Ad Library ads vs Speed's own ads."
    )
    parser.add_argument(
        "--competitor", default="Coinbase",
        help='Competitor brand name to analyse (e.g. "Cash App").',
    )
    parser.add_argument(
        "--country", default=None,
        help=f"ISO country code for the Ad Library (default: {_DEFAULT_COUNTRY}).",
    )
    parser.add_argument(
        "--platform", default="all", choices=["all", "facebook", "instagram", "messenger"],
    )
    parser.add_argument(
        "--page-id", default=None,
        help="Exact advertiser Page ID for the competitor (most precise).",
    )
    parser.add_argument(
        "--speed-query", default="Speed Wallet",
        help="Search term for Speed's own ads (default: 'Speed Wallet').",
    )
    parser.add_argument(
        "--speed-page-id", default=None,
        help="Exact Page ID for Speed's own advertiser page (most precise).",
    )
    parser.add_argument(
        "--no-speed-compare", action="store_true",
        help="Skip the Speed gap-analysis comparison step.",
    )
    args = parser.parse_args()

    country = args.country or _DEFAULT_COUNTRY
    if not args.country:
        print("⚠️  ASSUMPTION: no target market is documented in CLAUDE.md/docs and")
        print(f"⚠️  no --country was given, so defaulting to '{_DEFAULT_COUNTRY}' (UK) as a")
        print("⚠️  likely first market for a fintech wallet. CONFIRM the real target")
        print("⚠️  market before relying on these results — do not treat GB as settled.\n")

    # Pull Speed's own ads dynamically via the same Ad Library mechanism.
    speed_ads: list[str] = []
    if not args.no_speed_compare:
        print(f"Fetching Speed's own ads (query='{args.speed_query}', country={country})...")
        try:
            speed_ads = fetch_speed_ads(
                country=country, query=args.speed_query, page_id=args.speed_page_id
            )
            print(f"  {len(speed_ads)} Speed ad(s) retained")
        except (EnvironmentError, RuntimeError, ApifyApiError) as e:
            print(f"  Could not fetch Speed ads: {e}")
        if not speed_ads:
            print(
                "  No Speed ads found — gap analysis will be skipped. "
                "Supply --speed-page-id for a precise pull.\n"
            )

    result = run_analysis(
        competitor_name=args.competitor,
        platform=args.platform,
        country=country,
        page_id=args.page_id,
        speed_ad_examples=speed_ads,
    )

    if "error" in result:
        print(f"\n{result['error']}")
        if result.get("hint"):
            print(result["hint"])
        sys.exit(0)

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
