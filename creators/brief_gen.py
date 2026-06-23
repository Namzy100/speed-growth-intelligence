"""Claude-powered outreach brief generator for Speed Wallet creator partnerships."""

import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
# Database-driven generation: top creators -> briefs on disk
# ------------------------------------------------------------------

_BRIEFS_DIR = _ROOT / "docs" / "creator_briefs"


def _slug(name: str) -> str:
    """Filesystem-safe handle from a creator name ('Matt's Crypto' -> 'matts_crypto')."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "creator"


def generate_brief_for_row(row: dict) -> str:
    """Generate a brief from a flat Supabase creator row.

    DB rows already carry both the raw creator fields (name, followers,
    niche_tags, *_content_pct) and the scoring fields (segment_tag,
    composite_score, reasoning), so the same row satisfies both arguments of
    generate_brief().
    """
    return generate_brief(row, row)


def run(top_n: int = 5) -> None:
    """Generate outreach briefs for the top-N creators by composite score,
    print each, and save to docs/creator_briefs/<handle>.txt."""
    from creators import database

    rows = database.get_all_creators()  # ordered by composite_score desc
    if not rows:
        print("No creators in the database — nothing to generate.")
        return

    top = rows[:top_n]
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Generating outreach briefs for the top {len(top)} creators "
          f"by composite score...\n")
    for i, r in enumerate(top, 1):
        name = r.get("name", "Unknown")
        brief = generate_brief_for_row(r)
        header = (
            f"Outreach Brief — {name} ({r.get('platform', '')})\n"
            f"Segment: {r.get('segment_tag', '')} | "
            f"Score: {r.get('composite_score', '')}/100 | "
            f"{int(r.get('followers', 0)):,} followers\n"
            + "-" * 60 + "\n"
        )
        path = _BRIEFS_DIR / f"{_slug(name)}.txt"
        path.write_text(header + brief + "\n", encoding="utf-8")

        print("=" * 70)
        print(f"[{i}] {header}{brief}")
        print(f"\nSaved: {path.relative_to(_ROOT)}")
    print("=" * 70)
    print(f"\nDone — {len(top)} briefs written to {_BRIEFS_DIR.relative_to(_ROOT)}/")


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
