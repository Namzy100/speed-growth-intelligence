"""Trend-to-action pipeline for Speed Wallet (TikTok + Instagram).

Weekly system that turns trending content into ready-to-brief ad creative:
  1. Pull trending TikTok videos (Apify search) + Instagram posts (hashtag scraper)
     for Speed's categories.
  2. Per category, extract signal: content length, opening-line patterns, hashtag
     clusters, engagement rate, estimated reach.
  3. Claude classifies trends (directly usable / adaptable / irrelevant) and, for
     the usable ones, writes a concrete creative brief (hook, 15s script outline,
     Speed segment, paid channel to test, estimated CPI improvement).
  4. Feedback loop: diff this week's signal against last week's snapshot —
     what gained traction, what died, what's emerging.

Output: docs/trend_pipeline_YYYY_MM_DD.txt  (+ a machine snapshot in
data/processed/ for next week's comparison).

Run from repo root:  python intelligence/trend_pipeline.py
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from anthropic import Anthropic
from apify_client import ApifyClient
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from creators.apify_tiktok import TikTokCreatorFetcher

_DOCS = _ROOT / "docs"
_STATE_DIR = _ROOT / "data" / "processed"
_MODEL = "claude-sonnet-4-6"

_CATEGORIES = ["bitcoin", "crypto", "remittance", "money transfer",
               "send money", "lightning network", "fintech"]
_IG_HASHTAGS = {
    "bitcoin": "bitcoin", "crypto": "crypto", "remittance": "remittance",
    "money transfer": "moneytransfer", "send money": "sendmoney",
    "lightning network": "lightningnetwork", "fintech": "fintech",
}
_IG_ACTOR = "apify/instagram-hashtag-scraper"
_RESULTS_PER_QUERY = 30
_PER_CATEGORY = 15

# Current paid benchmark used to frame CPI-improvement estimates for the briefs.
_BENCHMARK_CPI = 3.17  # Payday - Android - Broad+ (best Meta CPI)


# ------------------------------------------------------------------
# Fetch
# ------------------------------------------------------------------

def _fetch_tt(fetcher: TikTokCreatorFetcher, term: str) -> list[dict]:
    try:
        items = fetcher.search_videos([term], results_per_query=_RESULTS_PER_QUERY)
    except Exception as e:
        print(f"    TikTok '{term}' failed: {e}")
        return []
    vids = [it for it in items if int(it.get("playCount", 0) or 0) > 0]
    vids.sort(key=lambda it: int(it.get("playCount", 0) or 0), reverse=True)
    return vids[:_PER_CATEGORY]


def _fetch_ig(client: ApifyClient, hashtag: str) -> list[dict]:
    try:
        run = client.actor(_IG_ACTOR).call(
            run_input={"hashtags": [hashtag], "resultsType": "posts",
                       "resultsLimit": _RESULTS_PER_QUERY}
        )
        if not run or run.status not in ("SUCCEEDED", "TIMED-OUT"):
            return []
        items = list(client.dataset(run.default_dataset_id).iterate_items())
    except Exception as e:
        print(f"    Instagram #{hashtag} failed: {e}")
        return []

    def rank(it) -> int:
        return max(int(it.get("likesCount", 0) or 0), int(it.get("videoViewCount", 0) or 0))
    posts = [it for it in items if rank(it) > 0]
    posts.sort(key=rank, reverse=True)
    return posts[:_PER_CATEGORY]


# ------------------------------------------------------------------
# Feature extraction
# ------------------------------------------------------------------

def _opening(text: str) -> str:
    return " ".join(str(text or "").split()[:7])


def _tt_features(term: str, vids: list[dict]) -> dict:
    if not vids:
        return {"category": term, "platform": "TikTok", "count": 0}
    views = [int(v.get("playCount", 0) or 0) for v in vids]
    durs = [int((v.get("videoMeta", {}) or {}).get("duration", 0) or 0) for v in vids]
    ers = []
    tags = Counter()
    openings = []
    for v in vids:
        pc = int(v.get("playCount", 0) or 0)
        eng = (int(v.get("diggCount", 0) or 0) + int(v.get("commentCount", 0) or 0)
               + int(v.get("shareCount", 0) or 0))
        if pc:
            ers.append(eng / pc)
        for ht in v.get("hashtags", []) or []:
            name = ht.get("name", "") if isinstance(ht, dict) else str(ht)
            if name:
                tags[name.lower()] += 1
        openings.append(_opening(v.get("text", "")))
    return {
        "category": term, "platform": "TikTok", "count": len(vids),
        "max_reach": max(views), "median_reach": int(median(views)),
        "median_len_s": int(median([d for d in durs if d] or [0])),
        "avg_er": round(sum(ers) / len(ers), 4) if ers else 0.0,
        "top_hashtags": tags.most_common(12),
        "openings": [o for o in openings[:6] if o],
        "captions": [" ".join(str(v.get("text", "")).split())[:160] for v in vids[:6]],
    }


def _ig_features(term: str, posts: list[dict]) -> dict:
    if not posts:
        return {"category": term, "platform": "Instagram", "count": 0}
    likes = [int(p.get("likesCount", 0) or 0) for p in posts]
    ers = []
    tags = Counter()
    openings = []
    for p in posts:
        vv = int(p.get("videoViewCount", 0) or 0)
        lk = int(p.get("likesCount", 0) or 0)
        cm = int(p.get("commentsCount", 0) or 0)
        if vv:
            ers.append((lk + cm) / vv)
        for ht in p.get("hashtags", []) or []:
            name = ht if isinstance(ht, str) else ht.get("name", "")
            if name:
                tags[str(name).lower()] += 1
        openings.append(_opening(p.get("caption", "")))
    return {
        "category": term, "platform": "Instagram", "count": len(posts),
        "max_reach": max(likes), "median_reach": int(median(likes)),
        "median_len_s": 0,
        "avg_er": round(sum(ers) / len(ers), 4) if ers else 0.0,
        "top_hashtags": tags.most_common(12),
        "openings": [o for o in openings[:6] if o],
        "captions": [" ".join(str(p.get("caption", "")).split())[:160] for p in posts[:6]],
    }


def _digest(f: dict) -> str:
    if not f.get("count"):
        return f"{f['category'].upper()} ({f['platform']}): no data"
    tags = ", ".join(f"#{t}({n})" for t, n in f["top_hashtags"])
    lines = [
        f"{f['category'].upper()} ({f['platform']}) — {f['count']} top items · "
        f"reach max {f['max_reach']:,} / median {f['median_reach']:,} · "
        f"median length {f['median_len_s']}s · avg engagement {f['avg_er']:.1%}",
        f"  hashtag clusters: {tags}",
        f"  opening-line patterns: {' | '.join(f['openings'][:5])}",
        "  top captions:",
    ]
    lines += [f"    - {c}" for c in f["captions"][:5]]
    return "\n".join(lines)


# ------------------------------------------------------------------
# Feedback loop (week-over-week)
# ------------------------------------------------------------------

def _snapshot(features: list[dict]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": {
            f"{f['category']}|{f['platform']}": {
                "median_reach": f.get("median_reach", 0),
                "avg_er": f.get("avg_er", 0.0),
                "hashtags": dict(f.get("top_hashtags", [])),
            }
            for f in features if f.get("count")
        },
    }


def _prior_snapshot() -> dict | None:
    files = sorted(_STATE_DIR.glob("trend_pipeline_state_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _feedback(current: dict, prior: dict | None) -> str:
    if not prior:
        return ("No prior snapshot found — this is the baseline week. "
                "Next week's run will show what gained traction, died, or emerged.")
    lines = []
    for key, cur in current["categories"].items():
        old = prior["categories"].get(key)
        if not old:
            lines.append(f"  NEW segment tracked: {key}")
            continue
        # reach delta
        o_reach = old.get("median_reach", 0) or 0
        delta = ((cur["median_reach"] - o_reach) / o_reach * 100) if o_reach else 0
        arrow = "▲" if delta > 5 else "▼" if delta < -5 else "▬"
        cur_tags, old_tags = set(cur["hashtags"]), set(old.get("hashtags", {}))
        emerging = sorted(cur_tags - old_tags)[:6]
        died = sorted(old_tags - cur_tags)[:6]
        lines.append(
            f"  {key}: reach {arrow} {delta:+.0f}%  "
            f"| emerging: {', '.join('#'+t for t in emerging) or '—'}  "
            f"| faded: {', '.join('#'+t for t in died) or '—'}"
        )
    prior_date = prior.get("generated_at", "")[:10]
    return f"Compared against snapshot from {prior_date}:\n" + "\n".join(lines)


# ------------------------------------------------------------------
# Claude briefs
# ------------------------------------------------------------------

def generate_briefs(digests: str, feedback: str) -> str:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (
        "You are a growth-creative strategist for Speed Wallet, a Bitcoin Lightning "
        "payments app. Segments: remittance (zero fees), iGaming (instant deposits), "
        "crypto-curious (simplicity). Best current paid CPI benchmark is "
        f"${_BENCHMARK_CPI:.2f} (Meta, Payday Android Broad+).\n\n"
        "Below are this week's trending TikTok + Instagram signals for Speed's "
        "categories, plus a week-over-week feedback section.\n\n"
        "TASK:\n"
        "1. Identify the 5-7 strongest trends across the signals. Classify EACH as: "
        "(a) DIRECTLY USABLE for Speed ads, (b) ADAPTABLE with a Speed angle, or "
        "(c) IRRELEVANT (name it and one-line why, then skip).\n"
        "2. For every (a) and (b) trend, write a RANKED, hand-to-creative brief with "
        "EXACTLY these fields:\n"
        "   - Trend & evidence (platform, hashtags, reach/engagement)\n"
        "   - Classification (a or b)\n"
        "   - Exact hook to use (the literal opening line)\n"
        "   - 15-second script outline (3-4 beats)\n"
        "   - Speed segment targeted\n"
        "   - Paid channel to test first (Meta / TikTok / IG) and why\n"
        f"   - Estimated CPI impact vs the ${_BENCHMARK_CPI:.2f} benchmark (a range + one-line rationale)\n"
        "3. Open with a 2-line 'MOMENTUM' summary drawn from the feedback section "
        "(what's rising/emerging worth acting on now).\n\n"
        "Rank briefs by actionability. Be concrete — no fluff, no markdown headers. "
        "Plain text a creative team can execute tomorrow.\n\n"
        "--- WEEK-OVER-WEEK FEEDBACK ---\n" + feedback +
        "\n\n--- THIS WEEK'S TREND SIGNALS ---\n" + digests
    )
    resp = client.messages.create(
        model=_MODEL, max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

def run() -> Path:
    for key in ("APIFY_API_KEY", "ANTHROPIC_API_KEY"):
        if not os.getenv(key):
            raise EnvironmentError(f"{key} must be set in .env")

    fetcher = TikTokCreatorFetcher(os.getenv("APIFY_API_KEY"))
    ig_client = ApifyClient(os.getenv("APIFY_API_KEY"))

    features = []
    for term in _CATEGORIES:
        print(f"Scanning '{term}'...")
        tt = _fetch_tt(fetcher, term)
        print(f"  TikTok: {len(tt)} videos")
        features.append(_tt_features(term, tt))
        ig_tag = _IG_HASHTAGS.get(term, term.replace(" ", ""))
        ig = _fetch_ig(ig_client, ig_tag)
        print(f"  Instagram #{ig_tag}: {len(ig)} posts")
        features.append(_ig_features(term, ig))

    digests = "\n\n".join(_digest(f) for f in features if f.get("count"))
    if not digests:
        raise RuntimeError("No trend data returned from either platform.")

    current = _snapshot(features)
    feedback = _feedback(current, _prior_snapshot())

    print(f"\nGenerating creative briefs ({_MODEL})...")
    briefs = generate_briefs(digests, feedback)

    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    header = (
        "=" * 70 + "\nSPEED WALLET — TREND-TO-ACTION PIPELINE\n"
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
        f"TikTok + Instagram · {len(_CATEGORIES)} categories\n" + "=" * 70 + "\n\n"
        "FEEDBACK LOOP (week-over-week)\n" + "-" * 32 + "\n" + feedback + "\n\n\n"
        "ACTIONABLE CREATIVE BRIEFS (ranked)\n" + "-" * 32 + "\n\n"
    )
    out = _DOCS / f"trend_pipeline_{stamp}.txt"
    out.write_text(header + briefs + "\n\n\n--- RAW TREND SIGNALS ---\n\n" + digests,
                   encoding="utf-8")

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    (_STATE_DIR / f"trend_pipeline_state_{stamp}.json").write_text(
        json.dumps(current, indent=2), encoding="utf-8")

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
