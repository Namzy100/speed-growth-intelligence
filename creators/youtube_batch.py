"""Real YouTube-only creator discovery batch across Speed's three segments.

Searches multiple terms per segment, applies the subscriber floor (in the
fetcher), scores, de-dupes, saves to Supabase, then reads everything back and
reports segment breakdown + score distribution. Tracks YouTube quota.

Run from repo root:  python creators/youtube_batch.py
"""

import os
import re
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from creators import database
from creators.scorer import CreatorScorer
from creators.youtube import QuotaExceededError, YouTubeCreatorFetcher

load_dotenv()

# Per-segment search terms. As of the influencer-pivot, remittance and
# crypto-curious target PERSONAL creators (expat/diaspora vloggers, lifestyle
# crypto stories) rather than finance/news channels; iGaming keeps the crypto
# gambling terms plus reaction/streamer angles. The educational/news crypto
# terms (e.g. "lightning network explained", "bitcoin news daily") were removed
# because they surfaced media outlets like "Crypto Daily News".
SEGMENT_SEARCHES: dict[str, list[str]] = {
    "remittance": [
        "expat life vlog",
        "immigrant life US",
        "life in Germany as Nigerian",
        "Brazilian in Portugal vlog",
        "sending money home",
        "diaspora vlogger",
        "Latino life in USA",
        "African in UK vlog",
        "Indian in USA lifestyle",
    ],
    "iGaming": [
        "crypto casino",
        "crypto gambling",
        "bitcoin casino",
        "online betting cryptocurrency",
        "crypto sports betting",
        "sports betting crypto",
        "bitcoin gambling",
        "play to earn crypto",
        "crypto poker",
        # Influencer / reaction angles
        "sports betting wins",
        "casino streamer",
        "gambling wins reaction",
        "betting strategy",
    ],
    "crypto-curious": [
        "crypto lifestyle",
        "made money with bitcoin story",
        "bitcoin changed my life",
        "crypto millionaire story",
        "financial freedom bitcoin",
        "quit my job crypto",
    ],
    # EU remittance focus — German-language + UK diaspora corridors, for the
    # top-3 EU market push (Germany, UK). Run as a targeted group.
    "remittance_eu": [
        # German-language
        "Geld überweisen",
        "Geld ins Ausland senden",
        "Remittance Deutschland",
        "Bitcoin Deutschland",
        "Krypto Anfänger",
        # UK / English diaspora corridors
        "send money Nigeria UK",
        "send money India UK",
        "remittance UK",
        "diaspora money transfer",
        "African remittance UK",
    ],
}

MAX_RESULTS_PER_SEARCH = 10   # channels requested per search term

# Competitors and brand accounts — these are companies, not individual creators,
# so they are never partnership targets. Any channel whose name contains one of
# these (case-insensitive) is skipped before saving.
EXCLUDED_BRANDS = [
    # Fintech / crypto competitors
    "Western Union", "Remitly", "Wise", "PayPal", "MoneyGram",
    "Coinbase", "Cash App", "Crypto.com", "Kraken", "Robinhood",
    # Media / news outlets — not individual creator partners
    "CNBC", "BBC", "Forbes", "Bloomberg", "Reuters", "CNN",
    "Fox Business", "Wall Street Journal", "Financial Times",
    # Money-transfer companies + LatAm news outlets that slipped through
    "Ria Money Transfer", "Latinus", "Periódico Excélsior",
]


def is_excluded_brand(name: str) -> bool:
    """True if `name` contains an excluded competitor/brand token (case-insensitive)."""
    low = (name or "").lower()
    return any(b.lower() in low for b in EXCLUDED_BRANDS)


# News/media/educational name patterns. Unlike EXCLUDED_BRANDS these are only
# excluded when the channel ALSO has low engagement (<1%), so legitimate
# personal creators with these words in their name aren't caught.
_MEDIA_PATTERN_SUBSTR = ("daily news", "news today")
_MEDIA_PATTERN_WORDS = ("academy", "university", "school", "institute",
                        "official", "tv", "channel")
_MEDIA_ENGAGEMENT_FLOOR = 0.01


def is_low_engagement_media(creator: dict) -> bool:
    """True for likely news/media/educational outlets with weak engagement.

    Excludes a channel whose NAME matches a media/education pattern AND whose
    engagement_rate is below 1% — so a real creator with e.g. 'TV' in their name
    but strong engagement is kept.
    """
    er = float(creator.get("engagement_rate", 0) or 0)
    if er >= _MEDIA_ENGAGEMENT_FLOOR:
        return False
    n = (creator.get("name", "") or "").lower()
    if any(s in n for s in _MEDIA_PATTERN_SUBSTR):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in _MEDIA_PATTERN_WORDS)


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for name comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def find_name_subset_duplicates(items, name_fn, score_fn) -> list:
    """Return the items to REMOVE as near-duplicates.

    Two items are near-duplicates when one's normalized name is a substring of
    the other's (e.g. "Remitly" vs "Remitly, Inc."). The lower-scored item is
    marked for removal; the higher-scored one is kept.

    Args:
        items:    list of items (creator dicts, (creator, score) tuples, DB rows…)
        name_fn:  callable(item) -> name string
        score_fn: callable(item) -> numeric score
    """
    norm = [(_normalize_name(name_fn(it)), float(score_fn(it) or 0), it) for it in items]
    removed = [False] * len(norm)
    to_remove = []
    for i in range(len(norm)):
        if removed[i] or not norm[i][0]:
            continue
        for j in range(i + 1, len(norm)):
            if removed[j] or not norm[j][0]:
                continue
            a, b = norm[i][0], norm[j][0]
            if a != b and not (a in b or b in a):
                continue
            # Same name or one contains the other → drop the lower-scored.
            if norm[i][1] >= norm[j][1]:
                to_remove.append(norm[j][2])
                removed[j] = True
            else:
                to_remove.append(norm[i][2])
                removed[i] = True
                break
    return to_remove


def verify_column() -> None:
    sb = database._client()
    try:
        sb.table("creators").select("deposit_relevance_score").limit(1).execute()
        print("OK: deposit_relevance_score column present.\n")
    except Exception as e:
        sys.exit(f"ABORT: deposit_relevance_score column still missing -> {e}")


def run_batch() -> None:
    verify_column()
    key = os.getenv("YOUTUBE_API_KEY")
    fetcher = YouTubeCreatorFetcher(key)

    # Instrument real quota consumption by shadowing the bound _get method.
    calls = {"search": 0, "channels": 0, "playlistItems": 0, "videos": 0}
    orig_get = fetcher._get

    def counting_get(endpoint, params):
        calls[endpoint] = calls.get(endpoint, 0) + 1
        return orig_get(endpoint, params)

    fetcher._get = counting_get

    scorer = CreatorScorer()
    by_key: dict[tuple, dict] = {}   # (name, platform) -> creator dict (dedupe)
    quota_hit = False

    print("Fetching across segments (subscriber floor applied in fetcher)...")
    for segment, terms in SEGMENT_SEARCHES.items():
        seg_count = 0
        for term in terms:
            try:
                got = fetcher.search(term, max_results=MAX_RESULTS_PER_SEARCH)
            except QuotaExceededError as e:
                print(f"  !! QUOTA EXCEEDED during '{term}': {e}")
                quota_hit = True
                break
            new = 0
            for c in got:
                k = (c["name"], c["platform"])
                if k not in by_key:
                    by_key[k] = c
                    new += 1
            seg_count += new
            print(f"  [{segment}] '{term}' -> {len(got)} returned, {new} new")
        print(f"  == {segment}: {seg_count} new unique creators\n")
        if quota_hit:
            break

    creators = list(by_key.values())
    print(f"Total unique creators (>= floor): {len(creators)}")

    # Drop competitor/brand channels — companies, not individual creators.
    excluded = [c for c in creators if is_excluded_brand(c.get("name", ""))]
    if excluded:
        print(f"Excluded {len(excluded)} competitor/brand channel(s): "
              f"{[c['name'] for c in excluded]}")
        creators = [c for c in creators if not is_excluded_brand(c.get("name", ""))]

    # Drop low-engagement news/media/educational outlets (name pattern + ER<1%).
    media = [c for c in creators if is_low_engagement_media(c)]
    if media:
        print(f"Excluded {len(media)} low-engagement media/edu channel(s): "
              f"{[c['name'] for c in media]}")
        creators = [c for c in creators if not is_low_engagement_media(c)]

    if not creators:
        sys.exit("No creators left to save after exclusions.")

    # Score, then drop near-duplicate names (keep the higher-scored).
    scored = [(c, scorer.score(c)) for c in creators]
    dupes = find_name_subset_duplicates(
        scored,
        name_fn=lambda cs: cs[0].get("name", ""),
        score_fn=lambda cs: cs[1]["composite_score"],
    )
    if dupes:
        dupe_ids = {id(cs) for cs in dupes}
        print(f"Skipping {len(dupes)} near-duplicate name(s): "
              f"{[cs[0]['name'] for cs in dupes]}")
        scored = [cs for cs in scored if id(cs) not in dupe_ids]

    # Save
    saved = []
    for c, score in scored:
        rec = database.save_creator(c, score)
        saved.append((rec.get("name"), rec.get("platform")))
    print(f"Saved {len(saved)} creators to Supabase.\n")

    units = (100 * calls["search"] + calls["channels"]
             + calls["playlistItems"] + calls["videos"])
    report_readback(saved, calls, units, quota_hit)


def report_readback(saved, calls, units, quota_hit) -> None:
    print("=" * 70)
    print("READ-BACK FROM SUPABASE")
    print("=" * 70)
    rows = database.get_all_creators()
    saved_set = set(saved)
    mine = [r for r in rows if (r["name"], r["platform"]) in saved_set]

    print(f"Total creators saved (read back): {len(mine)}\n")

    # Segment breakdown
    seg_counts: dict[str, int] = {}
    for r in mine:
        seg_counts[r["segment_tag"]] = seg_counts.get(r["segment_tag"], 0) + 1
    print("Segment breakdown:")
    for seg, n in sorted(seg_counts.items(), key=lambda x: -x[1]):
        print(f"  {seg:<16} {n}")

    # Score distribution
    scores = [r["composite_score"] for r in mine]
    print(f"\nComposite score distribution:")
    print(f"  min={min(scores):.1f}  max={max(scores):.1f}  avg={mean(scores):.1f}")

    # deposit_relevance_score persisted?
    drs = [r.get("deposit_relevance_score") for r in mine]
    populated = sum(1 for v in drs if v is not None)
    nonzero = sum(1 for v in drs if v)
    print(f"\ndeposit_relevance_score: {populated}/{len(mine)} populated, "
          f"{nonzero} non-zero  (min={min(drs):.1f} max={max(drs):.1f})")

    # Top 10 for eyeballing
    print("\nTop 10 by composite:")
    for r in sorted(mine, key=lambda r: r["composite_score"], reverse=True)[:10]:
        print(f"  {r['composite_score']:>5.1f}  drs={r.get('deposit_relevance_score'):>4}  "
              f"[{r['segment_tag']:<14}] {r['name']}  ({r['followers']:,} subs)")

    # --- Anomaly flags ---
    print("\n" + "-" * 70)
    print("ANOMALY FLAGS")
    print("-" * 70)
    # Duplicates by name (across platform) — shouldn't happen YT-only but check
    from collections import Counter
    name_counts = Counter(r["name"] for r in mine)
    dups = {n: c for n, c in name_counts.items() if c > 1}
    print(f"  duplicate names: {dups if dups else 'none'}")
    # Floor violations
    under = [r["name"] for r in mine if r["followers"] < 5000]
    print(f"  below 5k floor (should be none): {under if under else 'none'}")
    # Score clustering
    rounded = Counter(round(s) for s in scores)
    clustered = {k: v for k, v in rounded.items() if v >= max(3, len(scores) // 4)}
    print(f"  composite clustering (>=25% at same int): {clustered if clustered else 'none'}")
    # 'general' tag rate (segment tagging misses)
    gen = seg_counts.get("general", 0)
    print(f"  tagged 'general' (segment miss): {gen}/{len(mine)}")

    print("\n" + "=" * 70)
    print("YOUTUBE QUOTA CONSUMED")
    print("=" * 70)
    print(f"  calls={calls}")
    print(f"  total units = {units} of 10,000/day")
    if quota_hit:
        print("  NOTE: daily quota was exhausted mid-run — batch is partial.")


if __name__ == "__main__":
    run_batch()
