"""Creator scoring system for Speed Wallet partner discovery."""

import math
from typing import Any


# Keyword sets for Speed's three target audience segments
REMITTANCE_TAGS = {
    "remittance", "diaspora", "expat", "expats", "migrant", "migrants",
    "money transfer", "send money", "forex", "wire transfer",
    "international transfer", "immigrant", "immigrants", "overseas",
}

IGAMING_TAGS = {
    "igaming", "gambling", "casino", "betting", "poker", "slots",
    "esports", "sports betting", "fantasy sports", "online gambling",
    "sportsbook", "online casino",
}

CRYPTO_TAGS = {
    "crypto", "bitcoin", "ethereum", "blockchain", "defi", "web3",
    "nft", "cryptocurrency", "altcoin", "trading", "investing",
    "finance", "personal finance", "fintech", "payments", "lightning",
    "btc", "eth", "satoshi", "hodl", "crypto investing", "crypto trading",
}


class CreatorScorer:
    """Scores social media creators as potential Speed Wallet partners.

    Each creator is evaluated across five dimensions (each /20) for a
    100-point composite score. Call score() with a creator dict to get
    the full breakdown.
    """

    def score(self, creator: dict[str, Any]) -> dict[str, Any]:
        """Score a creator and return a detailed breakdown.

        Args:
            creator: Dict with keys:
                name (str), platform (str), followers (int),
                engagement_rate (float 0–1), engagement_quality (int 1–10),
                crypto_content_pct (float 0–1), fintech_content_pct (float 0–1),
                sponsorship_count (int), niche_tags (list[str]).

        Returns:
            Dict with:
                scores        — dict of 5 dimension scores, each float /20
                composite_score — float /100
                segment_tag   — "remittance" | "iGaming" | "crypto-curious" | "general"
                reasoning     — plain-English summary of the score
        """
        audience_fit, segment_tag, audience_notes = self._score_audience_fit(creator)
        engagement, engagement_notes = self._score_engagement_quality(creator)
        content, content_notes = self._score_content_alignment(creator)
        acquisition, acq_notes = self._score_acquisition_potential(creator, audience_fit)
        sponsorship, sponsor_notes = self._score_sponsorship_history(creator)

        composite = round(
            audience_fit + engagement + content + acquisition + sponsorship, 1
        )

        return {
            "name": creator["name"],
            "platform": creator["platform"],
            "scores": {
                "audience_fit": round(audience_fit, 1),
                "engagement_quality": round(engagement, 1),
                "content_alignment": round(content, 1),
                "acquisition_potential": round(acquisition, 1),
                "sponsorship_history": round(sponsorship, 1),
            },
            "composite_score": composite,
            "segment_tag": segment_tag,
            "reasoning": self._build_reasoning(
                creator, composite, segment_tag,
                audience_notes, engagement_notes,
                content_notes, acq_notes, sponsor_notes,
            ),
        }

    # ------------------------------------------------------------------
    # Dimension scorers
    # ------------------------------------------------------------------

    def _score_audience_fit(self, creator: dict) -> tuple[float, str, str]:
        """How well the creator's audience matches Speed's core segments (0–20)."""
        tags = {t.lower() for t in creator.get("niche_tags", [])}
        crypto_pct = creator.get("crypto_content_pct", 0)
        fintech_pct = creator.get("fintech_content_pct", 0)

        # Content percentages provide a secondary signal when tags are sparse
        raw_scores = {
            "remittance": len(tags & REMITTANCE_TAGS) * 5 + fintech_pct * 3,
            "iGaming": len(tags & IGAMING_TAGS) * 5,
            "crypto-curious": len(tags & CRYPTO_TAGS) * 3.5 + crypto_pct * 6 + fintech_pct * 2,
        }

        best = max(raw_scores, key=raw_scores.get)
        raw = raw_scores[best]

        if raw < 2.0:
            return min(raw, 2.0), "general", "low segment signal"

        score = min(raw, 20.0)
        return score, best, f"{best} ({raw:.1f} raw)"

    def _score_engagement_quality(self, creator: dict) -> tuple[float, str]:
        """Real vs. inflated engagement, combining quality score and rate (0–20)."""
        eq = creator.get("engagement_quality", 5)
        er = creator.get("engagement_rate", 0)

        eq_pts = (eq / 10) * 10  # quality score 1–10 → 1–10 pts

        # Tiered ER benchmarks valid across TikTok/YouTube/Instagram
        if er >= 0.08:
            er_pts = 10.0
        elif er >= 0.05:
            er_pts = 8.0
        elif er >= 0.03:
            er_pts = 6.0
        elif er >= 0.01:
            er_pts = 4.0
        elif er >= 0.005:
            er_pts = 2.0
        else:
            er_pts = 0.5

        return eq_pts + er_pts, f"EQ={eq}/10, ER={er:.1%}"

    def _score_content_alignment(self, creator: dict) -> tuple[float, str]:
        """How much content touches crypto/fintech/payments (0–20)."""
        crypto = creator.get("crypto_content_pct", 0)
        fintech = creator.get("fintech_content_pct", 0)

        # Crypto is the primary signal (Bitcoin Lightning app); fintech adds a half-weight boost
        score = min(crypto + fintech * 0.5, 1.0) * 20
        return score, f"crypto={crypto:.0%}, fintech={fintech:.0%}"

    def _score_acquisition_potential(
        self, creator: dict, audience_fit_score: float
    ) -> tuple[float, str]:
        """Predicted install volume proxy: followers × ER × fit ratio (0–20)."""
        followers = creator.get("followers", 0)
        er = creator.get("engagement_rate", 0)
        fit_ratio = audience_fit_score / 20.0

        raw = followers * er * fit_ratio
        if raw <= 0:
            return 0.0, "zero (no followers / ER / fit)"

        # Log scale anchored at 100K weighted engaged reach → 20/20
        log_score = math.log10(raw) / math.log10(100_000) * 20
        score = max(0.0, min(log_score, 20.0))
        return score, f"~{raw:,.0f} weighted engaged reach"

    def _score_sponsorship_history(self, creator: dict) -> tuple[float, str]:
        """Experience with brand deals, on a log2 curve (0–20)."""
        count = creator.get("sponsorship_count", 0)
        if count == 0:
            return 0.0, "no prior brand deals"

        # log2(count+1) / log2(21): 1 deal→4.5, 5→11.5, 10→15.5, 20→20
        score = min(math.log2(count + 1) / math.log2(21) * 20, 20.0)
        return score, f"{count} brand deal(s)"

    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        creator: dict,
        composite: float,
        segment_tag: str,
        audience_notes: str,
        engagement_notes: str,
        content_notes: str,
        acq_notes: str,
        sponsor_notes: str,
    ) -> str:
        tier = "strong" if composite >= 75 else "moderate" if composite >= 50 else "weak"
        return (
            f"{creator['name']} is a {tier} Speed partner candidate ({composite}/100). "
            f"Segment: {segment_tag}. "
            f"Audience fit: {audience_notes}. "
            f"Engagement: {engagement_notes}. "
            f"Content: {content_notes}. "
            f"Reach: {acq_notes}. "
            f"Sponsorships: {sponsor_notes}."
        )


# ------------------------------------------------------------------
# Quick smoke test
# ------------------------------------------------------------------

if __name__ == "__main__":
    samples = [
        {
            "name": "CryptoRico",
            "platform": "YouTube",
            "followers": 220_000,
            "engagement_rate": 0.045,
            "engagement_quality": 7,
            "crypto_content_pct": 0.60,
            "fintech_content_pct": 0.15,
            "sponsorship_count": 8,
            "niche_tags": ["bitcoin", "crypto", "lightning", "personal finance", "investing"],
        },
        {
            "name": "DiasporaDaily",
            "platform": "TikTok",
            "followers": 85_000,
            "engagement_rate": 0.07,
            "engagement_quality": 8,
            "crypto_content_pct": 0.10,
            "fintech_content_pct": 0.40,
            "sponsorship_count": 3,
            "niche_tags": ["remittance", "expats", "send money", "diaspora"],
        },
        {
            "name": "BetKing247",
            "platform": "Instagram",
            "followers": 310_000,
            "engagement_rate": 0.025,
            "engagement_quality": 5,
            "crypto_content_pct": 0.05,
            "fintech_content_pct": 0.05,
            "sponsorship_count": 15,
            "niche_tags": ["igaming", "sports betting", "casino", "poker"],
        },
    ]

    scorer = CreatorScorer()

    for creator in samples:
        result = scorer.score(creator)
        print(f"\n{'=' * 55}")
        print(f"Creator  : {result['name']} ({result['platform']})")
        print(f"Segment  : {result['segment_tag']}")
        print(f"Composite: {result['composite_score']} / 100")
        print("Breakdown:")
        for dim, val in result["scores"].items():
            bar = "█" * int(val / 20 * 20)
            print(f"  {dim:<26} {val:>4} / 20  {bar}")
        print(f"\n{result['reasoning']}")
