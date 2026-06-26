"""US TikTok trend scanner for Speed Wallet content/creative direction.

Searches TikTok (via Apify's clockworks/tiktok-scraper) for trending content in
crypto, bitcoin, remittance, and money-transfer categories, takes the top 20
videos per category by view count, extracts the common hashtags/captions/formats,
and uses Claude (claude-sonnet-4-6) to identify 3-5 actionable content trends
Speed can tap for creator briefs or ad-creative direction.
Saves to docs/trend_report_<date>.txt.

SCOPE/CAVEATS: TikTok only (the title mentions Instagram, but that needs a
separate Apify IG actor — flagged, not implemented). TikTok search via Apify is
not reliably geo-filtered, so "US" is approximated via English category terms;
this is noted in the output.

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

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"
_SEARCH_ACTOR = "clockworks/tiktok-scraper"
_CATEGORIES = ["crypto", "bitcoin", "remittance", "money transfer"]
_PER_CATEGORY = 20          # top N videos to keep per category
_RESULTS_PER_QUERY = 35     # pull more than N so we can rank down to the top 20


def _fetch_category(client: ApifyClient, term: str) -> list[dict]:
    run = client.actor(_SEARCH_ACTOR).call(
        run_input={"searchQueries": [term], "resultsPerPage": _RESULTS_PER_QUERY}
    )
    if not run or run.status not in ("SUCCEEDED", "TIMED-OUT"):
        return []
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    vids = [it for it in items if int(it.get("playCount", 0) or 0) > 0]
    vids.sort(key=lambda it: int(it.get("playCount", 0) or 0), reverse=True)
    return vids[:_PER_CATEGORY]


def _digest(term: str, vids: list[dict]) -> str:
    if not vids:
        return f"{term.upper()}: (no videos returned)"
    views = [int(v.get("playCount", 0) or 0) for v in vids]
    tags = Counter()
    for v in vids:
        for ht in v.get("hashtags", []) or []:
            name = ht.get("name", "") if isinstance(ht, dict) else str(ht)
            if name:
                tags[name.lower()] += 1
    top_tags = ", ".join(f"#{t}({n})" for t, n in tags.most_common(12))

    lines = [
        f"{term.upper()} — top {len(vids)} videos by views "
        f"(max {max(views):,}, median {int(median(views)):,} views)",
        f"  top hashtags: {top_tags or '(none)'}",
        "  top captions by views:",
    ]
    for v in vids[:6]:
        cap = " ".join(str(v.get("text", "")).split())[:160]
        dur = (v.get("videoMeta", {}) or {}).get("duration", "?")
        lines.append(f"    [{int(v.get('playCount',0)):>10,} views, {dur}s] {cap}")
    return "\n".join(lines)


_PROMPT = """\
You are a content strategist for Speed Wallet, a Bitcoin Lightning payments app \
(segments: remittance/zero-fees, iGaming/instant-deposits, crypto-curious/\
simplicity). Below is trending TikTok data (top videos by view count) across four \
categories relevant to Speed. Note: this is English-term TikTok search (US \
approximated, not strictly geo-filtered).

From the hashtags, captions, view counts, and durations, identify 3 to 5 \
ACTIONABLE content trends Speed could tap for creator briefs or ad-creative \
direction. Write in clean PLAIN TEXT (no markdown, asterisks, or bold markers — \
use dash/equals section headers). For EACH trend give:
  - a short trend name,
  - the evidence (hashtags/caption patterns/formats/view signals it's based on),
  - the format/hook (length, style, opening line),
  - how Speed should use it (which segment + a concrete creator-brief or ad idea).
End with a one-line note on which categories looked strongest vs thin. Keep it \
under 600 words.

--- TIKTOK TREND DATA ---
{data}
--- END DATA ---"""


def generate_report(data: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": _PROMPT.format(data=data)}],
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
    client = ApifyClient(api_key)

    digests = []
    for term in _CATEGORIES:
        print(f"Scanning TikTok for '{term}'...")
        try:
            vids = _fetch_category(client, term)
        except (RuntimeError, ApifyApiError) as e:
            print(f"  failed: {e}")
            vids = []
        print(f"  kept top {len(vids)} videos")
        digests.append(_digest(term, vids))

    data = "\n\n".join(digests)
    print(f"\nGenerating trend report ({_MODEL})...")
    report = generate_report(data)

    bar = "=" * 70
    full = (f"{bar}\nSPEED WALLET — US TIKTOK TREND REPORT\n"
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"(TikTok only; US approximated via English search terms)\n{bar}\n\n"
            f"RAW SIGNAL\n{'-' * 10}\n{data}\n\n{bar}\nTRENDS\n{'-' * 6}\n{report}")
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
