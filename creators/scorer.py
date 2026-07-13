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

# Name patterns that signal a media / news / educational / brand outlet rather
# than a personal creator. Multi-word substrings + short word-boundary tokens.
_MEDIA_SUBSTRINGS = ("daily news", "news today", "news network", "media", "magazine", "press")
_MEDIA_WORDS = ("news", "tv", "channel", "official", "academy", "university", "institute")

# Name tokens that signal a COMPANY / product / tool rather than a person — e.g.
# "ACE Money Transfer", "Cryptohopper - Automated Crypto Trading Platform".
_COMPANY_WORDS = ("transfer", "platform", "trading", "bot", "exchange", "software")


def looks_like_media_name(name: str) -> bool:
    """True if the channel name reads like a media/news/brand outlet, not a person."""
    n = (name or "").lower()
    if any(s in n for s in _MEDIA_SUBSTRINGS):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in _MEDIA_WORDS)


def looks_like_company(name: str) -> bool:
    """True if the name reads like a company/product/tool (not an individual)."""
    n = (name or "").lower()
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in _COMPANY_WORDS)


def _real_engagement(creator: dict) -> float:
    """A real interaction-engagement signal in 0-1, from engagement_quality.

    engagement_quality (1-10) is derived from (likes+comments)/views in the
    fetchers, so it reflects genuine interaction — unlike the legacy
    engagement_rate (views/followers, capped at 1.0), which view-gaming inflates.
    Mapped roughly: eq 10 -> 1.0, eq 5 -> ~0.4. Falls back to a damped legacy
    engagement_rate only when engagement_quality is absent.
    """
    eq = creator.get("engagement_quality")
    if eq is not None:
        return max(0.0, (float(eq) - 1) / 9.0)
    # Fallback: damp the legacy metric hard so it can't dominate.
    return min(float(creator.get("engagement_rate", 0) or 0), 0.3)


def detect_influencer(creator: dict) -> bool:
    """Is this a personal influencer (vs a finance/news/brand/company channel)?

    True when GENUINE engagement is solid (engagement_quality >= 5, i.e. a real
    interaction rate, not view-gamed) AND the name isn't a media or company
    outlet — a proxy for "single person, lifestyle/personal content".
    """
    name = creator.get("name", "")
    if looks_like_media_name(name) or looks_like_company(name):
        return False
    eq = creator.get("engagement_quality")
    if eq is not None:
        return float(eq) >= 5
    # Fallback for records without engagement_quality: require strong legacy ER.
    return float(creator.get("engagement_rate", 0) or 0) > 0.03


def influencer_score(creator: dict) -> float:
    """0-100 influencer fit from authentic-audience signals only: real engagement
    and audience size, weighted 50/50.

    The individual-vs-brand/media call is deliberately NOT recomputed here — that
    is what detect_influencer() / is_influencer already does (and it's surfaced
    directly in the UI). The old 40%-weighted "personal-brand" component was
    dropped in the 2026-07 audit for two reasons: its name string-match merely
    recycled the is_influencer classifier (double-counting), and its +40
    lifestyle bonus was driven by YouTube auto topic-categories (society, tourism,
    football) rather than any real lifestyle signal — it handed the max score to
    brand/media accounts like BTC-ECHO and Altcoin Daily. See the deposit_relevance
    removal for the same class of fix.
    """
    followers = int(creator.get("followers", 0) or 0)

    # Engagement component: real interaction signal (from engagement_quality).
    eng = _real_engagement(creator) * 100  # 0-100

    # Authentic audience (mid-size sweet spot).
    if followers < 5_000:
        aud = 40.0
    elif followers < 500_000:
        aud = 100.0
    elif followers < 2_000_000:
        aud = 70.0
    else:
        aud = 50.0

    return round(0.50 * eng + 0.50 * aud, 1)


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
                sponsorship_data_available (bool): True only when real sponsorship
                    data was measured for this creator. When False, sponsorship is
                    excluded from the composite and its weight redistributed.

        Returns:
            Dict with:
                scores               — dict of dimension scores, each float /20
                deposit_relevance_score — None (dimension removed in 2026-07 audit)
                composite_score      — float /100, renormalised over the dimensions
                                       that carry real signal: audience_fit +
                                       engagement + reach (+ sponsorship only when
                                       sponsorship_data_available)
                composite_basis      — list of the dimensions that fed the score
                sponsorship_data_available — bool echoed from input
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
        reach, reach_notes = self._score_reach(creator)
        sponsorship, sponsor_notes = self._score_sponsorship_history(creator)

        # --- Composite (revised again 2026-07: 4 real dimensions) ---
        # Each included dimension is /20. We score ONLY the dimensions that carry
        # real signal for this creator, then renormalise the total back to /100 so
        # creators scored on a different number of dimensions stay comparable.
        # Weighting is EQUAL across the included dimensions.
        #
        #   audience_fit · engagement · reach · sponsorship (where measured)
        #
        # reach (follower size, pure — see _score_reach) is NOW in the composite;
        # it used to be reference-only. It is deliberately decoupled from ER/fit so
        # it doesn't recycle the engagement / audience_fit dimensions.
        #
        # content_alignment was DROPPED from the composite: it reads the same
        # crypto/fintech keyword signal off the same text as audience_fit, so it
        # was double-counting. It is still computed + stored for reference, just
        # not scored into the composite.
        #
        # sponsorship_history is included ONLY when real sponsorship data was
        # measured (sponsorship_data_available); otherwise it is EXCLUDED and its
        # weight redistributed — a hardcoded 0 is indistinguishable from a
        # verified-low value.
        #
        # deposit_relevance stays removed (no real proxy signal exists yet).
        # scraped_data_available: False only for creators bulk-imported with
        # placeholder engagement/content and that CANNOT be re-fetched (the
        # Mimanshi Instagram/X set — no fetcher exists for those platforms). For
        # them audience_fit + engagement are fabricated (engagement_quality=7,
        # crypto_content_pct=0.8), so they are EXCLUDED; the composite is built
        # from the dims backed by real data only — reach (from real follower
        # counts) plus sponsorship where measured. Default True (normal scraped
        # creators have real engagement/content). Fit rating carries their vetting
        # separately (tiebreaker + visible badge), so it is not lost.
        scraped_available = bool(creator.get("scraped_data_available", True))
        spons_available = bool(creator.get("sponsorship_data_available", False))
        dims = [reach]
        basis = ["reach"]
        if scraped_available:
            dims[:0] = [audience_fit, engagement]     # prepend to keep readable order
            basis[:0] = ["audience_fit", "engagement"]
        if spons_available:
            dims.append(sponsorship)
            basis.append("sponsorship")
        composite = round(sum(dims) * 100.0 / (20.0 * len(dims)), 1)

        return {
            "name": creator["name"],
            "platform": creator["platform"],
            "scores": {
                "audience_fit": round(audience_fit, 1),
                "engagement_quality": round(engagement, 1),
                # Computed + stored for reference but NOT in the composite (dropped
                # 2026-07 — redundant with audience_fit).
                "content_alignment": round(content, 1),
                # This column now holds the REACH dimension (pure follower size),
                # which IS part of the composite. Name kept to avoid a migration.
                "acquisition_potential": round(reach, 1),
                "sponsorship_history": round(sponsorship, 1),
            },
            # deposit_relevance is no longer scored (removed from the composite,
            # pending a real signal). Explicit None so it is never mistaken for a 0.
            "deposit_relevance_score": None,
            "composite_score": composite,
            "composite_basis": basis,               # which dims actually fed the score
            "sponsorship_data_available": spons_available,
            "scraped_data_available": scraped_available,
            "segment_tag": segment_tag,
            # Influencer signals — separate from the finance-weighted composite.
            "is_influencer": self._detect_influencer(creator),
            "influencer_score": influencer_score(creator),
            "reasoning": self._build_reasoning(
                creator, composite, segment_tag, spons_available,
                audience_notes, engagement_notes,
                reach_notes, sponsor_notes,
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

        # Tag-match counts are capped at 3 before weighting: scraped channels
        # carry many near-synonym topic tags (bitcoin + btc + crypto +
        # cryptocurrency + blockchain + eth …) that all match the same segment,
        # which used to stack 3.5pts each and pin audience_fit at the ceiling.
        # Capping makes a tag-spammed scrape and a human-curated canonical tag
        # set score the same from tags; crypto_content_pct then differentiates.
        _CAP = 3
        crypto_hits = min(len(tags & CRYPTO_TAGS), _CAP)
        remit_hits = min(len(tags & REMITTANCE_TAGS), _CAP)
        igaming_hits = min(len(tags & IGAMING_TAGS), _CAP)

        # Content percentages provide a secondary signal when tags are sparse
        raw_scores = {
            "remittance": remit_hits * 5 + fintech_pct * 3,
            "iGaming": igaming_hits * 5,
            "crypto-curious": crypto_hits * 3.5 + crypto_pct * 6 + fintech_pct * 2,
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

    def _classify_individual_brand(self, creator: dict) -> bool | None:
        """Coarse individual-vs-brand call via one cheap Claude (Haiku) call.

        Returns True for an INDIVIDUAL (single person / personal creator / named
        persona), False for a BRAND (company, product, app, exchange, publication,
        or media/news outlet), or None if the call can't run (no API key / SDK
        missing / API error) — callers then fall back to the rule-based name check.

        Replaces the keyword name-heuristic for the individual/brand half of
        detect_influencer. Keyword matching cannot separate surface-identical
        names like "Crypto Wall Street" (brand) from "Crypto Casey" (person) —
        that is a world-knowledge judgment. This is a coarse categorical identity
        call, not a fabricated numeric signal. See the 2026-07 influencer audit.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        name = creator.get("name", "")
        description = (creator.get("description") or "")[:400]
        tags = ", ".join(creator.get("niche_tags", [])) or "(none)"

        prompt = (
            "You are labelling a social-media account as either INDIVIDUAL or "
            "BRAND.\n"
            "- INDIVIDUAL: a single real person, personal creator, or a named "
            "persona who is the face of the channel (even if the name contains "
            "words like trading, news, crypto, or a show title).\n"
            "- BRAND: a company, product, app, exchange, publication, or "
            "media/news outlet — an organization rather than one person.\n\n"
            f"Account name: {name}\n"
            f"Description: {description}\n"
            f"Niche tags: {tags}\n\n"
            "Reply with EXACTLY one word: INDIVIDUAL or BRAND."
        )

        try:
            if self._llm_client is None:
                from anthropic import Anthropic
                self._llm_client = Anthropic(api_key=api_key)
            resp = self._llm_client.messages.create(
                model=_FALLBACK_MODEL,
                max_tokens=8,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip().upper().strip("\"'.")
        except Exception:
            return None

        if "INDIVIDUAL" in text:
            return True
        if "BRAND" in text:
            return False
        return None

    def _detect_influencer(self, creator: dict) -> bool:
        """Instance detect_influencer: same engagement gate as the module-level
        rule-based version, but the individual-vs-brand half uses the LLM
        classifier when use_llm_fallback is on (falling back to the keyword name
        heuristic when the LLM is unavailable or disabled).
        """
        # Engagement gate — identical to the rule-based detect_influencer.
        eq = creator.get("engagement_quality")
        if eq is not None:
            eng_ok = float(eq) >= 5
        else:
            eng_ok = float(creator.get("engagement_rate", 0) or 0) > 0.03
        if not eng_ok:
            return False

        # Individual-vs-brand: LLM when enabled, else the rule-based name check.
        if self._use_llm_fallback:
            verdict = self._classify_individual_brand(creator)
            if verdict is not None:
                return verdict
        name = creator.get("name", "")
        return not (looks_like_media_name(name) or looks_like_company(name))

    # An engagement_rate above this is treated as a view-gaming / data artifact
    # rather than genuine reach. The legacy engagement_rate (views/followers,
    # capped at 1.0) is easily inflated — e.g. scraped channels carrying ER of
    # 0.35 or even 1.0 — so we refuse to reward it and lean on the trustworthy
    # engagement_quality signal instead.
    _ARTIFACT_ER = 0.15

    def _score_engagement_quality(self, creator: dict) -> tuple[float, str]:
        """Real vs. inflated engagement, weighted to genuine interaction (0–20)."""
        eq = creator.get("engagement_quality", 5)
        er = creator.get("engagement_rate", 0)

        # engagement_quality (a real interaction signal) now carries most of the
        # weight (up to 15 pts) — it can't be gamed by inflating views/followers.
        eq_pts = (eq / 10) * 15

        # engagement_rate is only a small corroborating bonus, and ONLY when it's
        # plausible. An artifact-high rate (> _ARTIFACT_ER) earns nothing — it
        # signals view-gaming, not engaged reach.
        if er > self._ARTIFACT_ER:
            er_pts = 0.0
        elif er >= 0.05:
            er_pts = 5.0
        elif er >= 0.03:
            er_pts = 4.0
        elif er >= 0.01:
            er_pts = 3.0
        elif er >= 0.005:
            er_pts = 1.5
        else:
            er_pts = 0.0

        return min(eq_pts + er_pts, 20.0), f"EQ={eq}/10, ER={er:.1%}"

    def _score_content_alignment(self, creator: dict) -> tuple[float, str]:
        """How much content touches crypto/fintech/payments (0–20)."""
        crypto = creator.get("crypto_content_pct", 0)
        fintech = creator.get("fintech_content_pct", 0)

        # Crypto is the primary signal (Bitcoin Lightning app); fintech adds a half-weight boost
        score = min(crypto + fintech * 0.5, 1.0) * 20
        return score, f"crypto={crypto:.0%}, fintech={fintech:.0%}"

    def _score_reach(self, creator: dict) -> tuple[float, str]:
        """Audience reach as an INDEPENDENT signal — follower count alone, on a
        diminishing-returns log curve (0–20).

        Deliberately decoupled from engagement_rate and audience_fit: those are
        their own composite dimensions, and multiplying them in here (as the old
        'acquisition_potential' formula did) would double-count them — the same
        recycling the 2026-07 audit removed from deposit_relevance. Engagement
        quality is scored separately, so reach stays a pure size signal.

        Curve B (chosen 2026-07): (log10(followers) - 3) / 3 * 20 — a 1k floor maps
        to 0 and 1M to a full 20, saturating beyond (a 3M and a 1M creator score
        ~the same: real diminishing returns at the top). Net effect: a 3M creator
        outranks an all-else-equal 30k creator by ~13–17 composite points, no more.
        """
        followers = int(creator.get("followers", 0) or 0)
        if followers < 1_000:
            return 0.0, f"{followers:,} followers (below 1k reach floor)"
        score = max(0.0, min((math.log10(followers) - 3.0) / 3.0 * 20.0, 20.0))
        return score, f"{followers:,} followers"

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

    # NOTE: _score_deposit_relevance was removed in the 2026-07 scoring audit.
    # It combined a never-set purchase_intent_signals=0.5 constant (40%) with
    # recycled content_alignment/audience_fit (60%), so it added no independent
    # signal. `purchase_intent_signals` is no longer read anywhere in the scorer.
    # Reintroduce a real deposit dimension only when a genuine proxy (e.g.
    # referral/CTA/app-download language in fetched captions/descriptions) exists.

    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        creator: dict,
        composite: float,
        segment_tag: str,
        spons_available: bool,
        audience_notes: str,
        engagement_notes: str,
        reach_notes: str,
        sponsor_notes: str,
    ) -> str:
        tier = "strong" if composite >= 75 else "moderate" if composite >= 50 else "weak"
        spons_line = (
            f"Sponsorships: {sponsor_notes}."
            if spons_available
            else "Sponsorships: no data available (excluded from score, weight redistributed)."
        )
        return (
            f"{creator['name']} is a {tier} Speed partner candidate ({composite}/100). "
            f"Segment: {segment_tag}. "
            f"Audience fit: {audience_notes}. "
            f"Engagement: {engagement_notes}. "
            f"Reach: {reach_notes}. "
            f"{spons_line}"
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
            "sponsorship_data_available": True,
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
            "sponsorship_data_available": True,
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
            "sponsorship_data_available": True,
        },
        # A creator with NO measured sponsorship data — sponsorship is excluded
        # from the composite and its weight redistributed across the other dims.
        {
            "name": "NoSponsData",
            "platform": "YouTube",
            "followers": 120_000,
            "engagement_rate": 0.04,
            "engagement_quality": 7,
            "crypto_content_pct": 0.50,
            "fintech_content_pct": 0.10,
            "sponsorship_count": 0,
            "niche_tags": ["bitcoin", "crypto", "investing"],
            "sponsorship_data_available": False,
        },
    ]

    scorer = CreatorScorer()

    for creator in samples:
        result = scorer.score(creator)
        print(f"\n{'=' * 55}")
        print(f"Creator  : {result['name']} ({result['platform']})")
        print(f"Segment  : {result['segment_tag']}")
        print(f"Composite: {result['composite_score']} / 100  "
              f"(basis: {', '.join(result['composite_basis'])})")
        print(f"Sponsorship data available: {result['sponsorship_data_available']}")
        print("Breakdown (scores dict):")
        for dim, val in result["scores"].items():
            bar = "█" * int(val / 20 * 20)
            note = " (ref only)" if dim == "acquisition_potential" else ""
            print(f"  {dim:<26} {val:>4} / 20  {bar}{note}")
        print(f"\n{result['reasoning']}")
