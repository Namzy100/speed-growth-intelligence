"""Trend-to-action pipeline for Speed Wallet (YouTube + TikTok).

Weekly system that turns trending content into ready-to-brief ad creative:
  1. Pull trending YouTube videos (Data API, last 7 days, by views) + TikTok
     videos (Apify search) for Speed's categories.
  2. Classify each item into a Speed segment (remittance / iGaming / crypto-curious)
     and extract engagement: YouTube ER = (likes+comments)/views; TikTok true ER =
     (likes+comments+shares+saves)/views.
  3. Claude classifies trends (usable / adaptable / irrelevant) and writes ranked
     creative briefs.
  4. Feedback loop: diff this week's signal against last week's snapshot.

Instagram was removed: the anonymous hashtag scraper returns errors, not posts.

Output: docs/trend_pipeline_YYYY_MM_DD.txt (+ a machine snapshot in data/processed/).
Reusable: collect_signals() returns the structured data the trend dashboard bakes in.

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
_BENCHMARK_CPI = 3.17  # Payday - Android - Broad+ (best Meta CPI)

# Segment keyword sets. Priority at classify time: iGaming > remittance > crypto.
_IGAMING_KW = {"casino", "bet", "betting", "gambling", "gamble", "poker", "slots",
               "sportsbook", "wager", "roulette", "blackjack", "jackpot", "stake"}
_REMITTANCE_KW = {"send money", "remittance", "remit", "money transfer",
                  "transfer money", "western union", "moneygram", "remesa", "remesas",
                  "remessa", "wire transfer", "send home", "back home", "abroad",
                  "overseas", "diaspora", "nri", "taptapsend", "expat", "send to"}
_CRYPTO_KW = {"bitcoin", "btc", "crypto", "cryptocurrency", "ethereum", "eth",
              "blockchain", "wallet", "lightning", "satoshi", "sats", "altcoin",
              "defi", "web3", "stablecoin", "usdt"}


# Tracks how many items each platform's language filter dropped (per collect run).
_FILTER_STATS = {"youtube_filtered": 0, "tiktok_filtered": 0}


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


# ------------------------------------------------------------------
# YouTube (Data API v3)
# ------------------------------------------------------------------

def _fetch_youtube(term: str) -> list[dict]:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return []
    after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        s = requests.get(f"{_YT_BASE}/search", params={
            "key": key, "q": term, "part": "snippet", "type": "video",
            "order": "viewCount", "publishedAfter": after, "maxResults": 10,
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

    out = []
    for it in v.json().get("items", []):
        sn, st = it["snippet"], it.get("statistics", {})
        views = int(st.get("viewCount", 0) or 0)
        likes = int(st.get("likeCount", 0) or 0)
        comments = int(st.get("commentCount", 0) or 0)
        if views <= 0:
            continue
        thumbs = sn.get("thumbnails", {}) or {}
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
        title = sn.get("title", "")
        out.append({
            "platform": "YouTube", "category": term,
            "segment": classify_segment(title, term),
            "title": title, "channel": sn.get("channelTitle", ""),
            "views": views, "likes": likes, "comments": comments,
            "er": round((likes + comments) / views, 4),
            "publish_date": sn.get("publishedAt", "")[:10],
            "url": f"https://www.youtube.com/watch?v={it['id']}",
            "thumbnail": thumb,
        })
    # English-only: drop non-English titles (search is already US/en-scoped).
    kept = [v for v in out if _is_english(v["title"])]
    _FILTER_STATS["youtube_filtered"] += len(out) - len(kept)
    kept.sort(key=lambda x: x["views"], reverse=True)
    return kept[:_PER_CATEGORY]


# ------------------------------------------------------------------
# TikTok (Apify search)
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
        likes = int(it.get("diggCount", 0) or 0)
        comments = int(it.get("commentCount", 0) or 0)
        shares = int(it.get("shareCount", 0) or 0)
        saves = int(it.get("collectCount", 0) or 0)  # newly captured
        caption = " ".join(str(it.get("text", "")).split())
        # English-only: keep if TikTok tags it English OR the caption reads ASCII.
        lang = str(it.get("textLanguage", "")).lower()
        if lang != "en" and not _is_english(caption):
            _FILTER_STATS["tiktok_filtered"] += 1
            continue
        hashtags = [h.get("name", "") if isinstance(h, dict) else str(h)
                    for h in (it.get("hashtags", []) or [])]
        author = (it.get("authorMeta", {}) or {}).get("name", "")
        out.append({
            "platform": "TikTok", "category": term,
            "segment": classify_segment(caption + " " + " ".join(hashtags), term),
            "title": caption[:140], "channel": author,
            "views": views, "likes": likes, "comments": comments,
            "shares": shares, "saves": saves,
            # true engagement uses ALL interactions, not just views/likes
            "er": round((likes + comments + shares + saves) / views, 4),
            "duration": int((it.get("videoMeta", {}) or {}).get("duration", 0) or 0),
            "publish_date": str(it.get("createTimeISO", ""))[:10],  # newly captured
            "url": it.get("webVideoUrl", ""),
            "hashtags": [h.lower() for h in hashtags if h],
            "thumbnail": "",
        })
    out.sort(key=lambda x: x["views"], reverse=True)
    return out[:_PER_CATEGORY]


# ------------------------------------------------------------------
# Collect (reusable by the dashboard)
# ------------------------------------------------------------------

_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]


def collect_signals() -> dict:
    """Fetch YouTube + TikTok trends, classify, and return structured data."""
    if not os.getenv("APIFY_API_KEY"):
        raise EnvironmentError("APIFY_API_KEY must be set in .env")

    _FILTER_STATS["youtube_filtered"] = 0
    _FILTER_STATS["tiktok_filtered"] = 0
    fetcher = TikTokCreatorFetcher(os.getenv("APIFY_API_KEY"))
    youtube, tiktok = [], []
    for term in _CATEGORIES:
        print(f"Scanning '{term}'...")
        yt = _fetch_youtube(term)
        tt = _fetch_tiktok(fetcher, term)
        print(f"  YouTube: {len(yt)} · TikTok: {len(tt)}")
        youtube += yt
        tiktok += tt

    # De-dup YouTube by url (same video can match multiple category queries).
    seen = set()
    yt_unique = []
    for v in sorted(youtube, key=lambda x: x["views"], reverse=True):
        if v["url"] not in seen:
            seen.add(v["url"])
            yt_unique.append(v)
    youtube = yt_unique

    by_segment = {seg: {"youtube": [], "tiktok": []} for seg in _SEGMENTS}
    for v in youtube:
        if v["segment"] in by_segment:
            by_segment[v["segment"]]["youtube"].append(v)
    for v in tiktok:
        if v["segment"] in by_segment:
            by_segment[v["segment"]]["tiktok"].append(v)

    # Platform signal: avg ER per segment per platform.
    signal = {}
    for seg in _SEGMENTS:
        yv = by_segment[seg]["youtube"]
        tv = by_segment[seg]["tiktok"]
        signal[seg] = {
            "youtube_er": round(sum(x["er"] for x in yv) / len(yv), 4) if yv else 0.0,
            "tiktok_er": round(sum(x["er"] for x in tv) / len(tv), 4) if tv else 0.0,
            "youtube_n": len(yv), "tiktok_n": len(tv),
        }

    snapshot = _snapshot(youtube, tiktok)
    prior = _prior_snapshot()
    feedback, died = _feedback(snapshot, prior)

    print(f"Language filter dropped: {_FILTER_STATS['youtube_filtered']} YouTube, "
          f"{_FILTER_STATS['tiktok_filtered']} TikTok (non-English).")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "youtube": youtube, "tiktok": tiktok, "by_segment": by_segment,
        "platform_signal": signal, "snapshot": snapshot,
        "feedback": feedback, "died": died,
        "filter_stats": dict(_FILTER_STATS),
    }


# ------------------------------------------------------------------
# Feedback loop (week-over-week)
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
        return {
            f"{cat}|{platform}": {
                "median_reach": int(median(d["reach"])) if d["reach"] else 0,
                "avg_er": round(sum(d["er"]) / len(d["er"]), 4) if d["er"] else 0.0,
                "hashtags": dict(d["tags"].most_common(12)),
            }
            for cat, d in by_cat.items()
        }
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
    """Return (human-readable feedback, list of 'what died' lines)."""
    if not prior:
        return ("No prior snapshot found — this is the baseline week. Next week's "
                "run will show what gained traction, died, or emerged.", [])
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
        emerging = sorted(cur_t - old_t)[:5]
        faded = sorted(old_t - cur_t)[:5]
        lines.append(
            f"  {key}: reach {arrow} {delta:+.0f}% | emerging: "
            f"{', '.join('#'+t for t in emerging) or '—'} | faded: "
            f"{', '.join('#'+t for t in faded) or '—'}"
        )
        if delta < -15:
            died.append(f"{key}: reach fell {delta:.0f}% week-over-week — cooling off.")
        for t in faded[:3]:
            died.append(f"#{t} ({key.split('|')[1]}) dropped out of the top hashtags.")
    prior_date = prior.get("generated_at", "")[:10]
    return (f"Compared against snapshot from {prior_date}:\n" + "\n".join(lines), died)


# ------------------------------------------------------------------
# Digest + Claude briefs (for the .txt report)
# ------------------------------------------------------------------

def _digest(data: dict) -> str:
    parts = []
    for label, items in (("YouTube", data["youtube"]), ("TikTok", data["tiktok"])):
        if not items:
            parts.append(f"{label}: (no data)")
            continue
        top = items[:10]
        lines = [f"{label} — top {len(top)} of {len(items)} by views:"]
        for v in top:
            lines.append(
                f"  [{v['segment']}] {v['views']:,} views, {v['er']:.1%} ER, "
                f"{v['publish_date']} — {v['title'][:90]}"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def generate_briefs(digest: str, feedback: str) -> str:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (
        "You are a growth-creative strategist for Speed Wallet, a Bitcoin Lightning "
        "payments app. Segments: remittance (zero fees), iGaming (instant deposits), "
        "crypto-curious (simplicity). Best current paid CPI benchmark is "
        f"${_BENCHMARK_CPI:.2f} (Meta, Payday Android Broad+).\n\n"
        "Below are this week's trending YouTube + TikTok signals (already segment-tagged), "
        "plus a week-over-week feedback section.\n\n"
        "TASK:\n"
        "1. Open with a 2-line MOMENTUM summary from the feedback (what's rising worth acting on).\n"
        "2. Identify the 5-7 strongest trends. Classify EACH as (a) DIRECTLY USABLE, "
        "(b) ADAPTABLE with a Speed angle, or (c) IRRELEVANT (name + one-line why, then skip).\n"
        "3. For every (a)/(b), a ranked hand-to-creative brief: Trend & evidence "
        "(platform, views/ER); Classification; Exact hook line; 15-second script (3-4 beats); "
        "Speed segment; Paid channel to test first (Meta/TikTok) and why; "
        f"Estimated CPI impact vs the ${_BENCHMARK_CPI:.2f} benchmark (range + rationale).\n\n"
        "Concrete, plain text, no markdown headers.\n\n"
        "--- WEEK-OVER-WEEK FEEDBACK ---\n" + feedback +
        "\n\n--- THIS WEEK'S TREND SIGNALS ---\n" + digest
    )
    resp = client.messages.create(
        model=_MODEL, max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Run (writes the .txt report + snapshot)
# ------------------------------------------------------------------

def run() -> Path:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    data = collect_signals()
    if not data["youtube"] and not data["tiktok"]:
        raise RuntimeError("No trend data returned from either platform.")

    digest = _digest(data)
    print(f"\nGenerating creative briefs ({_MODEL})...")
    briefs = generate_briefs(digest, data["feedback"])

    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    header = (
        "=" * 70 + "\nSPEED WALLET — TREND-TO-ACTION PIPELINE (YouTube + TikTok)\n"
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
        f"{len(data['youtube'])} YouTube + {len(data['tiktok'])} TikTok items · "
        f"{len(_CATEGORIES)} categories\n" + "=" * 70 + "\n\n"
        "FEEDBACK LOOP (week-over-week)\n" + "-" * 32 + "\n" + data["feedback"] + "\n\n\n"
        "ACTIONABLE CREATIVE BRIEFS (ranked)\n" + "-" * 32 + "\n\n"
    )
    out = _DOCS / f"trend_pipeline_{stamp}.txt"
    out.write_text(header + briefs + "\n\n\n--- RAW TREND SIGNALS ---\n\n" + digest,
                   encoding="utf-8")

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
