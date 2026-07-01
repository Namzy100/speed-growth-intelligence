"""Paid-spend optimization memo — specific, numeric budget reallocation.

Reads Meta Campaigns, Meta Creatives, Adjust Channel Overview and Campaign
Installs from the Google Sheet, computes current spend/CPI per campaign and the
projected install lift from shifting budget out of the worst performers into the
best one, then has Claude write a To:Niyati / From:Naman memo using ONLY the
computed numbers (no invented figures).

Output: docs/spend_optimization_memo_YYYY_MM_DD.txt

Run from repo root:  python intelligence/spend_optimization.py
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

load_dotenv(_ROOT / ".env")

from pipelines import sheets

_DOCS = _ROOT / "docs"
_MODEL = "claude-sonnet-4-6"

_BEST = "Payday - Android - Broad+"          # reallocation target
_DRAIN = ["Cash Deposit", "Re-engagement", "Re-targeting"]  # sources to cut


def _num(x) -> float:
    if x is None:
        return 0.0
    s = str(x).replace("$", "").replace(",", "").replace("USD", "").strip()
    try:
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def _records(ss, tab: str) -> list[dict]:
    return sheets._retry(lambda: ss.worksheet(tab)).get_all_records()


def _meta_campaigns(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        spend = _num(r.get("spend"))
        inst = _num(r.get("mobile_app_install"))
        out.append({
            "name": str(r.get("campaign_name", "")).strip(),
            "spend": spend, "installs": int(inst),
            "cpi": (spend / inst) if inst else None,
            "clicks": int(_num(r.get("clicks"))),
        })
    return out


def _match(campaigns: list[dict], needle: str) -> dict | None:
    for c in campaigns:
        if needle.lower() in c["name"].lower():
            return c
    return None


def compute(ss) -> dict:
    meta = _meta_campaigns(_records(ss, "Meta Campaigns"))
    best = _match(meta, _BEST)
    if not best or not best["cpi"]:
        raise RuntimeError(f"Best campaign '{_BEST}' not found or has no CPI.")

    # Sources to drain: matched underperformers with real spend.
    drains = []
    for needle in _DRAIN:
        c = _match(meta, needle)
        if c and c["spend"] > 0 and (c["cpi"] is None or c["cpi"] > best["cpi"] * 2):
            drains.append(c)

    freed = sum(c["spend"] for c in drains)
    wasted_installs = sum(c["installs"] for c in drains)
    # Projected installs if the freed budget ran at the best campaign's CPI.
    projected_from_freed = int(freed / best["cpi"]) if best["cpi"] else 0
    lift = projected_from_freed - wasted_installs

    # Blended CPI before vs after (over just the reallocated pool).
    pool_spend = freed + best["spend"]
    installs_before = wasted_installs + best["installs"]
    installs_after = projected_from_freed + best["installs"]
    blended_before = pool_spend / installs_before if installs_before else 0
    blended_after = pool_spend / installs_after if installs_after else 0

    # Apple Search Ads vs Facebook (Adjust Channel Overview eCPI).
    channels = _records(ss, "Channel Overview")
    def ch_ecpi(name):
        for r in channels:
            if name.lower() in str(r.get("channel", "")).lower() and _num(r.get("ecpi")) > 0:
                return _num(r.get("ecpi")), int(_num(r.get("installs")))
        return None, 0
    apple_ecpi, apple_inst = ch_ecpi("Apple")
    fb_ecpi, fb_inst = ch_ecpi("Facebook")

    return {
        "meta": meta, "best": best, "drains": drains,
        "freed": freed, "wasted_installs": wasted_installs,
        "projected_from_freed": projected_from_freed, "lift": lift,
        "blended_before": blended_before, "blended_after": blended_after,
        "apple": {"ecpi": apple_ecpi, "installs": apple_inst},
        "facebook": {"ecpi": fb_ecpi, "installs": fb_inst},
    }


def _facts_block(c: dict) -> str:
    lines = ["COMPUTED FIGURES (use these exact numbers — do not invent others):", ""]
    b = c["best"]
    lines.append(f"Best campaign — {b['name']}: ${b['spend']:,.2f} spend, "
                 f"{b['installs']:,} installs, CPI ${b['cpi']:.2f}")
    lines.append("")
    lines.append("Underperformers to drain:")
    for d in c["drains"]:
        cpi = f"${d['cpi']:.2f}" if d["cpi"] else "n/a"
        lines.append(f"  - {d['name']}: ${d['spend']:,.2f} spend, {d['installs']} installs, CPI {cpi}, {d['clicks']:,} clicks")
    lines += [
        "",
        f"Budget freed by pausing those: ${c['freed']:,.2f}",
        f"Installs those currently produce: {c['wasted_installs']}",
        f"Installs if that ${c['freed']:,.2f} ran at the best CPI (${b['cpi']:.2f}): {c['projected_from_freed']:,}",
        f"Net projected install lift: +{c['lift']:,}",
        f"Blended CPI over the reallocated pool — before: ${c['blended_before']:.2f}, after: ${c['blended_after']:.2f}",
        "",
        "Apple Search Ads vs Facebook (Adjust eCPI):",
        f"  Apple: eCPI ${c['apple']['ecpi']:.2f} on {c['apple']['installs']:,} installs" if c["apple"]["ecpi"] else "  Apple: n/a",
        f"  Facebook: eCPI ${c['facebook']['ecpi']:.2f} on {c['facebook']['installs']:,} installs" if c["facebook"]["ecpi"] else "  Facebook: n/a",
    ]
    return "\n".join(lines)


def generate_memo(facts: str) -> str:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (
        "Write a paid-spend reallocation memo for Speed Wallet (Bitcoin Lightning app).\n"
        "Format: a real business memo.\n"
        "  To: Niyati\n  From: Naman\n  Re: Meta budget reallocation — immediate\n\n"
        "Use ONLY the computed figures below — every dollar amount and install number "
        "must come from them; do not invent numbers. Structure:\n"
        "1. One-paragraph bottom line (the single move and its projected impact).\n"
        "2. The reallocation, with exact dollars: how much to pull from each "
        "underperformer and move into the best campaign, the projected install lift, "
        "and the before/after blended CPI.\n"
        "3. Apple Search Ads vs Facebook: which is more efficient and whether to shift "
        "budget there, with the numbers.\n"
        "4. Risks / caveats (2-3 lines).\n"
        "Direct, numeric, no markdown headers or bullet symbols beyond simple numbering.\n\n"
        + facts
    )
    resp = client.messages.create(
        model=_MODEL, max_tokens=1600,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run() -> Path:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")

    print("Reading Meta + Adjust tabs...")
    ss = sheets._open(spreadsheet_id)
    c = compute(ss)
    facts = _facts_block(c)
    print("\n" + facts + "\n")
    print(f"Writing memo ({_MODEL})...")
    memo = generate_memo(facts)

    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    header = (
        "=" * 70 + "\nSPEED WALLET — PAID SPEND OPTIMIZATION MEMO\n"
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · figures live from Google Sheet\n"
        + "=" * 70 + "\n\n"
    )
    out = _DOCS / f"spend_optimization_memo_{stamp}.txt"
    out.write_text(header + memo + "\n\n\n" + "-" * 32 + "\nAPPENDIX — " + facts + "\n",
                   encoding="utf-8")
    print(f"Saved: {out.relative_to(_ROOT)}")
    return out


if __name__ == "__main__":
    try:
        path = run()
        print("=" * 60)
        print(path.read_text(encoding="utf-8"))
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
