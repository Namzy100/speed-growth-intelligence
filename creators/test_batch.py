"""End-to-end 5-creator validation batch for the creators/ pipeline.

Fetches a small mix of YouTube (by search term) and TikTok (by handle) creators
across Speed's three segments, scores them, saves to Supabase, then reads the
rows back out and reports the real numbers — plus actual Apify $ and YouTube
quota consumed. Run from repo root:  python creators/test_batch.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from creators import database
from creators.apify_tiktok import TikTokCreatorFetcher
from creators.scorer import CreatorScorer
from creators.youtube import QuotaExceededError, YouTubeCreatorFetcher

load_dotenv()

# --- Inputs -----------------------------------------------------------------
# YouTube supports real search terms across the three segments.
YT_SEARCHES = [
    ("crypto for beginners buy bitcoin", 2),          # crypto-curious
    ("send money abroad international money transfer", 2),  # remittance
    ("crypto casino online betting", 2),              # iGaming
]
# TikTok scraper takes HANDLES, not search terms — pick segment-relevant brands.
TT_HANDLES = ["coinbase", "remitly"]   # crypto-curious, remittance
TT_RESULTS_PER_PROFILE = 8

TARGET_TOTAL = 5


def preflight() -> None:
    """Fail fast before spending any Apify money if config/DB isn't ready."""
    missing = [k for k in ("APIFY_API_KEY", "YOUTUBE_API_KEY",
                           "SUPABASE_URL", "SUPABASE_KEY") if not os.getenv(k)]
    if missing:
        sys.exit(f"PREFLIGHT FAIL: missing env keys: {missing}")
    if not database._table_exists():
        sys.exit("PREFLIGHT FAIL: Supabase 'creators' table not reachable. "
                 "Run database.CREATE_TABLE_SQL first. (No money spent.)")
    print("Preflight OK: keys present, Supabase 'creators' table reachable.\n")


def fetch_youtube(report: dict) -> list[dict]:
    """Fetch YouTube creators, instrumenting actual quota-unit consumption."""
    key = os.getenv("YOUTUBE_API_KEY")
    fetcher = YouTubeCreatorFetcher(key)

    # Count real API calls per endpoint by shadowing the bound _get method.
    calls = {"search": 0, "channels": 0, "playlistItems": 0, "videos": 0}
    orig_get = fetcher._get

    def counting_get(endpoint, params):
        calls[endpoint] = calls.get(endpoint, 0) + 1
        return orig_get(endpoint, params)

    fetcher._get = counting_get

    creators: list[dict] = []
    for query, n in YT_SEARCHES:
        try:
            got = fetcher.search(query, max_results=n)
            print(f"  YouTube '{query}' (max={n}) -> {len(got)} creators")
            creators.extend(got)
        except QuotaExceededError as e:
            print(f"  YouTube QUOTA EXCEEDED on '{query}': {e}")
            break

    # search.list = 100 units; channels/playlistItems/videos = 1 unit each.
    units = (100 * calls["search"] + calls["channels"]
             + calls["playlistItems"] + calls["videos"])
    report["youtube_calls"] = calls
    report["youtube_units"] = units
    return creators


def fetch_tiktok(report: dict) -> list[dict]:
    """Fetch TikTok creators by handle, capturing actual Apify $ usage."""
    key = os.getenv("APIFY_API_KEY")
    fetcher = TikTokCreatorFetcher(key)

    # Re-run the actor via the client so we can read the run's usage stats.
    # .call() returns a pydantic Run model (attribute access, not dict).
    run = fetcher._client.actor("clockworks/tiktok-profile-scraper").call(
        run_input={"profiles": TT_HANDLES, "resultsPerPage": TT_RESULTS_PER_PROFILE}
    )
    if not run or run.status != "SUCCEEDED":
        status = run.status if run else "NO_RUN"
        print(f"  TikTok actor status: {status} — no creators")
        report["apify_usd"] = (run.usage_total_usd or 0.0) if run else 0.0
        report["apify_items"] = 0
        return []

    items = list(fetcher._client.dataset(run.default_dataset_id).iterate_items())
    report["apify_usd"] = run.usage_total_usd or 0.0
    report["apify_items"] = len(items)
    report["apify_run_id"] = run.id

    # Reuse the class's own grouping/build logic on the raw items.
    from collections import defaultdict
    by_user: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        u = it.get("authorMeta", {}).get("name", "")
        if u:
            by_user[u].append(it)

    creators = []
    for vids in by_user.values():
        c = fetcher._build_creator(vids)
        if c:
            creators.append(c)
    print(f"  TikTok handles {TT_HANDLES} -> {len(creators)} creators "
          f"from {len(items)} video rows")
    return creators


def select_diverse(creators: list[dict], scorer: CreatorScorer, n: int) -> list[dict]:
    """Dedupe by (name, platform); pick n favoring segment diversity then score."""
    seen, unique = set(), []
    for c in creators:
        k = (c["name"], c["platform"])
        if k not in seen:
            seen.add(k)
            unique.append(c)

    scored = [(c, scorer.score(c)) for c in unique]
    scored.sort(key=lambda cs: cs[1]["composite_score"], reverse=True)

    picked, used_segments = [], set()
    # First pass: one per distinct segment.
    for c, s in scored:
        if len(picked) >= n:
            break
        seg = s["segment_tag"]
        if seg not in used_segments:
            picked.append((c, s))
            used_segments.add(seg)
    # Second pass: fill remaining slots by score.
    for c, s in scored:
        if len(picked) >= n:
            break
        if (c, s) not in picked:
            picked.append((c, s))
    return picked


def main() -> None:
    preflight()
    report: dict = {}
    scorer = CreatorScorer()

    print("Fetching creators...")
    yt = fetch_youtube(report)
    tt = fetch_tiktok(report)
    all_fetched = yt + tt
    print(f"\nFetched {len(all_fetched)} total ({len(yt)} YouTube + {len(tt)} TikTok)")

    if not all_fetched:
        sys.exit("No creators fetched — nothing to score/save.")

    picked = select_diverse(all_fetched, scorer, TARGET_TOTAL)
    print(f"Selected {len(picked)} for save (target {TARGET_TOTAL}).\n")

    saved_keys = []
    for creator, score in picked:
        rec = database.save_creator(creator, score)
        saved_keys.append((rec.get("name"), rec.get("platform")))
        print(f"  saved: {rec.get('name')} ({rec.get('platform')}) "
              f"id={rec.get('id')}")

    # --- Read back from Supabase (do NOT trust the save return values) ------
    print("\n" + "=" * 70)
    print("READ-BACK FROM SUPABASE (get_all_creators)")
    print("=" * 70)
    rows = database.get_all_creators()
    saved_set = set(saved_keys)
    for r in rows:
        if (r["name"], r["platform"]) not in saved_set:
            continue
        print(f"\n{r['name']}  [{r['platform']}]  segment={r['segment_tag']}")
        print(f"  followers={r['followers']:,}  ER={r['engagement_rate']}  "
              f"crypto%={r['crypto_content_pct']}  fintech%={r['fintech_content_pct']}")
        print(f"  COMPOSITE = {r['composite_score']} / 100")
        print(f"    audience_fit          = {r['audience_fit']}")
        print(f"    engagement_quality    = {r['engagement_quality_score']}")
        print(f"    content_alignment     = {r['content_alignment']}")
        print(f"    acquisition_potential = {r['acquisition_potential']}")
        print(f"    sponsorship_score     = {r['sponsorship_score']}")

    # --- Cost / quota report ------------------------------------------------
    print("\n" + "=" * 70)
    print("RESOURCE CONSUMPTION (this run)")
    print("=" * 70)
    print(f"  Apify (real $): ${report.get('apify_usd', 0.0):.4f}  "
          f"({report.get('apify_items', 0)} video rows scraped)")
    print(f"  YouTube quota : {report.get('youtube_units', 0)} units "
          f"of 10,000/day  calls={report.get('youtube_calls')}")


if __name__ == "__main__":
    main()
