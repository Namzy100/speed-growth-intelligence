"""X (Twitter) creator discovery via Apify's apidojo/tweet-scraper actor.

Mirrors creators/apify_tiktok.py: same class shape, same fetch()/search()
interface, and the SAME return-dict shape that database.save_creator() and
CreatorScorer.score() consume — so X creators plug into the pipeline with no
special-casing.

Why Apify (not the official X API): there is no X/Twitter credential in .env, and
X's official read tiers are paid and expensive (Basic ~$200/mo). Apify is already
the paid path for TikTok, so X reuses it.

What's real vs excluded (verified against live actor output, 2026-07):
  * followers, bio/description, per-tweet likeCount/retweetCount/replyCount/
    viewCount → real. Drives followers, engagement_rate, engagement_quality,
    crypto/fintech %, niche tags, audience fit.
  * SPONSORSHIP: X exposes NO branded-content / sponsored / paid-partnership flag
    (unlike TikTok isSponsored/isAd and YouTube paidProductPlacementDetails). So
    sponsorship is NOT measured — sponsorship_data_available is set False and
    sponsorship_count 0, honestly (the scorer then excludes it and renormalises,
    same as everywhere else). It is not fabricated.
"""

import os
from collections import defaultdict
from typing import Optional

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from dotenv import load_dotenv

load_dotenv()

_ACTOR_ID = "apidojo/tweet-scraper"   # tweets w/ embedded author profile; handle + search modes

# Same keyword/tag vocab as youtube.py + apify_tiktok.py for consistent
# cross-platform estimation.
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

# Reject creators below this follower count — mirrors apify_tiktok.MIN_FOLLOWERS.
MIN_FOLLOWERS = 5_000

_CRYPTO_SAT = 6
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


class XCreatorFetcher:
    """Fetches X (Twitter) creator profiles via Apify and formats them for CreatorScorer.

    The actor returns one row per tweet, with profile-level data embedded in each
    row's `author` field. This class groups rows by author, aggregates tweet
    engagement, and derives the signals CreatorScorer expects.

    Pricing: apidojo/tweet-scraper is ~$0.30 per 1,000 tweets. results_per_profile
    tweets per creator, so ~$0.003–0.006/creator at 10–20 tweets.
    """

    def __init__(self, api_key: str) -> None:
        self._client = ApifyClient(api_key)

    def fetch(self, usernames: list[str], results_per_profile: int = 15) -> list[dict]:
        """Fetch recent tweets for the given X handles and build creator dicts.

        Args:
            usernames: X handles WITHOUT the leading '@'.
            results_per_profile: Tweets to sample per handle (engagement basis).

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
                "twitterHandles": usernames,
                "maxItems": len(usernames) * results_per_profile,
                "sort": "Latest",
            }
        )
        if not run:
            raise RuntimeError("Apify actor run timed out with no result.")
        if run.status != "SUCCEEDED":
            raise RuntimeError(f"Apify actor run ended with status: {run.status}")

        items = list(self._client.dataset(run.default_dataset_id).iterate_items())
        return self._creators_from_items(items)

    def search(self, queries: list[str], results_per_query: int = 20) -> list[dict]:
        """Discover creators by TOPIC via X search (not by handle).

        Each query returns matching tweets; creators are grouped from the tweets'
        authors, deduped, and built into CreatorScorer-ready dicts (>= follower
        floor). Mirrors TikTokCreatorFetcher.search().
        """
        return self._creators_from_items(self.search_tweets(queries, results_per_query))

    def search_tweets(self, queries: list[str], results_per_query: int = 20) -> list[dict]:
        """Topic search returning RAW tweet items (one row per tweet)."""
        if not queries:
            return []

        run = self._client.actor(_ACTOR_ID).call(
            run_input={
                "searchTerms": queries,
                "maxItems": len(queries) * results_per_query,
                "sort": "Latest",
            }
        )
        if not run:
            raise RuntimeError("Apify search actor run timed out with no result.")
        if run.status not in ("SUCCEEDED", "TIMED-OUT"):
            raise RuntimeError(f"Apify search actor run ended with status: {run.status}")

        return list(self._client.dataset(run.default_dataset_id).iterate_items())

    # ------------------------------------------------------------------
    # Build helpers
    # ------------------------------------------------------------------

    def _creators_from_items(self, items: list[dict]) -> list[dict]:
        """Group tweet rows by author handle and build per-creator dicts."""
        by_handle: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            author = item.get("author") or {}
            handle = author.get("userName", "") if isinstance(author, dict) else ""
            if handle:
                by_handle[handle].append(item)

        creators = []
        for tweets in by_handle.values():
            try:
                creator = self._build_creator(tweets)
                if creator:
                    creators.append(creator)
            except ApifyApiError:
                raise
            except Exception:
                continue
        return creators

    def _build_creator(self, tweets: list[dict]) -> Optional[dict]:
        """Assemble a CreatorScorer-compatible dict from one author's tweet rows."""
        if not tweets:
            return None

        author = tweets[0].get("author", {}) or {}
        # Prefer the display name; fall back to the @handle.
        name = (author.get("name") or author.get("userName", "")).strip()
        if not name:
            return None

        followers = int(author.get("followers", 0) or 0)
        if followers < MIN_FOLLOWERS:
            return None
        bio = author.get("description", "") or ""

        view_counts = [int(t.get("viewCount", 0) or 0) for t in tweets]
        like_counts = [int(t.get("likeCount", 0) or 0) for t in tweets]
        # Reposts + replies + quotes are all genuine interactions on X.
        interaction_counts = [
            int(t.get("likeCount", 0) or 0)
            + int(t.get("retweetCount", 0) or 0)
            + int(t.get("replyCount", 0) or 0)
            + int(t.get("quoteCount", 0) or 0)
            for t in tweets
        ]

        avg_views = sum(view_counts) / len(view_counts) if view_counts else 0.0
        avg_interactions = sum(interaction_counts) / len(interaction_counts) if interaction_counts else 0.0

        # Reach ratio, capped at 1.0 — a viral tweet's impressions can exceed the
        # follower base, which would otherwise inflate the scorer's reach proxy.
        # Mirrors the TikTok/YouTube engagement_rate definition.
        engagement_rate = (
            min(round(avg_views / followers, 4), 1.0) if followers > 0 else 0.0
        )

        # Hashtags across the sampled tweets (entities.hashtags: [{"text": ...}]).
        all_hashtags: list[str] = []
        for t in tweets:
            for ht in (t.get("entities", {}) or {}).get("hashtags", []) or []:
                tag = ht.get("text", "") if isinstance(ht, dict) else str(ht)
                if tag:
                    all_hashtags.append(tag)

        tweet_texts = [(t.get("fullText") or t.get("text") or "") for t in tweets]

        crypto_pct, fintech_pct = self._estimate_content_pcts(bio, tweet_texts, all_hashtags)
        niche_tags = self._derive_niche_tags(bio, all_hashtags)
        eq = self._engagement_quality(avg_views, avg_interactions)

        return {
            "name": name,
            "platform": "X",
            "followers": followers,
            "engagement_rate": engagement_rate,
            "engagement_quality": eq,
            "crypto_content_pct": crypto_pct,
            "fintech_content_pct": fintech_pct,
            "sponsorship_count": 0,
            # X exposes NO sponsored/paid-partnership flag (verified against live
            # actor output) — so sponsorship is genuinely NOT measured. Marked
            # False, not fabricated: the scorer excludes it and renormalises.
            "sponsorship_data_available": False,
            "niche_tags": niche_tags,
            # Retained for the scorer's LLM segment-fallback (name/desc/tags).
            "description": bio,
        }

    # ------------------------------------------------------------------
    # Estimation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _engagement_quality(avg_views: float, avg_interactions: float) -> int:
        """Estimate 1–10 engagement quality from interaction-to-impression ratio.

        X interaction rates (likes+reposts+replies+quotes / impressions) run LOWER
        than TikTok — healthy accounts typically sit ~0.5–3% of impressions, so the
        thresholds are calibrated well below TikTok's FYP-inflated scale. Falls back
        to 1 when impressions aren't exposed (older tweets).
        """
        if avg_views <= 0:
            return 1
        rate = avg_interactions / avg_views
        if rate >= 0.06:
            return 10
        if rate >= 0.04:
            return 9
        if rate >= 0.025:
            return 8
        if rate >= 0.015:
            return 7
        if rate >= 0.010:
            return 6
        if rate >= 0.006:
            return 5
        if rate >= 0.003:
            return 4
        if rate >= 0.0015:
            return 3
        return 2

    @staticmethod
    def _estimate_content_pcts(
        bio: str, texts: list[str], hashtags: list[str]
    ) -> tuple[float, float]:
        """Estimate crypto and fintech content percentages from bio + tweets + tags."""
        text = " ".join([bio] + texts + hashtags).lower()
        crypto_hits = sum(1 for kw in _CRYPTO_KEYWORDS if kw in text)
        fintech_hits = sum(1 for kw in _FINTECH_KEYWORDS if kw in text)
        crypto_pct = round(min(crypto_hits / _CRYPTO_SAT, 1.0) * _CRYPTO_CAP, 2)
        fintech_pct = round(min(fintech_hits / _FINTECH_SAT, 1.0) * _FINTECH_CAP, 2)
        return crypto_pct, fintech_pct

    @staticmethod
    def _derive_niche_tags(bio: str, hashtags: list[str]) -> list[str]:
        """Derive niche tags from bio text and tweet hashtags."""
        text = bio.lower()
        tags = {tag for tag in _SCORER_TAGS if tag in text}
        for ht in hashtags:
            ht_lower = ht.lower().replace("-", " ")
            if ht_lower in _SCORER_TAGS:
                tags.add(ht_lower)
        return sorted(tags)


def fetch_x_creators(usernames: list[str], results_per_profile: int = 15) -> list[dict]:
    """Convenience wrapper — loads APIFY_API_KEY from env and returns creator dicts."""
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise EnvironmentError("APIFY_API_KEY not set in .env")
    return XCreatorFetcher(api_key).fetch(usernames, results_per_profile=results_per_profile)


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from creators.scorer import CreatorScorer

    TEST_HANDLES = ["WatcherGuru", "saylor", "BitcoinMagazine"]
    print(f"Fetching X profiles: {', '.join(TEST_HANDLES)}...")
    print("(This runs an Apify actor — takes ~15–30s)\n")
    try:
        creators = fetch_x_creators(TEST_HANDLES, results_per_profile=15)
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
    except (RuntimeError, ApifyApiError) as e:
        print(f"Actor error: {e}")
        sys.exit(1)

    scorer = CreatorScorer(use_llm_fallback=False)
    for c in creators:
        s = scorer.score(c)
        print(f"{c['name']} (@X) — {c['followers']:,} followers")
        print(f"  ER={c['engagement_rate']} EQ={c['engagement_quality']} "
              f"crypto={c['crypto_content_pct']} fintech={c['fintech_content_pct']} "
              f"tags={c['niche_tags']}")
        print(f"  sponsorship_data_available={c['sponsorship_data_available']} "
              f"composite={s['composite_score']} basis={s['composite_basis']}\n")
