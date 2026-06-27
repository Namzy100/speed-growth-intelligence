"""Generate a structured high-leverage marketing-strategy report via Claude.

Covers underused-but-legitimate, high-leverage growth tactics for a Bitcoin
Lightning payments app: diaspora/remittance community marketing, iGaming
affiliate models, crypto community growth, fintech referral mechanics, and
regulatory-safe guerrilla tactics. Saves to docs/fintech_marketing_strategies.txt.

Run from repo root:  python intelligence/fintech_marketing_strategies.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"

_PROMPT = """\
You are a senior growth strategist advising Speed Wallet, a Bitcoin Lightning \
payments app (segments: remittance/zero-fees, iGaming/instant-deposits, \
crypto-curious/simplicity; markets: US + EU paid, US/Mexico/Brazil influencers).

Write a structured report on HIGH-LEVERAGE, UNDERUSED but LEGITIMATE marketing \
strategies. "Grey area" here means under-exploited / unconventional — NOT \
deceptive, spammy, or non-compliant. Every tactic must be above-board: honest \
claims, disclosed partnerships (FTC/ASA), platform-ToS-compliant, and mindful of \
money-transmission, crypto, and gambling-advertising regulation. Where a tactic \
carries legal/compliance risk, say so explicitly and give the guardrail.

Cover these sections (plain text only — no markdown, asterisks, or bold markers; \
use dash/equals section headers):
1. DIASPORA / REMITTANCE COMMUNITY MARKETING — community-led tactics (WhatsApp/\
Telegram groups, hometown associations, corridor-specific creators, church/\
community events) and how to do them authentically.
2. IGAMING AFFILIATE MODELS — how affiliate/CPA/revshare works in regulated \
iGaming, what's compliant, and the licensing/geo guardrails Speed must respect.
3. CRYPTO COMMUNITY GROWTH — Bitcoin/Lightning-native growth (meetups, \
conferences, "orange-pilling", merchant adoption, sats-back), and where hype \
crosses into non-compliant promotion.
4. FINTECH REFERRAL MECHANICS THAT WORK — referral structures that actually move \
the needle in fintech (double-sided incentives, milestone bonuses), with the \
fraud/abuse and disclosure guardrails.
5. REGULATORY-SAFE GUERRILLA TACTICS — low-cost unconventional tactics that stay \
inside the lines, with the compliance note for each.

For each section give 3-5 concrete, specific tactics with a one-line "why it \
works" and a one-line "guardrail/risk". Be practical and specific to Speed. Keep \
under 900 words."""


def generate() -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2600,
        messages=[{"role": "user", "content": _PROMPT}],
    )
    return resp.content[0].text


def run() -> str:
    print(f"Generating fintech marketing strategies report ({_MODEL})...")
    body = generate()
    bar = "=" * 70
    full = (f"{bar}\nSPEED WALLET — HIGH-LEVERAGE MARKETING STRATEGIES\n"
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{bar}\n\n"
            + body)
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = _DOCS_DIR / "fintech_marketing_strategies.txt"
    path.write_text(full + "\n", encoding="utf-8")
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print(full)
    return full


if __name__ == "__main__":
    try:
        run()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
