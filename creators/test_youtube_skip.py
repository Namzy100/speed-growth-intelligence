"""Unit test: a 404 on one channel's uploads playlist must not abort search().

Offline test — all YouTube API calls are mocked. Verifies that when one channel
in a search result 404s on its uploads playlist, search() skips just that
channel and still returns the others, while a QuotaExceededError still aborts.

Run from repo root:  python creators/test_youtube_skip.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from creators.youtube import QuotaExceededError, YouTubeCreatorFetcher


def _channel(title: str, uploads_id: str, subs: int = 10_000) -> dict:
    return {
        "snippet": {"title": title, "description": "crypto bitcoin investing tips"},
        "statistics": {"subscriberCount": str(subs), "hiddenSubscriberCount": False},
        "topicDetails": {"topicCategories": []},
        "contentDetails": {"relatedPlaylists": {"uploads": uploads_id}},
    }


def _make_fetcher(channels: list[dict], bad_playlist: str | None) -> YouTubeCreatorFetcher:
    f = YouTubeCreatorFetcher("dummy-key")
    f._search_channels = lambda q, n: [c["snippet"]["title"] for c in channels]
    f._fetch_channel_details = lambda ids: channels

    def fake_get(endpoint: str, params: dict) -> dict:
        if endpoint == "playlistItems":
            if bad_playlist is not None and params.get("playlistId") == bad_playlist:
                resp = requests.Response()
                resp.status_code = 404
                raise requests.HTTPError("404 Client Error: Not Found", response=resp)
            return {"items": [{"contentDetails": {"videoId": "v1"}}]}
        if endpoint == "videos":
            return {"items": [{"statistics": {"viewCount": "1000",
                                              "likeCount": "50", "commentCount": "5"}}]}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    f._get = fake_get
    return f


def test_bad_playlist_channel_is_skipped() -> None:
    """One 404 channel is skipped; the other two are still processed."""
    channels = [
        _channel("Good One", "UU_GOOD1"),
        _channel("Bad Channel", "UU_BAD"),
        _channel("Good Two", "UU_GOOD2"),
    ]
    f = _make_fetcher(channels, bad_playlist="UU_BAD")

    result = f.search("anything", max_results=3)
    names = {c["name"] for c in result}

    assert len(result) == 2, f"expected 2 creators, got {len(result)}: {names}"
    assert names == {"Good One", "Good Two"}, f"unexpected names: {names}"
    assert "Bad Channel" not in names, "the 404 channel should have been skipped"
    print("PASS: 404 on one channel's playlist skipped it; other 2 channels processed.")


def test_quota_error_still_propagates() -> None:
    """A QuotaExceededError during a channel must still abort the search."""
    f = _make_fetcher([_channel("X", "UU_X")], bad_playlist=None)

    def quota_build(_ch):
        raise QuotaExceededError("quota exhausted")

    f._build_creator = quota_build

    try:
        f.search("x", max_results=1)
    except QuotaExceededError:
        print("PASS: QuotaExceededError still propagates (not swallowed by skip logic).")
        return
    raise AssertionError("QuotaExceededError should have propagated, but search() returned")


if __name__ == "__main__":
    test_bad_playlist_channel_is_skipped()
    test_quota_error_still_propagates()
    print("\nAll tests passed.")
