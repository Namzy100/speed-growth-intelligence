"""Trend-to-action pipeline for Speed Wallet — US market, YouTube + TikTok.

Weekly system that turns US trending content into ready-to-use organic ideas and
paid ad hooks:
  1. Pull trending YouTube (Data API, US, last 7 days) + TikTok (Apify) for Speed's
     categories, with STRICT US + English filters.
  2. Per item, extract engagement, save-rate (TikTok), channel size (YouTube), a
     hook pattern, a replication signal, and a Speed segment.
  3. Rank hooks by engagement; the dashboard layer asks Claude to enrich the top
     hooks and generate an organic content calendar + paid ad briefs.
  4. Feedback loop: diff this week's signal against last week's snapshot.

collect_signals() returns the structured data the trend dashboard bakes in.

Run from repo root:  python intelligence/trend_pipeline.py
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from creators.apify_tiktok import TikTokCreatorFetcher
from creators.apify_x import XCreatorFetcher

_DOCS = _ROOT / "docs"
_STATE_DIR = _ROOT / "data" / "processed"
_MODEL = "claude-sonnet-4-6"
# Cheap per-item relevance/fit judge (Part 1 + 3). Same pattern + tier as the
# scorer's _classify_individual_brand: one Haiku call, cached, keyword fallback.
_JUDGE_MODEL = "claude-haiku-4-5"
_RELEVANCE_CACHE = _STATE_DIR / "trend_relevance_cache.json"
# Speed-fit weights (0.4 fintech / 0.3 low-cost / 0.3 reach) and the relevance gate.
_FIT_W = {"fintech": 0.4, "lowcost": 0.3, "reach": 0.3}
_FIT_GATE = 3            # fintech_involvement below this => off-topic, fit forced to 0
_YT_BASE = "https://www.googleapis.com/youtube/v3"

_CATEGORIES = ["bitcoin", "crypto", "remittance", "send money",
               "money transfer", "lightning network", "fintech"]
_RESULTS_PER_QUERY = 30
_PER_CATEGORY = 15
_BENCHMARK_CPI = 3.17

# Instagram has no login-free hashtag discovery, so Reels are pulled PROFILE-based
# from a seed list of US fintech/crypto/remittance creators (username mode works
# without login). Add/remove handles here — irrelevant or off-topic reels are
# filtered out downstream by segment classification + the top-hooks view floor.
_IG_ACTOR = "apify/instagram-reel-scraper"
_IG_RESULTS_PER_HANDLE = 6
INSTAGRAM_HANDLES = [
    # Kept from the original seed (returned usable content).
    "cryptosrus", "altcoinbuzz", "boxmining", "wenmoon", "strike",
    "cashapp", "moonpay", "bitrefill",
    # US-active fintech / crypto / remittance creators & brands.
    "grayscale", "coinbase", "river_financial", "swanbitcoin", "unchainedcapital",
    "strike_app", "muunwallet", "walletofsatoshi", "lightspark", "voltage_cloud",
    "fold_app", "thebitcoinlayer", "breedlove22", "gladstein", "saifedean",
]

# Replicability thresholds (the "anyone-with-a-phone-could-make-this" signal).
_SMALL_VIEWS = 500_000
_SMALL_SUBS = 500_000
_HIGH_ER = 0.03
_HIGH_SAVE_RATE = 0.02

_FILTER_STATS = {"youtube_filtered": 0, "tiktok_filtered": 0, "non_us": 0,
                 "instagram_filtered": 0, "x_filtered": 0, "off_topic": 0}

_IGAMING_KW = {"casino", "bet", "betting", "gambling", "gamble", "poker", "slots",
               "sportsbook", "wager", "roulette", "blackjack", "jackpot", "stake"}
_REMITTANCE_KW = {"send money", "remittance", "remit", "money transfer",
                  "transfer money", "western union", "moneygram", "remesa", "remesas",
                  "remessa", "wire transfer", "send home", "back home", "abroad",
                  "overseas", "diaspora", "nri", "taptapsend", "expat", "send to"}
_CRYPTO_KW = {"bitcoin", "btc", "crypto", "cryptocurrency", "ethereum", "eth",
              "blockchain", "wallet", "lightning", "satoshi", "sats", "altcoin",
              "defi", "web3", "stablecoin", "usdt"}


def _is_english(text: str) -> bool:
    if not text or len(text) < 3:
        return True
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.80


def classify_segment(text: str, category: str = "") -> str:
    t = f"{text} {category}".lower()
    if any(k in t for k in _IGAMING_KW):
        return "iGaming"
    if any(k in t for k in _REMITTANCE_KW):
        return "remittance"
    if any(k in t for k in _CRYPTO_KW):
        return "crypto-curious"
    return "general"


def classify_track(item: dict) -> str:
    """ORGANIC (team can film on a phone) vs PAID (produced ad format)."""
    if item.get("replicable"):
        return "organic"
    if item["platform"] == "TikTok" and item.get("save_rate", 0) >= 0.015:
        return "organic"
    return "paid"


def hook_pattern(text: str) -> str:
    """Approximate the first ~3 seconds — the opening words of the caption/title."""
    return " ".join(str(text or "").split()[:10])


def _iso8601_seconds(dur: str) -> int:
    """Parse a YouTube ISO8601 duration (e.g. 'PT1M30S') to whole seconds; 0 if absent."""
    import re as _re
    m = _re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(dur or ""))
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


# ------------------------------------------------------------------
# Topical relevance + Speed-fit judgment (Part 1 + Part 3)
#
# Keyword presence is NOT topical relevance: a video that says "this is like
# finding bitcoin in 2009" or one about the *weather* "National Lightning
# Detection Network" trips the crypto/lightning keywords but is not ABOUT crypto.
# A single cheap Haiku call reads the real text and makes the judgment, exactly
# like scorer._classify_individual_brand. A keyword fallback keeps it working
# offline / when the API is unavailable. Results are cached by URL so the weekly
# rerun never re-pays for a video it already judged.
# ------------------------------------------------------------------

_JUDGE_PROMPT = (
    "You judge whether a trending social video is GENUINELY about Speed Wallet's space "
    "and how well it fits Speed.\n\n"
    "Speed is a Bitcoin + stablecoin app onboarding new users to actually invest in and "
    "use crypto for payments, remittances, and instant deposits. It uses GAMIFICATION "
    "(streaks, daily habits) — trends leaning into that mechanic are a plus.\n\n"
    "Read the content and return ONLY JSON:\n"
    "{\n"
    ' "on_topic": true/false,        // is the video ACTUALLY ABOUT crypto/fintech/money-'
    "movement/remittance/iGaming as its SUBJECT — not a passing metaphor, a homonym "
    "(e.g. weather 'lightning', laser 'lightbridge'), or a throwaway mention?\n"
    ' "fintech_involvement": 0-10,   // how central crypto/fintech/money-movement is (0 = '
    "unrelated/metaphor only, 10 = entirely about it)\n"
    ' "replicability": 0-10,         // how cheap/easy for a small team to replicate on a '
    "phone (10 = phone-only talking head/text-on-screen, 0 = big production)\n"
    ' "gamification": true/false,    // leans on streaks/daily-habit/challenge mechanics '
    "Speed could mirror?\n"
    ' "reputational_risk": "none|low|medium|high",  // would associating Speed with this '
    "look bad (crude, scammy, low-quality, gambling-harm)?\n"
    ' "reason": "one short line"\n'
    "}\n\n"
    "Content:\n"
    "platform: {platform}\n"
    "search term it matched: {category}\n"
    "title/caption: {title}\n"
    "description: {description}\n"
    "hashtags: {tags}"
)


def _load_relevance_cache() -> dict:
    if _RELEVANCE_CACHE.exists():
        try:
            return json.loads(_RELEVANCE_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_relevance_cache(cache: dict) -> None:
    _RELEVANCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _RELEVANCE_CACHE.write_text(json.dumps(cache), encoding="utf-8")


def _cache_key(item: dict) -> str:
    return item.get("url") or f"{item.get('platform','')}|{item.get('title','')[:80]}"


def _fallback_judge(item: dict) -> dict:
    """Deterministic keyword-based judgment when the LLM can't run.

    Preserves the OLD behaviour (segment!=general => on-topic) so the pipeline
    still functions offline — just without the metaphor/homonym discrimination
    the LLM adds. Marked source='fallback' so a run's provenance is visible.
    """
    on = item.get("segment", "general") in _SEGMENTS
    return {
        "on_topic": on,
        "fintech_involvement": 6 if on else 0,
        "replicability": 8 if item.get("replicable") else 4,
        "gamification": False,
        "reputational_risk": "none",
        "reason": "keyword fallback (LLM unavailable)",
        "source": "fallback",
    }


def _judge_llm(item: dict, client, feedback: str | None = None) -> dict | None:
    prompt = (_JUDGE_PROMPT
              .replace("{platform}", str(item.get("platform", "")))
              .replace("{category}", str(item.get("category", "")))
              .replace("{title}", str(item.get("title", ""))[:400])
              .replace("{description}", str(item.get("description", ""))[:400])
              .replace("{tags}", ",".join(item.get("hashtags", [])[:10])))
    if feedback:
        # Corrective context from the checker's grader (Outcomes revision loop).
        # The judge re-decides with the specific complaint in view — this is the
        # ONLY thing "revision" does: re-run the judgment on this item with the
        # grader's reason appended. No re-scraping.
        prompt += ("\n\nA reviewer flagged the previous judgment of this item as "
                   f"WRONG. Re-judge it carefully, taking this feedback into "
                   f"account:\n{str(feedback)[:600]}")
    try:
        r = client.messages.create(model=_JUDGE_MODEL, max_tokens=220,
                                   messages=[{"role": "user", "content": prompt}])
        t = r.content[0].text.strip()
        t = t[t.find("{"): t.rfind("}") + 1]
        j = json.loads(t)
        j["source"] = "llm"
        # coerce/clamp
        j["on_topic"] = bool(j.get("on_topic"))
        j["fintech_involvement"] = max(0.0, min(10.0, float(j.get("fintech_involvement", 0))))
        j["replicability"] = max(0.0, min(10.0, float(j.get("replicability", 0))))
        j["gamification"] = bool(j.get("gamification"))
        if j.get("reputational_risk") not in ("none", "low", "medium", "high"):
            j["reputational_risk"] = "none"
        return j
    except Exception:
        return None


def _reach_score(item: dict) -> float:
    """0-10 from REAL metrics already pulled: log-scaled views blended with ER.
    1k views -> ~0, 10M -> ~7 (views component); 10% ER -> full ER component."""
    import math
    views = max(int(item.get("views", 0) or 0), 1)
    vlog = min(max((math.log10(views) - 3) / 4, 0.0), 1.0)
    er = min((item.get("er", 0) or 0) / 0.10, 1.0)
    return round((0.7 * vlog + 0.3 * er) * 10, 1)


def _fit_score(judgment: dict, item: dict) -> tuple[float, bool]:
    """Speed-fit 0-10 and an off_brand flag.

    Gate: off-topic (or fintech < _FIT_GATE) => fit 0 (never surfaces on reach
    alone — the whole point of Part 1). Otherwise the locked 0.4/0.3/0.3 blend.
    High reputational_risk does NOT zero the score; it sets off_brand=True so the
    dashboard surfaces it WITH a warning badge (a decision left to a human).
    """
    fin = judgment.get("fintech_involvement", 0)
    if not judgment.get("on_topic") or fin < _FIT_GATE:
        return 0.0, False
    reach = _reach_score(item)
    low = judgment.get("replicability", 0)
    fit = _FIT_W["fintech"] * fin + _FIT_W["lowcost"] * low + _FIT_W["reach"] * reach
    off_brand = judgment.get("reputational_risk") == "high"
    return round(fit, 1), off_brand


def judge_and_score(items: list[dict]) -> None:
    """Attach on_topic / fintech / replicability / risk / gamification / fit_score
    to each item IN PLACE. Cached by URL; LLM-judged concurrently with a keyword
    fallback. Platform-agnostic: runs identically for YouTube/TikTok/Instagram/X."""
    from concurrent.futures import ThreadPoolExecutor
    cache = _load_relevance_cache()
    client = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        except Exception:
            client = None

    def resolve(item: dict) -> dict:
        key = _cache_key(item)
        if key in cache:
            return cache[key]
        j = _judge_llm(item, client) if client else None
        if j is None:
            j = _fallback_judge(item)
        cache[key] = j
        return j

    # Judge uncached items concurrently (cache hits return instantly inside resolve).
    with ThreadPoolExecutor(max_workers=8) as ex:
        judgments = list(ex.map(resolve, items))

    for item, j in zip(items, judgments):
        _attach_judgment(item, j)

    _save_relevance_cache(cache)


def _attach_judgment(item: dict, j: dict) -> None:
    """Write a judgment dict's fields onto an item IN PLACE, incl. the fit score.
    Single source of truth for the item shape (used by judge_and_score + rejudge)."""
    item["on_topic"] = j["on_topic"]
    item["fintech_involvement"] = j["fintech_involvement"]
    item["replicability"] = j["replicability"]
    item["gamification"] = j["gamification"]
    item["reputational_risk"] = j["reputational_risk"]
    item["relevance_reason"] = j.get("reason", "")
    item["relevance_source"] = j.get("source", "llm")
    fit, off_brand = _fit_score(j, item)
    item["fit_score"] = fit
    item["off_brand"] = off_brand


def rejudge_items(items: list[dict], ids: list[str], feedback: str) -> list[dict]:
    """Re-run the judgment on ONLY the items whose id/url is in `ids`, with the
    checker's `feedback` appended to the judgment prompt. This is the revision
    step of the Outcomes loop: no re-scraping, just a corrected re-judgment of the
    flagged items. Overrides the relevance cache for those items (the cached
    verdict is the one that was flagged). Returns the corrected item dicts.
    """
    from concurrent.futures import ThreadPoolExecutor
    idset = set(ids or [])
    targets = [it for it in items if (it.get("id") or it.get("url")) in idset]
    if not targets:
        return []
    cache = _load_relevance_cache()
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None

    def redo(item: dict) -> dict:
        j = _judge_llm(item, client, feedback=feedback) if client else None
        if j is None:
            j = _fallback_judge(item)
        cache[_cache_key(item)] = j          # override the flagged verdict
        _attach_judgment(item, j)
        return item

    with ThreadPoolExecutor(max_workers=8) as ex:
        corrected = list(ex.map(redo, targets))
    _save_relevance_cache(cache)
    return corrected


def verify_fit_invariants(items: list[dict]) -> list[dict]:
    """Deterministic (non-LLM) check of the two mechanical invariants, so the LLM
    grader only has to judge relevance DEFENSIBILITY. Returns a list of violation
    dicts (empty == clean). A violation here is a CODE bug (weighting/gate drift),
    not a judgment miss — the caller hard-fails rather than entering revision."""
    violations = []
    for it in items:
        iid = it.get("id") or it.get("url") or it.get("title", "")[:40]
        fin = float(it.get("fintech_involvement", 0) or 0)
        low = float(it.get("replicability", 0) or 0)
        on = bool(it.get("on_topic"))
        stored = float(it.get("fit_score", 0) or 0)
        if not on or fin < _FIT_GATE:
            if stored != 0.0:
                violations.append({"id": iid, "kind": "gate_not_enforced",
                                   "detail": f"off-topic (on_topic={on}, fintech={fin}) but fit={stored} (expected 0)"})
            continue
        expected = round(_FIT_W["fintech"] * fin + _FIT_W["lowcost"] * low
                         + _FIT_W["reach"] * _reach_score(it), 1)
        if abs(stored - expected) > 0.11:   # 0.1 rounding tolerance
            violations.append({"id": iid, "kind": "fit_miscomputed",
                               "detail": f"stored fit={stored}, recomputed 0.4/0.3/0.3={expected}"})
    return violations


# ------------------------------------------------------------------
# YouTube (Data API v3) — US, last 7 days, with channel-level US filter
# ------------------------------------------------------------------

def _yt_channels(key: str, ids: list[str]) -> dict:
    """Return {channelId: {country, subs, desc}} via channels.list (batched)."""
    out = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            r = requests.get(f"{_YT_BASE}/channels", params={
                "key": key, "id": ",".join(batch), "part": "snippet,statistics"}, timeout=30)
            if r.status_code != 200:
                continue
            for it in r.json().get("items", []):
                sn, st = it.get("snippet", {}), it.get("statistics", {})
                out[it["id"]] = {
                    "country": sn.get("country", ""),
                    "subs": int(st.get("subscriberCount", 0) or 0),
                    "desc": sn.get("description", ""),
                }
        except requests.RequestException:
            continue
    return out


def _fetch_youtube(term: str) -> list[dict]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return []
    after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        s = requests.get(f"{_YT_BASE}/search", params={
            "key": key, "q": term, "part": "snippet", "type": "video",
            "order": "viewCount", "publishedAfter": after, "maxResults": 12,
            "regionCode": "US", "relevanceLanguage": "en"}, timeout=30)
        if s.status_code != 200:
            print(f"    YouTube '{term}' search error {s.status_code}")
            return []
        ids = [it["id"]["videoId"] for it in s.json().get("items", [])
               if it.get("id", {}).get("videoId")]
        if not ids:
            return []
        v = requests.get(f"{_YT_BASE}/videos", params={
            "key": key, "id": ",".join(ids),
            "part": "snippet,statistics,contentDetails"}, timeout=30)
        if v.status_code != 200:
            return []
    except requests.RequestException as e:
        print(f"    YouTube '{term}' failed: {e}")
        return []

    raw = []
    for it in v.json().get("items", []):
        sn, st = it["snippet"], it.get("statistics", {})
        views = int(st.get("viewCount", 0) or 0)
        if views <= 0:
            continue
        title = sn.get("title", "")
        if not _is_english(title):
            _FILTER_STATS["youtube_filtered"] += 1
            continue
        likes = int(st.get("likeCount", 0) or 0)
        comments = int(st.get("commentCount", 0) or 0)
        thumbs = sn.get("thumbnails", {}) or {}
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
        # Real content-type signal: parse ISO8601 duration — <=3min reads as
        # short-form, otherwise a long-form video. (Shorts vs standard upload.)
        dur = _iso8601_seconds(it.get("contentDetails", {}).get("duration", ""))
        raw.append({
            "platform": "YouTube", "category": term, "channelId": sn.get("channelId", ""),
            "segment": classify_segment(title, term), "title": title,
            # Real description (already in the snippet we fetch) — the relevance
            # judge reads it so YouTube isn't judged on the title alone.
            "description": sn.get("description", "") or "",
            "content_type": "short_video" if (0 < dur <= 180) else "long_video",
            "channel": sn.get("channelTitle", ""), "views": views, "likes": likes,
            "comments": comments, "er": round((likes + comments) / views, 4),
            "save_rate": 0.0, "publish_date": sn.get("publishedAt", "")[:10],
            "url": f"https://www.youtube.com/watch?v={it['id']}", "thumbnail": thumb,
            "hook": hook_pattern(title),
        })

    # US filter via channel country / description language, and attach subs.
    chans = _yt_channels(key, list({r["channelId"] for r in raw if r["channelId"]}))
    kept = []
    for r in raw:
        c = chans.get(r["channelId"], {})
        country, desc = c.get("country", ""), c.get("desc", "")
        is_us = (country == "US") or (not country and _is_english(desc))
        if not is_us:
            _FILTER_STATS["non_us"] += 1
            continue
        r["subs"] = c.get("subs", 0)
        # "Low production value / replicable" signal: small creator, high engagement.
        r["replicable"] = (r["views"] < _SMALL_VIEWS and 0 < r["subs"] < _SMALL_SUBS
                           and r["er"] > _HIGH_ER)
        r["track"] = classify_track(r)
        kept.append(r)
    kept.sort(key=lambda x: x["views"], reverse=True)
    return kept[:_PER_CATEGORY]


# ------------------------------------------------------------------
# TikTok (Apify search) — strict English, prioritized by save-rate
# ------------------------------------------------------------------

def _fetch_tiktok(fetcher: TikTokCreatorFetcher, term: str) -> list[dict]:
    try:
        items = fetcher.search_videos([term], results_per_query=_RESULTS_PER_QUERY)
    except Exception as e:
        print(f"    TikTok '{term}' failed: {e}")
        return []
    out = []
    for it in items:
        views = int(it.get("playCount", 0) or 0)
        if views <= 0:
            continue
        # Strict: TikTok must self-tag the caption as English.
        if str(it.get("textLanguage", "")).lower() != "en":
            _FILTER_STATS["tiktok_filtered"] += 1
            continue
        likes = int(it.get("diggCount", 0) or 0)
        comments = int(it.get("commentCount", 0) or 0)
        shares = int(it.get("shareCount", 0) or 0)
        saves = int(it.get("collectCount", 0) or 0)
        caption = " ".join(str(it.get("text", "")).split())
        hashtags = [h.get("name", "") if isinstance(h, dict) else str(h)
                    for h in (it.get("hashtags", []) or [])]
        item = {
            "platform": "TikTok", "category": term,
            "segment": classify_segment(caption + " " + " ".join(hashtags), term),
            # Real content-type signal: the actor exposes isSlideshow (photo carousel)
            # vs a normal short video. This is the only static-post signal any current
            # actor gives us; IG (reel-only actor) and YouTube have none.
            "content_type": "slideshow" if it.get("isSlideshow") else "short_video",
            "description": caption,   # caption is the full text the judge reads
            "title": caption[:140], "channel": (it.get("authorMeta", {}) or {}).get("name", ""),
            "views": views, "likes": likes, "comments": comments, "shares": shares,
            "saves": saves, "er": round((likes + comments + shares + saves) / views, 4),
            "save_rate": round(saves / views, 4), "subs": 0,
            "duration": int((it.get("videoMeta", {}) or {}).get("duration", 0) or 0),
            "publish_date": str(it.get("createTimeISO", ""))[:10],
            "url": it.get("webVideoUrl", ""), "hashtags": [h.lower() for h in hashtags if h],
            "thumbnail": "", "hook": hook_pattern(caption),
        }
        # TikTok is phone-native; strong save-rate marks the most copyable pieces.
        item["replicable"] = item["save_rate"] >= _HIGH_SAVE_RATE or item["er"] > _HIGH_ER
        item["track"] = classify_track(item)
        out.append(item)
    # Prioritize by save-rate (intent to copy/return), then views.
    out.sort(key=lambda x: (x["save_rate"], x["views"]), reverse=True)
    return out[:_PER_CATEGORY]


# ------------------------------------------------------------------
# Instagram Reels (Apify, profile/username mode — works without login)
# ------------------------------------------------------------------

def _fetch_instagram_reels(handles: list[str]) -> list[dict]:
    """Recent Reels from a seed list of creator handles, shaped like YT/TikTok items.

    Instagram blocks login-free hashtag discovery, so this scrapes named profiles
    instead. Handles with no public reels return error records (skipped). Off-topic
    or old reels are filtered out later by segment classification + the view floor.
    """
    from apify_client import ApifyClient
    client = ApifyClient(os.getenv("APIFY_API_KEY"))
    try:
        run = client.actor(_IG_ACTOR).call(run_input={
            "username": handles, "resultsLimit": _IG_RESULTS_PER_HANDLE})
        ds = run.default_dataset_id if hasattr(run, "default_dataset_id") else run["defaultDatasetId"]
        items = list(client.dataset(ds).iterate_items())
    except Exception as e:
        print(f"    Instagram Reels fetch failed: {e}")
        return []

    out = []
    for it in items:
        if it.get("error") or "videoViewCount" not in it:
            continue  # no-reels / restricted / error record
        views = int(it.get("videoViewCount", 0) or 0)
        if views <= 0:
            continue
        caption = " ".join(str(it.get("caption", "")).split())
        if not _is_english(caption):
            _FILTER_STATS["instagram_filtered"] += 1
            continue
        likes = int(it.get("likesCount", 0) or 0)
        comments = int(it.get("commentsCount", 0) or 0)
        handle = it.get("ownerUsername", "")
        item = {
            "platform": "Instagram", "category": handle,
            "segment": classify_segment(caption, handle),
            # The actor is reel-only, so every IG item is short-form video.
            "content_type": "short_video", "description": caption,
            "title": caption[:140], "channel": handle,
            "views": views, "likes": likes, "comments": comments, "shares": 0,
            "saves": 0, "er": round((likes + comments) / views, 4),
            "save_rate": 0.0, "subs": 0,
            "publish_date": str(it.get("timestamp", ""))[:10],
            "url": it.get("url", ""), "hashtags": [],
            "thumbnail": "", "hook": hook_pattern(caption),
        }
        # Reels are phone-native like TikTok; high engagement marks copyable pieces.
        item["replicable"] = item["er"] > _HIGH_ER
        item["track"] = classify_track(item)
        out.append(item)
    out.sort(key=lambda x: x["views"], reverse=True)
    return out


# ------------------------------------------------------------------
# X / Twitter (Apify search) — topic search via the existing X fetcher
# ------------------------------------------------------------------

def _x_content_type(t: dict) -> str:
    """Best-effort real content type for a tweet: video / image / text_post.
    X is largely text, so 'text_post' is the honest default when no media."""
    media = (t.get("extendedEntities", {}) or {}).get("media") \
        or t.get("media") or []
    types = {str((m or {}).get("type", "")).lower() for m in media if isinstance(m, dict)}
    if "video" in types or "animated_gif" in types:
        return "short_video"
    if "photo" in types or "image" in types:
        return "static_image"
    return "text_post"


def _fetch_x(fetcher: "XCreatorFetcher", term: str) -> list[dict]:
    """Topic-search X and shape tweets like the other platforms' items, so the
    relevance judge + fit score apply to X uniformly (Part 4)."""
    try:
        tweets = fetcher.search_tweets([term], results_per_query=_RESULTS_PER_QUERY)
    except Exception as e:
        print(f"    X '{term}' failed: {e}")
        return []
    out = []
    for t in tweets:
        views = int(t.get("viewCount", 0) or 0)
        if views <= 0:
            continue
        text = " ".join(str(t.get("fullText") or t.get("text") or "").split())
        if not _is_english(text):
            _FILTER_STATS["x_filtered"] += 1
            continue
        likes = int(t.get("likeCount", 0) or 0)
        rts = int(t.get("retweetCount", 0) or 0)
        replies = int(t.get("replyCount", 0) or 0)
        quotes = int(t.get("quoteCount", 0) or 0)
        hashtags = [ht.get("text", "") if isinstance(ht, dict) else str(ht)
                    for ht in (t.get("entities", {}) or {}).get("hashtags", []) or []]
        author = t.get("author", {}) or {}
        item = {
            "platform": "X", "category": term,
            "segment": classify_segment(text + " " + " ".join(hashtags), term),
            "content_type": _x_content_type(t), "description": text,
            "title": text[:140], "channel": author.get("userName", ""),
            "views": views, "likes": likes, "comments": replies,
            "shares": rts + quotes,
            "er": round((likes + rts + replies + quotes) / views, 4),
            "save_rate": 0.0, "subs": int(author.get("followers", 0) or 0),
            "publish_date": str(t.get("createdAt", ""))[:10],
            "url": t.get("url") or t.get("twitterUrl", ""),
            "hashtags": [h.lower() for h in hashtags if h],
            "thumbnail": "", "hook": hook_pattern(text),
        }
        item["replicable"] = item["er"] > _HIGH_ER
        item["track"] = classify_track(item)
        out.append(item)
    out.sort(key=lambda x: (x["er"], x["views"]), reverse=True)
    return out[:_PER_CATEGORY]


# ------------------------------------------------------------------
# Collect
# ------------------------------------------------------------------

_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]


def collect_signals() -> dict:
    if not os.getenv("APIFY_API_KEY"):
        raise EnvironmentError("APIFY_API_KEY must be set in .env")
    for k in _FILTER_STATS:
        _FILTER_STATS[k] = 0

    fetcher = TikTokCreatorFetcher(os.getenv("APIFY_API_KEY"))
    x_fetcher = XCreatorFetcher(os.getenv("APIFY_API_KEY"))
    youtube, tiktok, x = [], [], []
    for term in _CATEGORIES:
        print(f"Scanning '{term}'...")
        yt = _fetch_youtube(term)
        tt = _fetch_tiktok(fetcher, term)
        xx = _fetch_x(x_fetcher, term)
        print(f"  YouTube(US): {len(yt)} · TikTok(en): {len(tt)} · X(en): {len(xx)}")
        youtube += yt
        tiktok += tt
        x += xx

    seen, yt_unique = set(), []
    for v in sorted(youtube, key=lambda x: x["views"], reverse=True):
        if v["url"] not in seen:
            seen.add(v["url"])
            yt_unique.append(v)
    youtube = yt_unique

    print(f"Scanning Instagram Reels ({len(INSTAGRAM_HANDLES)} handles)...")
    instagram = _fetch_instagram_reels(INSTAGRAM_HANDLES)
    print(f"  Instagram(en): {len(instagram)}")

    # Deduplicate every source by URL.
    all_items, urls = [], set()
    for v in youtube + tiktok + instagram + x:
        if v["url"] and v["url"] not in urls:
            urls.add(v["url"])
            all_items.append(v)

    # Part 1 + 3: judge REAL topical relevance + Speed-fit per item (LLM, cached,
    # keyword fallback). This attaches on_topic / fintech_involvement / fit_score /
    # reputational_risk / gamification, uniformly across ALL four platforms.
    print(f"Judging topical relevance + Speed-fit for {len(all_items)} items...")
    judge_and_score(all_items)
    _FILTER_STATS["off_topic"] = sum(1 for v in all_items if not v.get("on_topic"))

    # Top hooks: genuinely ON-TOPIC (real subject, not a keyword/metaphor hit),
    # English, non-trivial reach — RANKED BY SPEED-FIT (fintech + low-cost + reach),
    # not raw ER. off_brand items still appear (surfaced with a warning), not hidden.
    top_hooks = sorted(
        [v for v in all_items if v["views"] >= 10_000
         and v.get("on_topic") and _is_english(v["title"])],
        key=lambda x: (x.get("fit_score", 0), x["er"]), reverse=True)

    _BUCKET = {"YouTube": "youtube", "TikTok": "tiktok", "Instagram": "instagram", "X": "x"}
    by_segment = {seg: {"youtube": [], "tiktok": [], "instagram": [], "x": [], "organic": [], "paid": []}
                  for seg in _SEGMENTS}
    for v in all_items:
        seg = v["segment"]
        # Only bucket on-topic items — the segment label is only meaningful once
        # the item is confirmed genuinely about the space.
        if seg not in by_segment or not v.get("on_topic"):
            continue
        by_segment[seg][_BUCKET[v["platform"]]].append(v)
        by_segment[seg][v["track"]].append(v)

    signal = {}
    for seg in _SEGMENTS:
        yv, tv, iv, xv = (by_segment[seg]["youtube"], by_segment[seg]["tiktok"],
                          by_segment[seg]["instagram"], by_segment[seg]["x"])
        signal[seg] = {
            "youtube_er": round(sum(x["er"] for x in yv) / len(yv), 4) if yv else 0.0,
            "tiktok_er": round(sum(x["er"] for x in tv) / len(tv), 4) if tv else 0.0,
            "instagram_er": round(sum(x["er"] for x in iv) / len(iv), 4) if iv else 0.0,
            "x_er": round(sum(x["er"] for x in xv) / len(xv), 4) if xv else 0.0,
            "youtube_n": len(yv), "tiktok_n": len(tv), "instagram_n": len(iv), "x_n": len(xv),
        }

    snapshot = _snapshot(youtube, tiktok + instagram + x)
    feedback, died = _feedback(snapshot, _prior_snapshot())

    print(f"Filters dropped: {_FILTER_STATS['youtube_filtered']} non-English YT, "
          f"{_FILTER_STATS['non_us']} non-US YT, {_FILTER_STATS['tiktok_filtered']} non-English TikTok, "
          f"{_FILTER_STATS['instagram_filtered']} non-English IG, {_FILTER_STATS['x_filtered']} non-English X. "
          f"Off-topic (relevance judge): {_FILTER_STATS['off_topic']}.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "youtube": youtube, "tiktok": tiktok, "instagram": instagram, "x": x,
        "all_items": all_items,
        "top_hooks": top_hooks, "by_segment": by_segment, "platform_signal": signal,
        "snapshot": snapshot, "feedback": feedback, "died": died,
        "filter_stats": dict(_FILTER_STATS),
    }


# ------------------------------------------------------------------
# Feedback loop
# ------------------------------------------------------------------

def _snapshot(youtube: list[dict], tiktok: list[dict]) -> dict:
    def agg(items, platform):
        by_cat = {}
        for v in items:
            c = by_cat.setdefault(v["category"], {"reach": [], "er": [], "tags": Counter()})
            c["reach"].append(v["views"])
            c["er"].append(v["er"])
            for h in v.get("hashtags", []):
                c["tags"][h] += 1
        return {f"{cat}|{platform}": {
            "median_reach": int(median(d["reach"])) if d["reach"] else 0,
            "avg_er": round(sum(d["er"]) / len(d["er"]), 4) if d["er"] else 0.0,
            "hashtags": dict(d["tags"].most_common(12))} for cat, d in by_cat.items()}
    cats = {}
    cats.update(agg(youtube, "YouTube"))
    cats.update(agg(tiktok, "TikTok"))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "categories": cats}


def _prior_snapshot() -> dict | None:
    files = sorted(_STATE_DIR.glob("trend_pipeline_state_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _feedback(current: dict, prior: dict | None) -> tuple[str, list[str]]:
    if not prior:
        return ("No prior snapshot found — baseline week. Next week's run will show "
                "what gained traction, died, or emerged.", [])
    lines, died = [], []
    for key, cur in current["categories"].items():
        old = prior.get("categories", {}).get(key)
        if not old:
            lines.append(f"  NEW tracked: {key}")
            continue
        o_reach = old.get("median_reach", 0) or 0
        delta = ((cur["median_reach"] - o_reach) / o_reach * 100) if o_reach else 0
        arrow = "▲" if delta > 5 else "▼" if delta < -5 else "▬"
        cur_t, old_t = set(cur["hashtags"]), set(old.get("hashtags", {}))
        emerging, faded = sorted(cur_t - old_t)[:5], sorted(old_t - cur_t)[:5]
        lines.append(f"  {key}: reach {arrow} {delta:+.0f}% | emerging: "
                     f"{', '.join('#'+t for t in emerging) or '—'} | faded: "
                     f"{', '.join('#'+t for t in faded) or '—'}")
        if delta < -15:
            died.append(f"{key}: reach fell {delta:.0f}% week-over-week — cooling off.")
        for t in faded[:3]:
            died.append(f"#{t} ({key.split('|')[1]}) dropped out of the top hashtags.")
    return (f"Compared against snapshot from {prior.get('generated_at','')[:10]}:\n" + "\n".join(lines), died)


# ------------------------------------------------------------------
# .txt report (standalone run) — briefs via Claude
# ------------------------------------------------------------------

def _digest(data: dict) -> str:
    lines = []
    for v in data["top_hooks"][:20]:
        lines.append(f"[{v['segment']}/{v['track']}] {v['platform']} {v['views']:,}v "
                     f"{v['er']:.1%}ER save{v.get('save_rate',0):.1%} — {v['hook']}")
    return "TOP HOOKS THIS WEEK (US, ranked by engagement):\n" + "\n".join(lines)


def run() -> Path:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    data = collect_signals()
    if not data["all_items"]:
        raise RuntimeError("No trend data returned.")
    digest = _digest(data)

    print(f"\nGenerating briefs ({_MODEL})...")
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(model=_MODEL, max_tokens=2600, messages=[{"role": "user",
        "content": ("You are Speed Wallet's growth-creative strategist (Bitcoin Lightning app; "
            "segments: remittance/zero-fee, crypto-curious/simplicity, iGaming/instant deposits). "
            f"Benchmark CPI ${_BENCHMARK_CPI:.2f}. From these US trending hooks, write a MOMENTUM "
            "line, then 5-7 ranked creative briefs (hook, 15s script, segment, organic-or-paid, "
            "platform, est CPI impact). Plain text.\n\n" + data["feedback"] + "\n\n" + digest)}])
    briefs = resp.content[0].text

    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    header = ("=" * 70 + "\nSPEED WALLET — TREND-TO-ACTION PIPELINE (US · YouTube + TikTok)\n"
              f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
              f"{len(data['youtube'])} YouTube + {len(data['tiktok'])} TikTok · "
              f"filters: {data['filter_stats']}\n" + "=" * 70 + "\n\n"
              "FEEDBACK LOOP\n" + "-" * 32 + "\n" + data["feedback"] + "\n\n\nBRIEFS\n" + "-" * 32 + "\n\n")
    out = _DOCS / f"trend_pipeline_{stamp}.txt"
    out.write_text(header + briefs + "\n\n\n--- RAW TOP HOOKS ---\n\n" + digest, encoding="utf-8")
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    (_STATE_DIR / f"trend_pipeline_state_{stamp}.json").write_text(
        json.dumps(data["snapshot"], indent=2), encoding="utf-8")
    print(f"\nSaved: {out.relative_to(_ROOT)}")
    return out


if __name__ == "__main__":
    try:
        path = run()
        print("=" * 60)
        print(path.read_text(encoding="utf-8")[:2500])
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
