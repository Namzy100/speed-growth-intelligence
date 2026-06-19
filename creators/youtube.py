"""YouTube Data API v3 creator discovery for Speed Wallet partner scouting."""

import os
import re
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://www.googleapis.com/youtube/v3"

# Keywords scanned in channel descriptions and topic categories to estimate
# how much of the creator's content touches Speed-relevant domains.
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

# Keyword hits required to reach the cap — prevents description spam inflating scores.
_CRYPTO_SAT = 6
_FINTECH_SAT = 5

# Description-only signals can't confirm video content; cap below 1.0.
_CRYPTO_CAP = 0.80
_FINTECH_CAP = 0.60

# Tags from scorer.py that we try to match in descriptions and topic titles.
_SCORER_TAGS = {
    "remittance", "diaspora", "expat", "expats", "migrant", "migrants",
    "money transfer", "send money", "forex", "wire transfer", "immigrant",
    "immigrants", "overseas",
    "igaming", "gambling", "casino", "betting", "poker", "slots", "esports",
    "sports betting", "sports bet", "esports betting", "fantasy sports",
    "online gambling", "sportsbook", "wager", "play to earn", "p2e",
    "crypto", "bitcoin", "ethereum", "blockchain", "defi", "web3", "nft",
    "cryptocurrency", "altcoin", "trading", "investing", "finance",
    "personal finance", "fintech", "payments", "lightning", "btc", "eth",
    "satoshi", "hodl",
}


# Reject channels below this subscriber count. Tiny channels produce
# artificially capped engagement ratios (avg_views/subscribers >> 1, clamped
# to 1.0) that inflate engagement scores — e.g. 5- and 20-subscriber junk
# channels scoring 15–19/20 on engagement in early test runs.
MIN_SUBSCRIBERS = 5_000


class QuotaExceededError(Exception):
    """Raised when the YouTube API daily quota is exhausted."""


class YouTubeCreatorFetcher:
    """Fetches YouTube channel data formatted for CreatorScorer.

    API cost per search(max_results=N):
        search.list          100 units
        channels.list          1 unit
        playlistItems.list     N units  (one per channel)
        videos.list            N units  (one per channel)
        ─────────────────────────────
        Total for N=10:      ~121 units  (daily limit: 10,000)
    """

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._session = requests.Session()

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search YouTube channels and return CreatorScorer-ready dicts.

        Args:
            query: Search string (e.g. "bitcoin lightning wallet").
            max_results: Number of channels to return (1–50).

        Returns:
            List of creator dicts compatible with CreatorScorer.score().

        Raises:
            QuotaExceededError: Daily API quota exhausted.
            requests.HTTPError: Any other non-recoverable API error.
        """
        channel_ids = self._search_channels(query, max_results)
        if not channel_ids:
            return []

        creators = []
        for ch in self._fetch_channel_details(channel_ids):
            try:
                creator = self._build_creator(ch)
                if creator:
                    creators.append(creator)
            except (QuotaExceededError, requests.HTTPError):
                raise
            except Exception:
                # Skip individual channels with incomplete data without aborting the batch.
                continue

        return creators

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> dict:
        resp = self._session.get(
            f"{_BASE}/{endpoint}",
            params={**params, "key": self._key},
            timeout=10,
        )
        if resp.status_code == 403:
            errors = resp.json().get("error", {}).get("errors", [])
            if errors and errors[0].get("reason") == "quotaExceeded":
                raise QuotaExceededError("YouTube API daily quota exhausted.")
        resp.raise_for_status()
        return resp.json()

    def _search_channels(self, query: str, max_results: int) -> list[str]:
        data = self._get("search", {
            "part": "id",
            "q": query,
            "type": "channel",
            "maxResults": min(max_results, 50),
            "relevanceLanguage": "en",
        })
        return [item["id"]["channelId"] for item in data.get("items", [])]

    def _fetch_channel_details(self, channel_ids: list[str]) -> list[dict]:
        data = self._get("channels", {
            "part": "snippet,statistics,topicDetails,contentDetails",
            "id": ",".join(channel_ids),
            "maxResults": 50,
        })
        return data.get("items", [])

    def _fetch_recent_video_ids(self, uploads_playlist_id: str, count: int = 10) -> list[str]:
        # Uploads playlist returns videos newest-first by default.
        data = self._get("playlistItems", {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": count,
        })
        return [item["contentDetails"]["videoId"] for item in data.get("items", [])]

    def _fetch_video_stats(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        data = self._get("videos", {
            "part": "statistics",
            "id": ",".join(video_ids),
        })
        return [item.get("statistics", {}) for item in data.get("items", [])]

    # ------------------------------------------------------------------
    # Estimation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _field_avg(stats: list[dict], key: str) -> float:
        values = [int(s[key]) for s in stats if s.get(key)]
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _engagement_quality(avg_views: float, avg_likes: float, avg_comments: float) -> int:
        """Estimate 1–10 engagement quality from the interaction-to-view ratio.

        YouTube interaction rate (likes + comments / views) benchmarks:
        healthy channels typically land in the 2–6% range.
        """
        if avg_views <= 0:
            return 1
        rate = (avg_likes + avg_comments) / avg_views
        if rate >= 0.10:
            return 10
        if rate >= 0.07:
            return 9
        if rate >= 0.05:
            return 8
        if rate >= 0.03:
            return 7
        if rate >= 0.02:
            return 6
        if rate >= 0.01:
            return 5
        if rate >= 0.005:
            return 4
        if rate >= 0.002:
            return 3
        return 2

    @staticmethod
    def _estimate_content_pcts(description: str, topics: list[str]) -> tuple[float, float]:
        """Estimate crypto and fintech content percentages from text signals."""
        text = (description + " " + " ".join(topics)).lower()

        crypto_hits = sum(1 for kw in _CRYPTO_KEYWORDS if kw in text)
        fintech_hits = sum(1 for kw in _FINTECH_KEYWORDS if kw in text)

        # Wikipedia topic category URLs (e.g. ".../Bitcoin") are strong signals.
        topic_text = " ".join(t.lower() for t in topics)
        if any(kw in topic_text for kw in ("bitcoin", "cryptocurrency", "blockchain", "ethereum")):
            crypto_hits += 3
        if any(kw in topic_text for kw in ("finance", "financial", "banking", "economics")):
            fintech_hits += 2

        crypto_pct = round(min(crypto_hits / _CRYPTO_SAT, 1.0) * _CRYPTO_CAP, 2)
        fintech_pct = round(min(fintech_hits / _FINTECH_SAT, 1.0) * _FINTECH_CAP, 2)
        return crypto_pct, fintech_pct

    @staticmethod
    def _derive_niche_tags(name: str, description: str, topics: list[str]) -> list[str]:
        """Build niche tags from the channel name, description, and topic titles.

        The name is scanned too: channels like "Leading Crypto Casinos" or
        "PlayToEarn" carry their strongest segment signal in the title, not the
        description. CamelCase names are also split (PlayToEarn -> "play to
        earn") so multi-word tags still match.
        """
        name_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
        text = f"{name} {name_split} {description}".lower()
        tags = {tag for tag in _SCORER_TAGS if tag in text}

        for url in topics:
            title = url.rstrip("/").split("/")[-1].replace("_", " ").lower()
            if title and title not in ("youtube",):
                tags.add(title)

        return sorted(tags)

    # ------------------------------------------------------------------

    def _build_creator(self, channel: dict) -> Optional[dict]:
        """Assemble a CreatorScorer-compatible dict from raw channel API data."""
        snippet = channel.get("snippet", {})
        stats = channel.get("statistics", {})
        topic_details = channel.get("topicDetails", {})
        content_details = channel.get("contentDetails", {})

        name = snippet.get("title", "").strip()
        if not name:
            return None

        # Channels with hidden subscriber counts can't be scored on reach.
        if stats.get("hiddenSubscriberCount", False):
            return None

        subscribers = int(stats.get("subscriberCount", 0))

        # Subscriber floor: reject channels too small to score reliably.
        if subscribers < MIN_SUBSCRIBERS:
            return None

        uploads_id = content_details.get("relatedPlaylists", {}).get("uploads", "")
        avg_views = avg_likes = avg_comments = 0.0
        if uploads_id:
            video_ids = self._fetch_recent_video_ids(uploads_id, count=10)
            if video_ids:
                vstats = self._fetch_video_stats(video_ids)
                avg_views = self._field_avg(vstats, "viewCount")
                avg_likes = self._field_avg(vstats, "likeCount")
                avg_comments = self._field_avg(vstats, "commentCount")

        # View-per-video relative to subscriber base is the best public reach proxy.
        # Capped at 1.0 — viral/evergreen content can push the raw ratio above 100%,
        # which would distort acquisition_potential in the scorer.
        engagement_rate = min(round(avg_views / subscribers, 4), 1.0) if subscribers > 0 else 0.0

        description = snippet.get("description", "")
        topic_categories = topic_details.get("topicCategories", [])

        crypto_pct, fintech_pct = self._estimate_content_pcts(description, topic_categories)

        return {
            "name": name,
            "platform": "YouTube",
            "followers": subscribers,
            "engagement_rate": engagement_rate,
            "engagement_quality": self._engagement_quality(avg_views, avg_likes, avg_comments),
            "crypto_content_pct": crypto_pct,
            "fintech_content_pct": fintech_pct,
            "sponsorship_count": 0,
            "niche_tags": self._derive_niche_tags(name, description, topic_categories),
        }


def fetch_youtube_creators(query: str, max_results: int = 10) -> list[dict]:
    """Convenience wrapper — loads API key from env and returns creator dicts."""
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise EnvironmentError("YOUTUBE_API_KEY not set in .env")
    return YouTubeCreatorFetcher(api_key).search(query, max_results=max_results)


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from creators.scorer import CreatorScorer

    QUERY = "bitcoin lightning wallet"
    print(f"Searching YouTube for '{QUERY}'...")

    try:
        creators = fetch_youtube_creators(QUERY, max_results=10)
    except QuotaExceededError as e:
        print(f"Quota exceeded: {e}")
        sys.exit(1)
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    if not creators:
        print("No creators returned.")
        sys.exit(0)

    scorer = CreatorScorer()
    results = sorted(
        (scorer.score(c) for c in creators),
        key=lambda r: r["composite_score"],
        reverse=True,
    )

    print(f"\nTop 3 results from {len(creators)} channels found:\n")
    for result in results[:3]:
        print(f"{'=' * 60}")
        print(f"Creator  : {result['name']} ({result['platform']})")
        print(f"Segment  : {result['segment_tag']}")
        print(f"Composite: {result['composite_score']} / 100")
        print("Breakdown:")
        for dim, val in result["scores"].items():
            bar = "█" * int(val / 20 * 20)
            print(f"  {dim:<26} {val:>4} / 20  {bar}")
        print(f"\n{result['reasoning']}\n")
