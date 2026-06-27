"""Generate 3 creator-outreach email templates (one per segment) via Claude.

Produces warm, human (non-corporate) outreach templates for Speed Wallet creator
partnerships — remittance, crypto-curious, and iGaming — each with a subject
line, a personalized opening hook, segment value prop, a clear ask, and a CTA.
Saves to docs/outreach_email_templates.txt.

Run from repo root:  python intelligence/outreach_email_templates.py
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
You write creator-partnership outreach emails for Speed Wallet, a Bitcoin \
Lightning payments app. Speed's segments and hooks:
- remittance: zero fees on cross-border money sends (diaspora audiences)
- crypto-curious: the simplest way to actually use/spend Bitcoin (mainstream)
- iGaming: instant deposits and withdrawals (online gambling/gaming audiences)

Write THREE outreach email templates — one per segment. Tone: warm, genuine, \
human, like a real partnerships person who actually watched their content. NOT \
corporate, no buzzwords, no "I hope this email finds you well". Short and \
respectful of their time.

Each template must include, clearly labeled:
  SUBJECT: a short, non-spammy subject line
  Opening hook: 1-2 lines referencing the creator's niche — use a {placeholder} \
    the team fills in (e.g. {creator_name}, {recent_video_topic}) so it's \
    personalizable.
  Value prop: 2-3 lines on why Speed fits THIS segment's audience specifically.
  The ask: one clear partnership ask appropriate to the segment (paid \
    partnership, affiliate/rev-share, or content collab) — vary it across the three.
  CTA: one low-friction closing line.

Use plain text only — no markdown, asterisks, or bold markers. Separate the three \
templates with a clear dashed divider and a header naming the segment. Keep each \
template under ~150 words."""


def generate() -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": _PROMPT}],
    )
    return resp.content[0].text


def run() -> str:
    print(f"Generating outreach email templates ({_MODEL})...")
    body = generate()
    bar = "=" * 70
    full = (f"{bar}\nSPEED WALLET — CREATOR OUTREACH EMAIL TEMPLATES\n"
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{bar}\n\n"
            + body)
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = _DOCS_DIR / "outreach_email_templates.txt"
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
