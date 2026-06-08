"""Claude-powered outreach brief generator for Speed Wallet creator partnerships."""

import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

_SPEED_CONTEXT = """
Speed is a Bitcoin Lightning wallet built for real-world payments. Key features:
- Zero fees on Lightning transactions
- Instant payments, settled in seconds
- Stablecoin support (USDT, USDC) for those who want stable value
- Speed Stacks — a rewards program that lets users earn sats on everyday purchases
- Available on iOS and Android; designed for everyday people, not just crypto enthusiasts

Core audience segments:
- Remittance senders: people sending money home internationally — fee savings are the hook
- iGaming users: online gamblers and gaming platforms — instant deposits/withdrawals are the hook
- Crypto-curious: mainstream users interested in Bitcoin but not sure where to start — simplicity and utility are the hook
""".strip()

_BRIEF_PROMPT = """
You are a partnerships specialist at Speed writing an outreach brief for a potential creator partner.

Creator profile:
- Name: {name}
- Platform: {platform}
- Followers: {followers:,}
- Primary segment: {segment_tag}
- Niche tags: {niche_tags}
- Content mix: {content_summary}
- Partnership score: {composite_score}/100
- Why they scored this way: {reasoning}

Speed context:
{speed_context}

Write a concise outreach brief (under 250 words) that covers:
1. Why this creator's audience is a natural fit for Speed
2. Which Speed features are most relevant to their audience — tailor to the segment (remittance → fee savings messaging, iGaming → instant deposit/withdrawal speed messaging, crypto-curious → simplicity and utility messaging)
3. Three specific talking points for the partnership pitch
4. A recommended call to action
5. One thing to avoid in the messaging

Write in plain prose. No headers, no bullet points. Sound like a real person wrote it, not a template. Be specific to this creator — reference their actual niche and audience, not generic crypto talking points.
""".strip()


def generate_brief(creator_dict: dict, score_dict: dict) -> str:
    """Generate a personalized outreach brief for a creator using Claude.

    Args:
        creator_dict: Raw creator dict from youtube.py or apify_tiktok.py.
        score_dict:   Score dict returned by CreatorScorer.score().

    Returns:
        A plain-text outreach brief as a string (under 250 words).

    Raises:
        EnvironmentError: If ANTHROPIC_API_KEY is not set.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    niche_tags = creator_dict.get("niche_tags", [])
    crypto_pct = creator_dict.get("crypto_content_pct", 0.0)
    fintech_pct = creator_dict.get("fintech_content_pct", 0.0)
    content_summary = f"~{crypto_pct:.0%} crypto/Bitcoin, ~{fintech_pct:.0%} fintech/payments"

    prompt = _BRIEF_PROMPT.format(
        name=creator_dict.get("name", "Unknown"),
        platform=creator_dict.get("platform", "Unknown"),
        followers=creator_dict.get("followers", 0),
        segment_tag=score_dict.get("segment_tag", "general"),
        niche_tags=", ".join(niche_tags) if niche_tags else "none identified",
        content_summary=content_summary,
        composite_score=score_dict.get("composite_score", 0),
        reasoning=score_dict.get("reasoning", ""),
        speed_context=_SPEED_CONTEXT,
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from creators.scorer import CreatorScorer

    SAMPLE_CREATOR = {
        "name": "CryptoRico",
        "platform": "YouTube",
        "followers": 220_000,
        "engagement_rate": 0.045,
        "engagement_quality": 7,
        "crypto_content_pct": 0.60,
        "fintech_content_pct": 0.15,
        "sponsorship_count": 8,
        "niche_tags": ["bitcoin", "crypto", "lightning", "personal finance", "investing"],
    }

    scorer = CreatorScorer()
    score = scorer.score(SAMPLE_CREATOR)

    print(f"Generating brief for {SAMPLE_CREATOR['name']} ({SAMPLE_CREATOR['platform']})...")
    print(f"Segment: {score['segment_tag']}  Score: {score['composite_score']}/100\n")

    try:
        brief = generate_brief(SAMPLE_CREATOR, score)
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    print("=" * 60)
    print(brief)
    print("=" * 60)
    print(f"\nWord count: ~{len(brief.split())}")
