"""Creator scoring system for Speed Wallet partner discovery."""

import math
import os
import re
from typing import Any

# LLM fallback classifier: when rule-based scoring finds no segment match (the
# tag would be "general"), a single cheap Claude call classifies the creator
# from its name/description/tags. Kept to Haiku and one call per general creator
# so cost stays negligible.
_FALLBACK_MODEL = "claude-haiku-4-5"


# Keyword sets for Speed's three target audience segments
REMITTANCE_TAGS = {
    "remittance", "diaspora", "expat", "expats", "migrant", "migrants",
    "money transfer", "send money", "forex", "wire transfer",
    "international transfer", "immigrant", "immigrants", "overseas",
    # Spanish/Portuguese terms for the Mexico & Brazil diaspora corridors
    "remesas", "enviar dinero", "mandar dinero", "mandar dinheiro",
    "remessa", "transferencia internacional", "transferência internacional",
    "dinero al extranjero", "dinheiro para o exterior",
    "envio de dinero", "envio de dinheiro",
}

IGAMING_TAGS = {
    "igaming", "gambling", "casino", "betting", "poker", "slots",
    "esports", "sports betting", "sports bet", "esports betting",
    "fantasy sports", "online gambling", "sportsbook", "online casino",
    "wager", "play to earn", "p2e",
}

# Unambiguous iGaming signals. If a creator carries ANY of these in their
# niche_tags, the segment is forced to iGaming regardless of how high their
# crypto_content_pct would otherwise push the crypto-curious score — a clear
# casino/betting/gambling channel is iGaming first, crypto-adjacent second.
IGAMING_PRECEDENCE_TAGS = {
    "casino", "betting", "gambling", "poker", "wager", "play to earn", "p2e",
}

CRYPTO_TAGS = {
    "crypto", "bitcoin", "ethereum", "blockchain", "defi", "web3",
    "nft", "cryptocurrency", "altcoin", "trading", "investing",
    "finance", "personal finance", "fintech", "payments", "lightning",
    "btc", "eth", "satoshi", "hodl", "crypto investing", "crypto trading",
}

# Niche-tag signals of a real person / lifestyle creator (vs a media/finance
# outlet). Includes YouTube topic-category style tags.
PERSONAL_TAGS = {
    "lifestyle", "lifestyle (sociology)", "vlog", "vlogging", "entertainment",
    "society", "family", "comedy", "travel", "tourism", "fitness", "food",
    "music", "sport", "association football", "hobby", "humour", "humor",
}

# Name patterns that signal a media / news / educational / brand outlet rather
# than a personal creator. Multi-word substrings + short word-boundary tokens.
_MEDIA_SUBSTRINGS = ("daily news", "news today", "news network", "media", "magazine", "press")
_MEDIA_WORDS = ("news", "tv", "channel", "official", "academy", "university", "institute")


def looks_like_media_name(name: str) -> bool:
    """True if the channel name reads like a media/news/brand outlet, not a person."""
    n = (name or "").lower()
    if any(s in n for s in _MEDIA_SUBSTRINGS):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in _MEDIA_WORDS)


def detect_influencer(creator: dict) -> bool:
    """Is this a personal influencer (vs a finance/news/brand channel)?

    True when engagement is high (>3%) AND the channel is not a media/brand
    outlet by name — a proxy for "single person, lifestyle/personal content".
    """
    er = float(creator.get("engagement_rate", 0) or 0)
    if er <= 0.03:
        return False
    return not looks_like_media_name(creator.get("name", ""))


def influencer_score(creator: dict) -> float:
    """0-100 influencer fit, weighted to engagement + personal brand + authentic
    audience size (NOT content-keyword matching).

    Engagement 50% (cap 10% ER -> 100), personal-brand 30% (penalise media name,
    reward lifestyle tags), authentic audience 20% (mid-size sweet spot).
    """
    er = float(creator.get("engagement_rate", 0) or 0)
    followers = int(creator.get("followers", 0) or 0)
    tags = {str(t).lower() for t in creator.get("niche_tags", [])}
    name = creator.get("name", "")

    eng = min(er / 0.10, 1.0) * 100
    pb = (0 if looks_like_media_name(name) else 60) + (40 if tags & PERSONAL_TAGS else 0)
    if followers < 5_000:
        aud = 40.0
    elif followers < 500_000:
        aud = 100.0
    elif followers < 2_000_000:
        aud = 70.0
    else:
        aud = 50.0
    return round(0.5 * eng + 0.3 * min(pb, 100) + 0.2 * aud, 1)


class CreatorScorer:
    """Scores social media creators as potential Speed Wallet partners.

    Each creator is evaluated across five dimensions (each /20) for a
    100-point composite score. Call score() with a creator dict to get
    the full breakdown.
    """

    def __init__(self, use_llm_fallback: bool = True) -> None:
        """Args:
            use_llm_fallback: when True, a creator the rule-based scorer would
                tag "general" gets one cheap Claude (Haiku) classification
                attempt from its name/description/tags. Set False to keep
                scoring fully offline and deterministic (e.g. in tests).
        """
        self._use_llm_fallback = use_llm_fallback
        self._llm_client = None  # lazy-initialised on first fallback call

    def score(self, creator: dict[str, Any]) -> dict[str, Any]:
        """Score a creator and return a detailed breakdown.

        Args:
            creator: Dict with keys:
                name (str), platform (str), followers (int),
                engagement_rate (float 0–1), engagement_quality (int 1–10),
                crypto_content_pct (float 0–1), fintech_content_pct (float 0–1),
                sponsorship_count (int), niche_tags (list[str]),
                purchase_intent_signals (float 0–1, default 0.5).

        Returns:
            Dict with:
                scores               — dict of dimension scores, each float /20
                deposit_relevance_score — float /20; likelihood of USD deposit completion
                composite_score      — float /100 (audience_fit + engagement +
                                       content_alignment + deposit_relevance + sponsorship)
                segment_tag          — "remittance" | "iGaming" | "crypto-curious" | "general"
                reasoning            — plain-English summary of the score
        """
        audience_fit, segment_tag, audience_notes = self._score_audience_fit(creator)

        # LLM fallback: only when the rule-based scorer found no segment signal.
        # The numeric audience_fit score is left untouched (it correctly reflects
        # weak *measurable* signal); only the segment_tag label is reclassified.
        if segment_tag == "general" and self._use_llm_fallback:
            llm_tag = self._classify_general_llm(creator)
            if llm_tag and llm_tag != "general":
                segment_tag = llm_tag
                audience_notes += f"; LLM-reclassified to '{llm_tag}'"

        engagement, engagement_notes = self._score_engagement_quality(creator)
        content, content_notes = self._score_content_alignment(creator)
        acquisition, acq_notes = self._score_acquisition_potential(creator, audience_fit)
        sponsorship, sponsor_notes = self._score_sponsorship_history(creator)
        deposit_rel, deposit_notes = self._score_deposit_relevance(creator, audience_fit, content)

        # acquisition_potential is retained in scores for reference but excluded from
        # the composite; deposit_relevance_score fills that 20-point slot because
        # USD deposits — not installs — are Speed's primary conversion goal.
        composite = round(
            audience_fit + engagement + content + deposit_rel + sponsorship, 1
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
            "deposit_relevance_score": round(deposit_rel, 1),
            "composite_score": composite,
            "segment_tag": segment_tag,
            # Influencer signals — separate from the finance-weighted composite.
            "is_influencer": detect_influencer(creator),
            "influencer_score": influencer_score(creator),
            "reasoning": self._build_reasoning(
                creator, composite, segment_tag,
                audience_notes, engagement_notes,
                content_notes, acq_notes, sponsor_notes, deposit_notes,
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

        # Segment precedence: a clear iGaming signal wins outright. Without this,
        # a crypto-heavy casino/betting channel gets pulled into crypto-curious
        # by crypto_content_pct even though it plainly belongs in iGaming.
        if tags & IGAMING_PRECEDENCE_TAGS:
            raw = raw_scores["iGaming"]
            return min(raw, 20.0), "iGaming", f"iGaming (forced by clear tag, {raw:.1f} raw)"

        best = max(raw_scores, key=raw_scores.get)
        raw = raw_scores[best]

        if raw < 2.0:
            return min(raw, 2.0), "general", "low segment signal"

        score = min(raw, 20.0)
        return score, best, f"{best} ({raw:.1f} raw)"

    def _classify_general_llm(self, creator: dict) -> str | None:
        """Classify a would-be-"general" creator via one cheap Claude call.

        Fires only as a fallback when keyword/percentage scoring found no segment
        signal. Returns one of the three segments, "general", or None if the call
        can't run (no API key / SDK missing / API error) — callers keep "general"
        in those cases.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        name = creator.get("name", "")
        description = (creator.get("description") or "")[:600]
        tags = ", ".join(creator.get("niche_tags", [])) or "(none)"

        prompt = (
            "Classify this social-media creator into ONE audience segment for "
            "Speed Wallet, a Bitcoin Lightning payments app.\n\n"
            "Segments:\n"
            "- remittance: audience sends money across borders — diaspora, "
            "migrants, money transfer, remesas/remessas, international transfers\n"
            "- iGaming: online gambling, casino, betting, poker, sports betting\n"
            "- crypto-curious: mainstream people interested in buying, using, or "
            "learning about crypto/Bitcoin; crypto investing or education\n"
            "- general: none of the above, or unrelated to money/crypto/gambling\n\n"
            f"Creator name: {name}\n"
            f"Description: {description}\n"
            f"Tags: {tags}\n\n"
            "Reply with EXACTLY one of: remittance, iGaming, crypto-curious, "
            "general. No other text."
        )

        try:
            if self._llm_client is None:
                from anthropic import Anthropic
                self._llm_client = Anthropic(api_key=api_key)
            resp = self._llm_client.messages.create(
                model=_FALLBACK_MODEL,
                max_tokens=16,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip().lower().strip("\"'.")
        except Exception:
            return None

        # Normalise the reply to a canonical segment label.
        if "remittance" in text:
            return "remittance"
        if "igaming" in text or "gaming" in text or "gambl" in text:
            return "iGaming"
        if "crypto" in text:
            return "crypto-curious"
        return "general"

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

    # Above this count, the "sponsorships" are almost certainly a brand's own
    # ad-flagged product videos (self-promotion), not third-party brand deals.
    # A real creator's brand-deal history rarely exceeds this. We refuse to let
    # self-promotion max out the signal — it gets a fixed, modest score instead.
    _SELF_PROMO_THRESHOLD = 50
    _SELF_PROMO_SCORE = 6.0

    def _score_sponsorship_history(self, creator: dict) -> tuple[float, str]:
        """Experience with *third-party* brand deals, on a log2 curve (0–20)."""
        count = creator.get("sponsorship_count", 0)
        if count == 0:
            return 0.0, "no prior brand deals"

        if count > self._SELF_PROMO_THRESHOLD:
            # Implausibly high → self-promotion artifact, not partnership track record.
            return self._SELF_PROMO_SCORE, (
                f"{count} ad-flagged videos — likely self-promotion, "
                f"down-weighted (not third-party deals)"
            )

        # log2(count+1) / log2(21): 1 deal→4.5, 5→11.5, 10→15.5, 20→20
        score = min(math.log2(count + 1) / math.log2(21) * 20, 20.0)
        return score, f"{count} brand deal(s)"

    def _score_deposit_relevance(
        self, creator: dict, audience_fit_score: float, content_score: float
    ) -> tuple[float, str]:
        """Likelihood that this creator's audience will complete a USD deposit (0–20).

        Combines three signals:
        - purchase_intent_signals (0–1): how often the creator's content discusses
          actually buying/using crypto vs. just talking about it abstractly.
          Defaults to 0.5 if not provided.
        - content_alignment: understanding of crypto/fintech predicts KYC completion.
        - audience_fit: segment match predicts deposit motivation.
        """
        intent = float(creator.get("purchase_intent_signals", 0.5))
        intent = max(0.0, min(1.0, intent))  # clamp to valid range

        fit_ratio = audience_fit_score / 20.0
        content_ratio = content_score / 20.0

        # Intent is the strongest deposit predictor (40%); content second (35%); fit third (25%).
        raw = intent * 0.40 + content_ratio * 0.35 + fit_ratio * 0.25
        score = round(raw * 20, 1)
        return score, f"intent={intent:.0%}, content={content_ratio:.0%}, fit={fit_ratio:.0%}"

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
        deposit_notes: str,
    ) -> str:
        tier = "strong" if composite >= 75 else "moderate" if composite >= 50 else "weak"
        return (
            f"{creator['name']} is a {tier} Speed partner candidate ({composite}/100). "
            f"Segment: {segment_tag}. "
            f"Audience fit: {audience_notes}. "
            f"Engagement: {engagement_notes}. "
            f"Content: {content_notes}. "
            f"Deposit relevance: {deposit_notes}. "
            f"Reach (ref): {acq_notes}. "
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
            "purchase_intent_signals": 0.75,  # regularly covers buying/stacking BTC
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
            "purchase_intent_signals": 0.60,  # audience already transacts; some crypto crossover
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
            "purchase_intent_signals": 0.35,  # gambling focus; low crypto buying intent
        },
    ]

    scorer = CreatorScorer()

    for creator in samples:
        result = scorer.score(creator)
        print(f"\n{'=' * 55}")
        print(f"Creator  : {result['name']} ({result['platform']})")
        print(f"Segment  : {result['segment_tag']}")
        print(f"Composite: {result['composite_score']} / 100")
        print(f"Deposit relevance: {result['deposit_relevance_score']} / 20")
        print("Breakdown (scores dict):")
        for dim, val in result["scores"].items():
            bar = "█" * int(val / 20 * 20)
            note = " (ref only)" if dim == "acquisition_potential" else ""
            print(f"  {dim:<26} {val:>4} / 20  {bar}{note}")
        print(f"\n{result['reasoning']}")
