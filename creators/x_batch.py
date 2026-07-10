"""Real X (Twitter)-only creator discovery batch across Speed's three segments.

Mirrors creators/youtube_batch.py: searches per-segment terms (via the Apify X
scraper), applies the follower floor (in the fetcher), scores, de-dupes, excludes
competitor/brand + low-engagement media accounts, and saves to Supabase — so X
creators enter the pipeline exactly like YouTube/TikTok ones, no special-casing.

Differences from youtube_batch, both intrinsic to the platform:
  * No unit quota to track — the X scraper is Apify pay-per-use (~$0.30/1k tweets).
    All of a segment's terms go in ONE actor run (searchTerms list) to avoid
    per-term actor-startup overhead.
  * DRY RUN by default (discovers + scores but does not write). Pass --write to
    persist — a guard, because a topic sweep can insert many rows into the live DB.

Run from repo root:  python creators/x_batch.py [--write]
"""

import os
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from apify_client.errors import ApifyApiError

from creators import database
from creators.scorer import CreatorScorer
from creators.apify_x import XCreatorFetcher
from creators.youtube_batch import (
    SEGMENT_SEARCHES,
    is_excluded_brand,
    is_low_engagement_media,
    find_name_subset_duplicates,
)

load_dotenv()

RESULTS_PER_QUERY = 20   # tweets per search term (author dedupe happens after)


def run_x_batch(write: bool, segments: list[str] | None = None) -> None:
    key = os.getenv("APIFY_API_KEY")
    if not key:
        sys.exit("APIFY_API_KEY not set in .env")
    fetcher = XCreatorFetcher(key)
    scorer = CreatorScorer()

    by_key: dict[tuple, dict] = {}   # (name, platform) -> creator dict (dedupe)
    seg_items = SEGMENT_SEARCHES.items() if not segments else \
        [(s, SEGMENT_SEARCHES[s]) for s in segments]

    print(f"{'WRITE' if write else 'DRY RUN'} · discovering X creators "
          f"(follower floor applied in fetcher)...\n")
    for segment, terms in seg_items:
        try:
            # One actor run for the whole segment's term list.
            got = fetcher.search(terms, results_per_query=RESULTS_PER_QUERY)
        except (RuntimeError, ApifyApiError) as e:
            print(f"  !! X search failed for segment '{segment}': {e}")
            continue
        new = 0
        for c in got:
            k = (c["name"], c["platform"])
            if k not in by_key:
                by_key[k] = c
                new += 1
        print(f"  [{segment}] {len(terms)} terms -> {len(got)} creators, {new} new unique")

    creators = list(by_key.values())
    print(f"\nTotal unique X creators (>= follower floor): {len(creators)}")

    # Drop competitor/brand accounts — companies, not individual creators.
    excluded = [c for c in creators if is_excluded_brand(c.get("name", ""))]
    if excluded:
        print(f"Excluded {len(excluded)} competitor/brand account(s): {[c['name'] for c in excluded]}")
        creators = [c for c in creators if not is_excluded_brand(c.get("name", ""))]

    # Drop low-engagement news/media/educational outlets (name pattern + low ER).
    media = [c for c in creators if is_low_engagement_media(c)]
    if media:
        print(f"Excluded {len(media)} low-engagement media/edu account(s): {[c['name'] for c in media]}")
        creators = [c for c in creators if not is_low_engagement_media(c)]

    if not creators:
        print("No creators left to save after exclusions.")
        return

    # Score, then drop near-duplicate names (keep the higher-scored).
    scored = [(c, scorer.score(c)) for c in creators]
    dupes = find_name_subset_duplicates(
        scored,
        name_fn=lambda cs: cs[0].get("name", ""),
        score_fn=lambda cs: cs[1]["composite_score"],
    )
    if dupes:
        dupe_ids = {id(cs) for cs in dupes}
        print(f"Skipping {len(dupes)} near-duplicate name(s): {[cs[0]['name'] for cs in dupes]}")
        scored = [cs for cs in scored if id(cs) not in dupe_ids]

    print(f"\n{len(scored)} X creators ready. Top by composite:")
    for c, s in sorted(scored, key=lambda cs: cs[1]["composite_score"], reverse=True)[:10]:
        print(f"  {s['composite_score']:>5.1f}  [{s['segment_tag']:<14}] {c['name']} "
              f"({c['followers']:,} followers, EQ={c['engagement_quality']})")

    if not write:
        print("\n(dry run — pass --write to save these X creators to Supabase.)")
        return

    saved = []
    for c, score in scored:
        rec = database.save_creator(c, score)
        saved.append((rec.get("name"), rec.get("platform")))
    print(f"\nSaved {len(saved)} X creators to Supabase.")
    comps = [s["composite_score"] for _, s in scored]
    print(f"composite: min={min(comps):.1f} max={max(comps):.1f} avg={mean(comps):.1f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    segs = None
    if "--segment" in args:
        segs = [args[args.index("--segment") + 1]]
    run_x_batch(write="--write" in args, segments=segs)
