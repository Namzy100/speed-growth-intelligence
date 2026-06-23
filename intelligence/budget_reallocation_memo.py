"""Budget reallocation memo from Channel Overview (Adjust) + Meta Campaigns tabs.

Reads the channel-level eCPI/install data and Meta campaign spend, then asks
Claude (claude-sonnet-4-6) to write a budget reallocation memo with specific
dollar amounts and percentage shifts grounded in the real efficiency data.
Saves to docs/budget_memo_<date>.txt and prints to terminal.

Run from repo root:  python intelligence/budget_reallocation_memo.py
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

from pipelines import sheets  # reuse service-account auth + retry

_DOCS_DIR = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"


# ------------------------------------------------------------------
# Sheet helpers
# ------------------------------------------------------------------

def _num(x) -> float:
    """Tolerant numeric parse: strips $, commas, currency words; 0.0 on failure."""
    if x is None:
        return 0.0
    s = str(x).replace("$", "").replace(",", "").replace("USD", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def _records(ss, tab: str) -> list[dict]:
    ws = sheets._retry(lambda: ss.worksheet(tab))
    return sheets._retry(ws.get_all_records)


# ------------------------------------------------------------------
# Build the data summary fed to Claude
# ------------------------------------------------------------------

def build_data_summary(channel_rows: list[dict], meta_rows: list[dict]) -> str:
    parts = []

    # Channel Overview — installs + eCPI by channel. Paid channels (eCPI > 0)
    # have an estimated spend of installs x eCPI, which anchors dollar figures.
    channels = []
    for r in channel_rows:
        name = str(r.get("channel", "")).strip()
        installs = int(_num(r.get("installs")))
        if not name or installs == 0:
            continue
        ecpi = _num(r.get("ecpi"))
        est_spend = installs * ecpi  # 0 for organic/unpaid channels
        channels.append({"channel": name, "installs": installs, "ecpi": ecpi,
                         "est_spend": est_spend})
    channels.sort(key=lambda c: c["installs"], reverse=True)

    paid_spend = sum(c["est_spend"] for c in channels if c["ecpi"] > 0)
    lines = [f"ADJUST — CHANNEL OVERVIEW (last 30 days; estimated paid spend "
             f"${paid_spend:,.0f} across paid channels)"]
    for c in channels:
        if c["ecpi"] > 0:
            lines.append(f"  {c['channel']}: {c['installs']:,} installs, eCPI ${c['ecpi']:.2f}, "
                         f"est. spend ${c['est_spend']:,.0f} (PAID)")
        else:
            lines.append(f"  {c['channel']}: {c['installs']:,} installs, $0 eCPI "
                         f"(organic/unpaid — no reallocatable budget)")
    parts.append("\n".join(lines))

    # Meta Campaigns — actual spend + installs + cost-per-install.
    meta = []
    for r in meta_rows:
        name = str(r.get("campaign_name", "")).strip()
        if not name:
            continue
        spend = _num(r.get("spend"))
        installs = int(_num(r.get("mobile_app_install")))
        cpi = spend / installs if installs > 0 else 0.0
        meta.append({"campaign": name, "spend": spend, "installs": installs, "cpi": cpi})
    meta.sort(key=lambda c: c["spend"], reverse=True)

    total_spend = sum(c["spend"] for c in meta)
    lines = [f"META — CAMPAIGN SPEND (actual; total ${total_spend:,.2f})"]
    for c in meta:
        cpi_str = f"${c['cpi']:.2f}" if c["cpi"] > 0 else "n/a (0 installs)"
        lines.append(f"  {c['campaign']}: ${c['spend']:,.2f} spend, {c['installs']:,} installs, "
                     f"cost/install {cpi_str}")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ------------------------------------------------------------------
# Claude memo
# ------------------------------------------------------------------

_PROMPT = """\
You are Naman, a growth analyst at Speed Wallet (a Bitcoin Lightning payments \
app), writing a budget reallocation memo to Niyati, the growth lead. Speed's \
primary paid markets are the US and EU; influencer markets are the US, Mexico, \
and Brazil.

Use ONLY the real data below: channel-level eCPI and installs from Adjust (with \
estimated paid spend = installs x eCPI), and actual Meta campaign spend and \
cost-per-install. Do not invent channels or numbers.

Write a professional budget reallocation memo. Begin with EXACTLY this header \
block (four lines), then the body:

To: Niyati
From: Naman
Date: {today}
Re: Budget Reallocation Recommendation

The body must:
- Open with a one-paragraph summary of the current efficiency picture, citing \
real eCPI/CPI figures.
- Recommend specific reallocations with BOTH dollar amounts AND percentage \
shifts (e.g. "shift $X (Y% of channel Z's budget) from <high-eCPI channel> to \
<low-eCPI channel>"), grounded in the eCPI/CPI gaps in the data.
- Keep total recommended paid spend roughly flat (this is a REALLOCATION, not a \
budget increase) unless you explicitly justify a net change.
- Do not treat organic/unpaid ($0 eCPI) channels as a budget source.
- Close with a short expected-impact line (e.g. projected extra installs at the \
better blended eCPI).

Write in clean plain-text memo prose (short paragraphs, no markdown tables, no \
code blocks). Keep it under 450 words. Be concrete and decisive.

--- DATA ---
{data_summary}
--- END DATA ---"""


def generate_memo(data_summary: str, today_str: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1500,
        messages=[{"role": "user",
                   "content": _PROMPT.format(data_summary=data_summary, today=today_str)}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# Save + entrypoint
# ------------------------------------------------------------------

def save_memo(text: str) -> Path:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"budget_memo_{today}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def run() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    print("Opening Google Sheet (live)...")
    ss = sheets._open(sid)
    print("Reading tabs: Channel Overview (Adjust), Meta Campaigns...")
    channel_rows = _records(ss, "Channel Overview")
    meta_rows = _records(ss, "Meta Campaigns")
    print(f"  Channel rows: {len(channel_rows)} | Meta campaign rows: {len(meta_rows)}")

    summary = build_data_summary(channel_rows, meta_rows)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"Generating budget reallocation memo ({_MODEL})...")
    memo = generate_memo(summary, today_str)

    path = save_memo(memo)
    print(f"\nSaved: {path.relative_to(_ROOT)}\n")
    print("=" * 70)
    print(memo)
    print("=" * 70)
    return memo


if __name__ == "__main__":
    try:
        run()
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
