"""TikTok creator discovery via Apify's tiktok-profile-scraper actor."""

import os
from collections import defaultdict
from typing import Optional

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from dotenv import load_dotenv

load_dotenv()

_ACTOR_ID = "clockworks/tiktok-profile-scraper"
# General TikTok scraper — supports topic/keyword search via searchQueries.
_SEARCH_ACTOR_ID = "clockworks/tiktok-scraper"

# Same keyword lists as youtube.py for consistent cross-platform estimation.
_CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
    "blockchain", "defi", "lightning", "satoshi", "altcoin", "web3",
    "nft", "hodl", "binance", "coinbase", "crypto trading", "crypto investing",
    "digital asset", "digital currency",
]
_FINTECH_KEYWORDS = [
    "fintech", "payments", "payment", "remittance", "money transfer",
    "mobile banking", "neobank", "neo-bank", "financial technology",
    "send money", "digital banking", "banking", "financial services",
    "money app", "wallet app", "paytech",
]

# Reject creators below this follower count — too small to score reliably and
# prone to artificially capped engagement ratios. Mirrors youtube.MIN_SUBSCRIBERS.
MIN_FOLLOWERS = 5_000

_CRYPTO_SAT = 6   # hits required to reach the cap
_FINTECH_SAT = 5
_CRYPTO_CAP = 0.80
_FINTECH_CAP = 0.60

_SCORER_TAGS = {
    "remittance", "diaspora", "expat", "expats", "migrant", "migrants",
    "money transfer", "send money", "forex", "wire transfer", "immigrant",
    "immigrants", "overseas",
    "igaming", "gambling", "casino", "betting", "poker", "slots", "esports",
    "sports betting", "fantasy sports", "online gambling", "sportsbook",
    "crypto", "bitcoin", "ethereum", "blockchain", "defi", "web3", "nft",
    "cryptocurrency", "altcoin", "trading", "investing", "finance",
    "personal finance", "fintech", "payments", "lightning", "btc", "eth",
    "satoshi", "hodl",
}


class TikTokCreatorFetcher:
    """Fetches TikTok creator profiles via Apify and formats them for CreatorScorer.

    The actor returns one dataset row per video, with profile-level data
    embedded in each row's `authorMeta` field. This class groups by username,
    aggregates video stats, and derives the engagement/content signals that
    CreatorScorer expects.

    Pricing: ~$0.004 per video result. results_per_profile=10 costs ~$0.04/creator.
    """

    def __init__(self, api_key: str) -> None:
        self._client = ApifyClient(api_key)

    def fetch(self, usernames: list[str], results_per_profile: int = 10) -> list[dict]:
        """Fetch profiles for the given TikTok usernames.

        Args:
            usernames: TikTok handles without the @ prefix.
            results_per_profile: Videos to sample per creator. More videos improve
                estimate accuracy but increase cost.

        Returns:
            List of creator dicts compatible with CreatorScorer.score().

        Raises:
            RuntimeError: If the Apify actor run fails or is aborted.
            ApifyApiError: For authentication or platform-level API errors.
        """
        if not usernames:
            return []

        run = self._client.actor(_ACTOR_ID).call(
            run_input={
                "profiles": usernames,
                "resultsPerPage": results_per_profile,
            }
        )

        if not run:
            raise RuntimeError("Apify actor run timed out with no result.")
        if run.status != "SUCCEEDED":
            raise RuntimeError(f"Apify actor run ended with status: {run.status}")

        items = list(self._client.dataset(run.default_dataset_id).iterate_items())
        return self._creators_from_items(items)

    def search(self, queries: list[str], results_per_query: int = 15) -> list[dict]:
        """Discover creators by TOPIC via TikTok search (not by handle).

        Uses the general tiktok-scraper actor's search mode. Each query returns
        matching videos; creators are grouped from the videos' authors, then
        built into CreatorScorer-ready dicts (deduped by username, >= follower
        floor).

        Args:
            queries: Search terms (e.g. "bitcoin", "send money", "remessa").
            results_per_query: Videos to pull per query — more = wider net + cost.

        Returns:
            List of creator dicts compatible with CreatorScorer.score().

        Raises:
            RuntimeError: If the Apify actor run fails or is aborted.
            ApifyApiError: For authentication or platform-level API errors.
        """
        return self._creators_from_items(self.search_videos(queries, results_per_query))

    def search_videos(self, queries: list[str], results_per_query: int = 15) -> list[dict]:
        """Topic search returning RAW video items (one row per video).

        Same actor as search() but WITHOUT grouping into creators — each item
        keeps its per-video signals (playCount, text/caption, hashtags,
        videoMeta), which trend analysis needs. search() builds on top of this.

        Returns:
            List of raw actor items (TikTok videos matching the queries).
        """
        if not queries:
            return []

        run = self._client.actor(_SEARCH_ACTOR_ID).call(
            run_input={
                "searchQueries": queries,
                "resultsPerPage": results_per_query,
            }
        )

        if not run:
            raise RuntimeError("Apify search actor run timed out with no result.")
        # TIMED-OUT is acceptable — the scraper paginates and we stop it early,
        # but the dataset still holds the items collected so far.
        if run.status not in ("SUCCEEDED", "TIMED-OUT"):
            raise RuntimeError(f"Apify search actor run ended with status: {run.status}")

        return list(self._client.dataset(run.default_dataset_id).iterate_items())

    # ------------------------------------------------------------------
    # Build helpers
    # ------------------------------------------------------------------

    def _creators_from_items(self, items: list[dict]) -> list[dict]:
        """Group video rows by author username and build per-creator dicts.

        Shared by fetch() (profiles) and search() (topics) — the actor returns
        one row per video with profile data in `authorMeta`.
        """
        by_username: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            username = item.get("authorMeta", {}).get("name", "")
            if username:
                by_username[username].append(item)

        creators = []
        for videos in by_username.values():
            try:
                creator = self._build_creator(videos)
                if creator:
                    creators.append(creator)
            except ApifyApiError:
                raise
            except Exception:
                continue
        return creators

    def _build_creator(self, videos: list[dict]) -> Optional[dict]:
        """Assemble a CreatorScorer-compatible dict from one creator's video rows."""
        if not videos:
            return None

        author = videos[0].get("authorMeta", {})
        name = (author.get("nickName") or author.get("name", "")).strip()
        if not name:
            return None

        followers = int(author.get("fans", 0) or 0)
        if followers < MIN_FOLLOWERS:
            return None
        bio = author.get("signature", "") or ""
        total_video_count = int(author.get("video", 0) or 0)

        play_counts = [int(v.get("playCount", 0) or 0) for v in videos]
        like_counts = [int(v.get("diggCount", 0) or 0) for v in videos]
        comment_counts = [int(v.get("commentCount", 0) or 0) for v in videos]

        avg_views = sum(play_counts) / len(play_counts) if play_counts else 0.0
        avg_likes = sum(like_counts) / len(like_counts) if like_counts else 0.0
        avg_comments = sum(comment_counts) / len(comment_counts) if comment_counts else 0.0

        # Extrapolate total brand deals from the sponsored-video fraction in our sample.
        # isSponsored flags paid promotions; isAd flags TikTok-boosted ads.
        sponsored_in_sample = sum(
            1 for v in videos if v.get("isSponsored") or v.get("isAd")
        )
        sponsored_fraction = sponsored_in_sample / len(videos)
        sponsorship_count = round(sponsored_fraction * total_video_count)

        # Gather all hashtag names from the sampled videos — much richer signal than bio alone.
        all_hashtags: list[str] = []
        for v in videos:
            for ht in v.get("hashtags", []):
                tag_name = ht.get("name", "") if isinstance(ht, dict) else str(ht)
                if tag_name:
                    all_hashtags.append(tag_name)

        captions = [v.get("text", "") or "" for v in videos]

        # Capped at 1.0 — TikTok's FYP can deliver far more views than the follower base,
        # which would otherwise inflate acquisition_potential in the scorer.
        engagement_rate = (
            min(round(avg_views / followers, 4), 1.0) if followers > 0 else 0.0
        )

        crypto_pct, fintech_pct = self._estimate_content_pcts(bio, captions, all_hashtags)
        niche_tags = self._derive_niche_tags(bio, all_hashtags)
        eq = self._engagement_quality(avg_views, avg_likes, avg_comments)

        return {
            "name": name,
            "platform": "TikTok",
            "followers": followers,
            "engagement_rate": engagement_rate,
            "engagement_quality": eq,
            "crypto_content_pct": crypto_pct,
            "fintech_content_pct": fintech_pct,
            "sponsorship_count": sponsorship_count,
            "niche_tags": niche_tags,
        }

    # ------------------------------------------------------------------
    # Estimation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _engagement_quality(avg_views: float, avg_likes: float, avg_comments: float) -> int:
        """Estimate 1–10 engagement quality from interaction-to-view ratio.

        TikTok benchmarks run higher than YouTube — healthy creators typically
        land in the 5–15% range due to the FYP amplification loop.
        """
        if avg_views <= 0:
            return 1
        rate = (avg_likes + avg_comments) / avg_views
        if rate >= 0.15:
            return 10
        if rate >= 0.10:
            return 9
        if rate >= 0.07:
            return 8
        if rate >= 0.05:
            return 7
        if rate >= 0.03:
            return 6
        if rate >= 0.015:
            return 5
        if rate >= 0.008:
            return 4
        if rate >= 0.003:
            return 3
        return 2

    @staticmethod
    def _estimate_content_pcts(
        bio: str, captions: list[str], hashtags: list[str]
    ) -> tuple[float, float]:
        """Estimate crypto and fintech content percentages.

        Uses bio + video captions + hashtags for a richer signal than bio alone.
        """
        text = " ".join([bio] + captions + hashtags).lower()

        crypto_hits = sum(1 for kw in _CRYPTO_KEYWORDS if kw in text)
        fintech_hits = sum(1 for kw in _FINTECH_KEYWORDS if kw in text)

        crypto_pct = round(min(crypto_hits / _CRYPTO_SAT, 1.0) * _CRYPTO_CAP, 2)
        fintech_pct = round(min(fintech_hits / _FINTECH_SAT, 1.0) * _FINTECH_CAP, 2)
        return crypto_pct, fintech_pct

    @staticmethod
    def _derive_niche_tags(bio: str, hashtags: list[str]) -> list[str]:
        """Derive niche tags from bio text and video hashtags."""
        text = bio.lower()
        tags = {tag for tag in _SCORER_TAGS if tag in text}

        # TikTok hashtags are an especially clean, creator-curated signal.
        for ht in hashtags:
            ht_lower = ht.lower().replace("-", " ")
            if ht_lower in _SCORER_TAGS:
                tags.add(ht_lower)

        return sorted(tags)


def fetch_tiktok_creators(usernames: list[str], results_per_profile: int = 10) -> list[dict]:
    """Convenience wrapper — loads APIFY_API_KEY from env and returns creator dicts."""
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise EnvironmentError("APIFY_API_KEY not set in .env")
    return TikTokCreatorFetcher(api_key).fetch(usernames, results_per_profile=results_per_profile)


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from creators.scorer import CreatorScorer

    # Three crypto-focused TikTok accounts (handles must exist; missing profiles are skipped)
    TEST_USERNAMES = ["coinbase", "binance", "strike"]

    print(f"Fetching TikTok profiles: {', '.join(TEST_USERNAMES)}...")
    print("(This runs an Apify actor — takes ~15–30s)\n")

    try:
        creators = fetch_tiktok_creators(TEST_USERNAMES, results_per_profile=10)
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
    except (RuntimeError, ApifyApiError) as e:
        print(f"Actor error: {e}")
        sys.exit(1)

    if not creators:
        print("No creators returned (profiles may be private or not found).")
        sys.exit(0)

    scorer = CreatorScorer()
    results = sorted(
        (scorer.score(c) for c in creators),
        key=lambda r: r["composite_score"],
        reverse=True,
    )

    print(f"Results ({len(creators)} profiles fetched):\n")
    for result in results:
        print(f"{'=' * 60}")
        print(f"Creator  : {result['name']} ({result['platform']})")
        print(f"Segment  : {result['segment_tag']}")
        print(f"Composite: {result['composite_score']} / 100")
        print("Breakdown:")
        for dim, val in result["scores"].items():
            bar = "█" * int(val / 20 * 20)
            print(f"  {dim:<26} {val:>4} / 20  {bar}")
        print(f"\n{result['reasoning']}\n")
