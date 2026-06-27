"""US TikTok + Instagram trend scanner for Speed Wallet content/creative direction.

Scans trending content across crypto, bitcoin, remittance, and money-transfer:
  - TikTok via Apify clockworks/tiktok-scraper (apify_tiktok.search_videos), top
    videos by view count.
  - Instagram via Apify apify/instagram-hashtag-scraper, top posts by likes.
Extracts common hashtags/captions/formats/hooks per platform, and uses Claude
(claude-sonnet-4-6) to identify 3-5 actionable content trends Speed can tap for
creator briefs or ad-creative direction. Saves to docs/trend_report_<date>.txt
with separate TikTok and Instagram sections.

CAVEATS (noted in output): "US" is approximated via English terms (neither
platform's search is reliably geo-filtered). Instagram hashtags can't contain
spaces, so "money transfer" is scanned as #moneytransfer.

Run from repo root:  python intelligence/trend_scanner.py
"""

import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from anthropic import Anthropic

from creators.apify_tiktok import TikTokCreatorFetcher

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"
_CATEGORIES = ["crypto", "bitcoin", "remittance", "money transfer"]
_PER_CATEGORY = 20          # top N items to keep per category
_RESULTS_PER_QUERY = 35     # pull more than N so we can rank down to the top N

_IG_ACTOR = "apify/instagram-hashtag-scraper"
# Instagram hashtags can't contain spaces.
_IG_HASHTAGS = {"crypto": "crypto", "bitcoin": "bitcoin",
                "remittance": "remittance", "money transfer": "moneytransfer"}


# ------------------------------------------------------------------
# TikTok
# ------------------------------------------------------------------

def _fetch_tt(fetcher: TikTokCreatorFetcher, term: str) -> list[dict]:
    items = fetcher.search_videos([term], results_per_query=_RESULTS_PER_QUERY)
    vids = [it for it in items if int(it.get("playCount", 0) or 0) > 0]
    vids.sort(key=lambda it: int(it.get("playCount", 0) or 0), reverse=True)
    return vids[:_PER_CATEGORY]


def _tt_digest(term: str, vids: list[dict]) -> str:
    if not vids:
        return f"{term.upper()} (TikTok): (no videos returned)"
    views = [int(v.get("playCount", 0) or 0) for v in vids]
    tags = Counter()
    for v in vids:
        for ht in v.get("hashtags", []) or []:
            name = ht.get("name", "") if isinstance(ht, dict) else str(ht)
            if name:
                tags[name.lower()] += 1
    top_tags = ", ".join(f"#{t}({n})" for t, n in tags.most_common(12))
    lines = [
        f"{term.upper()} (TikTok) — top {len(vids)} videos by views "
        f"(max {max(views):,}, median {int(median(views)):,} views)",
        f"  top hashtags: {top_tags or '(none)'}",
        "  top captions by views:",
    ]
    for v in vids[:6]:
        cap = " ".join(str(v.get("text", "")).split())[:160]
        dur = (v.get("videoMeta", {}) or {}).get("duration", "?")
        lines.append(f"    [{int(v.get('playCount', 0)):>10,} views, {dur}s] {cap}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Instagram
# ------------------------------------------------------------------

def _fetch_ig(client: ApifyClient, hashtag: str) -> list[dict]:
    """Top Instagram posts for a hashtag, ranked by likes (then video views)."""
    run = client.actor(_IG_ACTOR).call(
        run_input={"hashtags": [hashtag], "resultsType": "posts",
                   "resultsLimit": _RESULTS_PER_QUERY}
    )
    if not run or run.status not in ("SUCCEEDED", "TIMED-OUT"):
        return []
    items = list(client.dataset(run.default_dataset_id).iterate_items())

    def rank(it) -> int:
        return max(int(it.get("likesCount", 0) or 0), int(it.get("videoViewCount", 0) or 0))

    posts = [it for it in items if rank(it) > 0]
    posts.sort(key=rank, reverse=True)
    return posts[:_PER_CATEGORY]


def _ig_digest(category: str, posts: list[dict]) -> str:
    if not posts:
        return f"{category.upper()} (Instagram): (no posts returned)"
    likes = [int(p.get("likesCount", 0) or 0) for p in posts]
    tags = Counter()
    for p in posts:
        for ht in p.get("hashtags", []) or []:
            name = ht if isinstance(ht, str) else ht.get("name", "")
            if name:
                tags[str(name).lower()] += 1
    top_tags = ", ".join(f"#{t}({n})" for t, n in tags.most_common(12))
    lines = [
        f"{category.upper()} (Instagram) — top {len(posts)} posts by likes "
        f"(max {max(likes):,}, median {int(median(likes)):,} likes)",
        f"  top hashtags: {top_tags or '(none)'}",
        "  top captions by likes:",
    ]
    for p in posts[:6]:
        cap = " ".join(str(p.get("caption", "")).split())[:160]
        typ = p.get("type", "?")
        vv = int(p.get("videoViewCount", 0) or 0)
        vstr = f", {vv:,} views" if vv else ""
        lines.append(f"    [{int(p.get('likesCount', 0)):>9,} likes{vstr}, {typ}] {cap}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Claude report
# ------------------------------------------------------------------

_PROMPT = """\
You are a content strategist for Speed Wallet, a Bitcoin Lightning payments app \
(segments: remittance/zero-fees, iGaming/instant-deposits, crypto-curious/\
simplicity). Below is trending data from BOTH TikTok (top videos by views) and \
Instagram (top posts by likes) across four categories. "US" is approximated via \
English terms — not strictly geo-filtered.

From the hashtags, captions, engagement, and formats, identify 3 to 5 ACTIONABLE \
content trends Speed could tap for creator briefs or ad-creative direction. Write \
in clean PLAIN TEXT (no markdown, asterisks, or bold markers — use dash/equals \
section headers). For EACH trend give:
  - a short trend name,
  - the evidence (which platform, hashtags/caption patterns/formats/engagement),
  - the format/hook (length, style, opening line),
  - how Speed should use it (which segment + a concrete creator-brief or ad idea).
Note any TikTok-vs-Instagram differences in what performs. End with a one-line \
note on which categories/platforms looked strongest vs thin. Keep under 650 words.

--- TIKTOK TREND DATA ---
{tiktok_data}
--- END TIKTOK ---

--- INSTAGRAM TREND DATA ---
{ig_data}
--- END INSTAGRAM ---"""


def generate_report(tiktok_data: str, ig_data: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": _PROMPT.format(
            tiktok_data=tiktok_data, ig_data=ig_data)}],
    )
    return resp.content[0].text


def save_report(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"trend_report_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def run() -> str:
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise EnvironmentError("APIFY_API_KEY must be set in .env")
    fetcher = TikTokCreatorFetcher(api_key)
    ig_client = ApifyClient(api_key)

    tt_digests, ig_digests = [], []
    for term in _CATEGORIES:
        print(f"TikTok: scanning '{term}'...")
        try:
            tt = _fetch_tt(fetcher, term)
        except (RuntimeError, ApifyApiError) as e:
            print(f"  TikTok failed: {e}"); tt = []
        print(f"  kept {len(tt)} TikTok videos")
        tt_digests.append(_tt_digest(term, tt))

        ig_tag = _IG_HASHTAGS[term]
        print(f"Instagram: scanning #{ig_tag}...")
        try:
            ig = _fetch_ig(ig_client, ig_tag)
        except (RuntimeError, ApifyApiError) as e:
            print(f"  Instagram failed: {e}"); ig = []
        print(f"  kept {len(ig)} Instagram posts")
        ig_digests.append(_ig_digest(term, ig))

    tiktok_data = "\n\n".join(tt_digests)
    ig_data = "\n\n".join(ig_digests)

    print(f"\nGenerating trend report ({_MODEL})...")
    report = generate_report(tiktok_data, ig_data)

    bar = "=" * 70
    full = (
        f"{bar}\nSPEED WALLET — US TIKTOK + INSTAGRAM TREND REPORT\n"
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"(US approximated via English terms; not strictly geo-filtered)\n{bar}\n\n"
        f"RAW SIGNAL — TIKTOK\n{'-' * 20}\n{tiktok_data}\n\n"
        f"RAW SIGNAL — INSTAGRAM\n{'-' * 22}\n{ig_data}\n\n"
        f"{bar}\nTRENDS\n{'-' * 6}\n{report}"
    )
    path = save_report(full)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print(full)
    return full


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
