"""Deterministic, no-API tests that PIN the scorer math (creators/scorer.py).

scorer.py is the heart of the project and changed the most in the 2026-07 audit.
These tests pin the actual numbers so a successor can't silently regress them.
Everything here is offline: the LLM classifier path is bypassed by constructing
the scorer with use_llm_fallback=False, and the deterministic logic is tested
directly. No network, no Anthropic, no Supabase.

Mirrors the repo's existing test convention (creators/test_youtube_skip.py):
plain `def test_*()` functions with asserts, run from a __main__ block.

Run from repo root:  python creators/test_scorer.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from creators.scorer import CreatorScorer, looks_like_media_name, looks_like_company
from pipelines.build_creator_dashboard import _score_bands, _percentile

_TOL = 1e-3


def _approx(a: float, b: float, tol: float = _TOL) -> bool:
    return abs(a - b) <= tol


def _base_creator(**overrides) -> dict:
    """A normal, fully-scraped creator with all four real dimensions present."""
    c = {
        "name": "Jane Crypto",
        "platform": "YouTube",
        "followers": 1_000_000,          # reach dimension -> exactly 20.0
        "engagement_quality": 8,
        "engagement_rate": 0.05,
        "crypto_content_pct": 0.7,
        "fintech_content_pct": 0.2,
        "niche_tags": ["bitcoin", "crypto", "ethereum"],
        "sponsorship_count": 10,
        "sponsorship_data_available": True,
        "scraped_data_available": True,
    }
    c.update(overrides)
    return c


# ------------------------------------------------------------------
# 1. Composite: 4-dimension equal-weighted renormalization
# ------------------------------------------------------------------

def test_composite_all_four_dimensions_equal_weighted() -> None:
    """All 4 dims available -> composite == equal-weighted average scaled to 100,
    computed over the UNROUNDED dimension scores with denominator 20*4."""
    sc = CreatorScorer(use_llm_fallback=False)
    c = _base_creator()

    af, _seg, _ = sc._score_audience_fit(c)
    eng, _ = sc._score_engagement_quality(c)
    reach, _ = sc._score_reach(c)
    spons, _ = sc._score_sponsorship_history(c)
    expected = round((af + eng + reach + spons) * 100.0 / (20.0 * 4), 1)

    result = sc.score(c)
    assert result["composite_basis"] == ["audience_fit", "engagement", "reach", "sponsorship"], \
        result["composite_basis"]
    assert result["composite_score"] == expected, \
        f"composite {result['composite_score']} != equal-weighted {expected}"
    print(f"PASS: 4-dim composite = equal-weighted average ({expected}).")


def test_composite_renormalizes_when_sponsorship_unavailable() -> None:
    """Sponsorship not measured -> renormalize over the remaining 3 dims (denom
    20*3), NOT treat the missing dim as 0 (which would use denom 20*4 and score
    strictly lower). Proves the renormalization, not zero-fill."""
    sc = CreatorScorer(use_llm_fallback=False)
    c = _base_creator(sponsorship_data_available=False)

    af, _seg, _ = sc._score_audience_fit(c)
    eng, _ = sc._score_engagement_quality(c)
    reach, _ = sc._score_reach(c)

    expected_renorm = round((af + eng + reach) * 100.0 / (20.0 * 3), 1)
    wrong_zero_fill = round((af + eng + reach + 0.0) * 100.0 / (20.0 * 4), 1)

    result = sc.score(c)
    assert result["composite_basis"] == ["audience_fit", "engagement", "reach"], \
        result["composite_basis"]
    assert result["composite_score"] == expected_renorm, \
        f"composite {result['composite_score']} != 3-dim renorm {expected_renorm}"
    assert result["composite_score"] != wrong_zero_fill, \
        "composite matches the zero-fill value — sponsorship was silently treated as 0!"
    assert expected_renorm > wrong_zero_fill, "sanity: renorm must exceed zero-fill"
    print(f"PASS: missing sponsorship renormalizes over 3 dims ({expected_renorm}), "
          f"not zero-filled ({wrong_zero_fill}).")


# ------------------------------------------------------------------
# 2. Reach curve (Curve B): (log10(followers) - 3) / 3 * 20
# ------------------------------------------------------------------

def _reach(followers: int) -> float:
    return CreatorScorer(use_llm_fallback=False)._score_reach({"followers": followers})[0]


def test_reach_curve_pinned_values() -> None:
    assert _reach(999) == 0.0, "sub-1k must hit the reach floor (0)"
    assert _reach(1_000) == 0.0, "1k maps to exactly 0"
    assert _approx(_reach(30_000), 9.8475), _reach(30_000)
    assert _approx(_reach(100_000), 13.3333), _reach(100_000)
    assert _reach(1_000_000) == 20.0, "1M maps to a full 20"
    # strictly increasing between the floor and the ceiling
    assert _reach(30_000) < _reach(100_000) < _reach(1_000_000)
    print("PASS: reach curve pinned (0 @<1k, 9.85 @30k, 13.33 @100k, 20.0 @1M).")


def test_reach_saturates_above_1M() -> None:
    """Above 1M the curve must clamp at 20, not keep climbing (real diminishing
    returns — a 3M and a 1M creator score ~the same on reach)."""
    assert _reach(3_000_000) == 20.0, "3M must saturate at 20"
    assert _reach(10_000_000) == 20.0, "10M must saturate at 20"
    assert _reach(3_000_000) == _reach(1_000_000), "reach must not exceed 20 above 1M"
    print("PASS: reach saturates at 20 above 1M (does not keep climbing).")


# ------------------------------------------------------------------
# 3. Sponsorship gating (uses the real column, not a proxy) + scorer values
# ------------------------------------------------------------------

def test_sponsorship_gating_uses_real_column() -> None:
    """sponsorship_data_available True -> included; False -> excluded + weight
    redistributed. And it keys on the REAL column, not the old platform=='TikTok'
    proxy (a TikTok creator with the flag False must still exclude sponsorship)."""
    sc = CreatorScorer(use_llm_fallback=False)

    incl = sc.score(_base_creator(sponsorship_data_available=True))
    excl = sc.score(_base_creator(sponsorship_data_available=False))
    assert "sponsorship" in incl["composite_basis"], incl["composite_basis"]
    assert "sponsorship" not in excl["composite_basis"], excl["composite_basis"]

    # Real-column, not platform proxy: TikTok + flag False -> still excluded.
    tt = sc.score(_base_creator(platform="TikTok", sponsorship_data_available=False))
    assert "sponsorship" not in tt["composite_basis"], \
        "sponsorship included from a platform proxy instead of the real column"
    assert tt["sponsorship_data_available"] is False
    print("PASS: sponsorship gates on the real column (not the platform proxy).")


def test_sponsorship_scorer_values() -> None:
    sc = CreatorScorer(use_llm_fallback=False)

    def spons(n):
        return sc._score_sponsorship_history({"sponsorship_count": n})[0]

    assert spons(0) == 0.0, "no deals -> 0"
    assert _approx(spons(1), 4.5534), spons(1)
    assert _approx(spons(10), 15.7524), spons(10)
    assert spons(20) == 20.0, "20 deals -> full 20"
    # Above the self-promo threshold (50) -> fixed modest score, never the ceiling.
    assert spons(51) == 6.0, "self-promotion artifact must be down-weighted to 6.0"
    assert spons(500) == 6.0
    print("PASS: sponsorship scorer values pinned (0,1,10,20 curve; >50 -> 6.0).")


# ------------------------------------------------------------------
# 4. Percentile colour bands (must recompute, never go stale)
# ------------------------------------------------------------------

def test_score_bands_percentile_cutoffs() -> None:
    """Cutoffs against a known distribution, hand-verified via linear interpolation
    (green = 75th pct, red = 35th pct)."""
    dist = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    b = _score_bands(dist)
    assert b["green"] == 77.5, b            # p75: 70 + 10*0.75
    assert b["red"] == 41.5, b              # p35: 40 + 10*0.15
    assert b["green_pct"] == 25 and b["red_pct"] == 35, b
    # edge cases
    assert _score_bands([50]) == {"green": 50.0, "red": 50.0, "green_pct": 25, "red_pct": 35}
    assert _score_bands([])["green"] == 0.0 and _score_bands([])["red"] == 0.0
    print("PASS: _score_bands cutoffs correct (green 77.5 / red 41.5 on the known dist).")


def test_score_bands_shift_with_distribution() -> None:
    """The mechanism that's supposed to never go stale: change the underlying
    scores and the cutoffs must move on the next build."""
    high = _score_bands([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    low = _score_bands([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    # cutoffs are rounded to 1 decimal by _score_bands (raw 7.75/4.15 -> 7.8/4.2).
    assert high["green"] == 77.5 and low["green"] == 7.8, (high, low)
    assert high["red"] == 41.5 and low["red"] == 4.2, (high, low)
    assert high["green"] != low["green"] and high["red"] != low["red"], \
        "bands did NOT shift with the distribution — cutoffs are stale/fixed!"
    print("PASS: colour bands shift with the distribution (recomputed, not fixed).")


# ------------------------------------------------------------------
# 5. is_influencer deterministic (keyword) fallback path
# ------------------------------------------------------------------

def test_influencer_fallback_catches_keyword_media_and_company() -> None:
    """With the LLM disabled, media/company NAMES the keyword heuristic can see
    must gate is_influencer to False even when engagement is strong."""
    sc = CreatorScorer(use_llm_fallback=False)   # deterministic path only
    media_or_company = [
        "CoinDesk News", "Bitcoin Magazine", "Cointelegraph TV", "Binance Academy",
        "Crypto Daily News",                       # media (substring/word)
        "ACE Money Transfer", "Cryptohopper Trading Platform",  # company words
    ]
    for name in media_or_company:
        assert looks_like_media_name(name) or looks_like_company(name), \
            f"keyword heuristic should flag '{name}'"
        got = sc._detect_influencer({"name": name, "engagement_quality": 9})
        assert got is False, f"'{name}' should NOT be is_influencer (brand/media), got {got}"
    print("PASS: keyword fallback gates media/company names out of is_influencer.")


def test_influencer_fallback_no_false_positive_on_individuals() -> None:
    """Real individual names must NOT trip the media/company heuristic, and with
    solid engagement they read as influencers."""
    sc = CreatorScorer(use_llm_fallback=False)
    individuals = ["Crypto Casey", "Andreas Antonopoulos", "Coin Bureau",
                   "Layah Heilpern", "MrBeast"]
    for name in individuals:
        assert not looks_like_media_name(name), f"false positive (media) on '{name}'"
        assert not looks_like_company(name), f"false positive (company) on '{name}'"
        got = sc._detect_influencer({"name": name, "engagement_quality": 8})
        assert got is True, f"'{name}' should read as an individual influencer, got {got}"
    print("PASS: no false positives on real individual names.")


def test_influencer_fallback_engagement_gate() -> None:
    """The engagement gate is upstream of the name check: weak engagement -> False
    regardless of the name."""
    sc = CreatorScorer(use_llm_fallback=False)
    assert sc._detect_influencer({"name": "Crypto Casey", "engagement_quality": 4}) is False
    assert sc._detect_influencer({"name": "Crypto Casey", "engagement_quality": 5}) is True
    print("PASS: engagement gate (eq>=5) enforced before the name check.")


def test_influencer_fallback_known_blindspot_is_llm_territory() -> None:
    """HONEST documentation of the keyword fallback's known limitation: it does NOT
    catch brandless media names like BTC-ECHO or Altcoin Daily — those have no
    media/company keyword. Catching them is exactly what the LLM classifier
    (_classify_individual_brand) exists for (see the 2026-07 audit), and is
    deliberately NOT part of the deterministic path. This test pins the real
    (limited) behavior so the boundary is explicit, not mistaken for a bug."""
    for name in ["BTC-ECHO", "Altcoin Daily"]:
        assert not looks_like_media_name(name) and not looks_like_company(name), \
            f"keyword heuristic unexpectedly flags '{name}' — update this note if the sets changed"
    print("PASS (documented): keyword fallback does NOT catch BTC-ECHO / Altcoin Daily "
          "— that is the LLM classifier's job, not the deterministic path's.")


if __name__ == "__main__":
    tests = [
        test_composite_all_four_dimensions_equal_weighted,
        test_composite_renormalizes_when_sponsorship_unavailable,
        test_reach_curve_pinned_values,
        test_reach_saturates_above_1M,
        test_sponsorship_gating_uses_real_column,
        test_sponsorship_scorer_values,
        test_score_bands_percentile_cutoffs,
        test_score_bands_shift_with_distribution,
        test_influencer_fallback_catches_keyword_media_and_company,
        test_influencer_fallback_no_false_positive_on_individuals,
        test_influencer_fallback_engagement_gate,
        test_influencer_fallback_known_blindspot_is_llm_territory,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} scorer tests passed.")
