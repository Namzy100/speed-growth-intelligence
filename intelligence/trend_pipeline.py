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

_DOCS = _ROOT / "docs"
_STATE_DIR = _ROOT / "data" / "processed"
_MODEL = "claude-sonnet-4-6"
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
                 "instagram_filtered": 0}

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
            "key": key, "id": ",".join(ids), "part": "snippet,statistics"}, timeout=30)
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
        raw.append({
            "platform": "YouTube", "category": term, "channelId": sn.get("channelId", ""),
            "segment": classify_segment(title, term), "title": title,
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
# Collect
# ------------------------------------------------------------------

_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]


def collect_signals() -> dict:
    if not os.getenv("APIFY_API_KEY"):
        raise EnvironmentError("APIFY_API_KEY must be set in .env")
    for k in _FILTER_STATS:
        _FILTER_STATS[k] = 0

    fetcher = TikTokCreatorFetcher(os.getenv("APIFY_API_KEY"))
    youtube, tiktok = [], []
    for term in _CATEGORIES:
        print(f"Scanning '{term}'...")
        yt = _fetch_youtube(term)
        tt = _fetch_tiktok(fetcher, term)
        print(f"  YouTube(US): {len(yt)} · TikTok(en): {len(tt)}")
        youtube += yt
        tiktok += tt

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
    for v in youtube + tiktok + instagram:
        if v["url"] and v["url"] not in urls:
            urls.add(v["url"])
            all_items.append(v)

    # Top hooks: rank by ER, but only genuinely relevant, English, non-trivial
    # items — exclude 'general' (viral junk that merely matched a search term)
    # and re-apply the English check to the hook text itself.
    top_hooks = sorted(
        [v for v in all_items if v["views"] >= 10_000
         and v["segment"] in _SEGMENTS and _is_english(v["title"])],
        key=lambda x: x["er"], reverse=True)

    _BUCKET = {"YouTube": "youtube", "TikTok": "tiktok", "Instagram": "instagram"}
    by_segment = {seg: {"youtube": [], "tiktok": [], "instagram": [], "organic": [], "paid": []}
                  for seg in _SEGMENTS}
    for v in all_items:
        seg = v["segment"]
        if seg not in by_segment:
            continue
        by_segment[seg][_BUCKET[v["platform"]]].append(v)
        by_segment[seg][v["track"]].append(v)

    signal = {}
    for seg in _SEGMENTS:
        yv, tv, iv = (by_segment[seg]["youtube"], by_segment[seg]["tiktok"],
                      by_segment[seg]["instagram"])
        signal[seg] = {
            "youtube_er": round(sum(x["er"] for x in yv) / len(yv), 4) if yv else 0.0,
            "tiktok_er": round(sum(x["er"] for x in tv) / len(tv), 4) if tv else 0.0,
            "instagram_er": round(sum(x["er"] for x in iv) / len(iv), 4) if iv else 0.0,
            "youtube_n": len(yv), "tiktok_n": len(tv), "instagram_n": len(iv),
        }

    snapshot = _snapshot(youtube, tiktok + instagram)
    feedback, died = _feedback(snapshot, _prior_snapshot())

    print(f"Filters dropped: {_FILTER_STATS['youtube_filtered']} non-English YT, "
          f"{_FILTER_STATS['non_us']} non-US YT, {_FILTER_STATS['tiktok_filtered']} non-English TikTok, "
          f"{_FILTER_STATS['instagram_filtered']} non-English IG.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "youtube": youtube, "tiktok": tiktok, "instagram": instagram, "all_items": all_items,
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
