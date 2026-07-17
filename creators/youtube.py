"""YouTube Data API v3 creator discovery for Speed Wallet partner scouting."""

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# Repo root on path so the scorer import works whether this module is imported
# as creators.youtube or run directly (python creators/youtube.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from creators.scorer import CRYPTO_TAGS, IGAMING_TAGS, REMITTANCE_TAGS

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
# Vocabulary used to derive niche_tags from channel text. Built directly from
# the scorer's segment tag sets so there is ONE source of truth: a term added to
# REMITTANCE_TAGS / IGAMING_TAGS / CRYPTO_TAGS in scorer.py automatically becomes
# extractable here, and only contributes to a segment the scorer recognises.
# (There is no separate FINTECH_TAGS — fintech/payments terms live in CRYPTO_TAGS.)
_SCORER_TAGS = REMITTANCE_TAGS | IGAMING_TAGS | CRYPTO_TAGS


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

    def search(self, query: str, max_results: int = 10,
               region_code: str | None = None,
               relevance_language: str = "en") -> list[dict]:
        """Search YouTube channels and return CreatorScorer-ready dicts.

        Args:
            query: Search string (e.g. "bitcoin lightning wallet").
            max_results: Number of channels to return (1–50).
            region_code: Optional ISO 3166-1 alpha-2 country (e.g. "DE", "GB",
                "PT") to bias results toward that market — the same regionCode
                lever the trend live-search uses. None = no region bias (default).
            relevance_language: ISO 639-1 language to bias toward (e.g. "de",
                "pt"); defaults to "en" to preserve existing callers' behavior.

        Returns:
            List of creator dicts compatible with CreatorScorer.score().

        Raises:
            QuotaExceededError: Daily API quota exhausted.
            requests.HTTPError: Any other non-recoverable API error.
        """
        channel_ids = self._search_channels(query, max_results,
                                             region_code=region_code,
                                             relevance_language=relevance_language)
        if not channel_ids:
            return []

        creators = []
        for ch in self._fetch_channel_details(channel_ids):
            try:
                creator = self._build_creator(ch)
                if creator:
                    creators.append(creator)
            except QuotaExceededError:
                # Quota / rate limit is run-fatal — propagate so the caller can
                # stop gracefully and save progress. Never swallow it.
                raise
            except requests.HTTPError as e:
                # A single channel can fail in isolation — most commonly a 404 on
                # its uploads playlist. Skip just this channel and keep going so
                # one bad channel can't abort the whole search term or run.
                name = ch.get("snippet", {}).get("title", "?")
                print(f"  ! skipping channel '{name}' — API error: {e}")
                continue
            except Exception:
                # Skip individual channels with incomplete data without aborting the batch.
                continue

        return creators

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    # Two DIFFERENT limits, handled differently:
    #  * DAILY quota (10k units/day, resets midnight Pacific) — fatal for the day.
    #    Reported as dailyLimitExceeded/quotaExceeded, OR as a 429 whose message
    #    says "per day" (e.g. "Search Queries per day"). Caller stops + resumes
    #    tomorrow.
    #  * BURST rate limit (queries per 100 seconds) — TRANSIENT. Reported as
    #    rateLimitExceeded/userRateLimitExceeded without a "per day" message. We
    #    back off and retry the same request so one invocation keeps making
    #    progress instead of aborting on a momentary spike.
    _DAILY_REASONS = {"quotaexceeded", "dailylimitexceeded"}
    _BURST_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}
    _BURST_BACKOFF_SECS = (5, 15, 45, 90)   # exponential-ish; ~155s total worst case

    def _get(self, endpoint: str, params: dict) -> dict:
        for attempt in range(len(self._BURST_BACKOFF_SECS) + 1):
            resp = self._session.get(
                f"{_BASE}/{endpoint}",
                params={**params, "key": self._key},
                timeout=10,
            )

            if resp.status_code in (403, 429):
                reasons = {r.lower() for r in self._error_reasons(resp)}
                message = self._error_message(resp).lower()
                is_daily = ("per day" in message) or bool(reasons & self._DAILY_REASONS)
                is_burst = (not is_daily) and (
                    resp.status_code == 429 or bool(reasons & self._BURST_REASONS)
                )

                if is_daily:
                    detail = ", ".join(sorted(reasons)) or f"HTTP {resp.status_code}"
                    print(f"  !! YouTube DAILY quota exhausted ({detail}) — stopping; "
                          f"resume after the midnight-Pacific reset.")
                    raise QuotaExceededError(
                        f"YouTube API daily quota reached ({detail})."
                    )
                if is_burst and attempt < len(self._BURST_BACKOFF_SECS):
                    wait = self._BURST_BACKOFF_SECS[attempt]
                    print(f"  .. burst rate limit (per-100s); backing off {wait}s and "
                          f"retrying ({attempt + 1}/{len(self._BURST_BACKOFF_SECS)})")
                    time.sleep(wait)
                    continue
                # A burst limit that outlasted all retries, or any other 403/429 →
                # fall through to raise_for_status below.

            resp.raise_for_status()
            return resp.json()

        # Burst retries exhausted without a success: surface the last response.
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _error_reasons(resp) -> list[str]:
        """Extract the API error 'reason' codes, tolerating a non-JSON body."""
        try:
            errors = resp.json().get("error", {}).get("errors", []) or []
        except ValueError:
            return []
        return [e.get("reason", "") for e in errors if e.get("reason")]

    @staticmethod
    def _error_message(resp) -> str:
        """Extract the API error 'message' (distinguishes daily vs burst), tolerant."""
        try:
            return resp.json().get("error", {}).get("message", "") or ""
        except ValueError:
            return ""

    def _search_channels(self, query: str, max_results: int,
                         region_code: str | None = None,
                         relevance_language: str = "en") -> list[str]:
        params = {
            "part": "id",
            "q": query,
            "type": "channel",
            "maxResults": min(max_results, 50),
            "relevanceLanguage": relevance_language,
        }
        if region_code:
            params["regionCode"] = region_code   # bias to a market (DE/GB/PT/...)
        data = self._get("search", params)
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
        """Fetch per-video statistics AND the paid-product-placement flag.

        `paidProductPlacementDetails.hasPaidProductPlacement` is YouTube's own
        creator-declared sponsorship flag — the direct analogue of TikTok's
        isSponsored. It rides on the SAME videos.list call (no extra quota) and,
        verified 2026-07, IS returned to third-party API keys. It is low-recall
        (creators self-declare; many sponsored videos go unflagged), so treat it
        as an undercount, not ground truth — but it is real, not fabricated.
        """
        if not video_ids:
            return []
        data = self._get("videos", {
            "part": "statistics,paidProductPlacementDetails",
            "id": ",".join(video_ids),
        })
        return data.get("items", [])

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
        sampled = 0
        sponsored_in_sample = 0
        if uploads_id:
            video_ids = self._fetch_recent_video_ids(uploads_id, count=10)
            if video_ids:
                vitems = self._fetch_video_stats(video_ids)
                sampled = len(vitems)
                vstats = [it.get("statistics", {}) for it in vitems]
                avg_views = self._field_avg(vstats, "viewCount")
                avg_likes = self._field_avg(vstats, "likeCount")
                avg_comments = self._field_avg(vstats, "commentCount")
                # Creator-declared paid-promotion flag (undercounts; see _fetch_video_stats).
                sponsored_in_sample = sum(
                    1 for it in vitems
                    if (it.get("paidProductPlacementDetails") or {}).get("hasPaidProductPlacement")
                )

        # Extrapolate total sponsored videos from the flagged fraction in the sample,
        # mirroring the TikTok fetcher. Real data (self-declared flag), so mark it
        # available — a measured 0 is now distinguishable from "no data".
        total_videos = int(stats.get("videoCount", 0) or 0)
        sponsored_fraction = (sponsored_in_sample / sampled) if sampled else 0.0
        sponsorship_count = round(sponsored_fraction * total_videos)

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
            "sponsorship_count": sponsorship_count,
            "sponsorship_data_available": True,
            "niche_tags": self._derive_niche_tags(name, description, topic_categories),
            # Retained for the scorer's LLM fallback classifier (name/desc/tags).
            "description": description,
            # Creator's self-declared channel country (ISO code), or None if the
            # owner never set it (~25% are null). Already in the snippet we fetch,
            # so no extra quota. Consumed by database.derive_creator_country.
            "channel_country": snippet.get("country") or None,
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
