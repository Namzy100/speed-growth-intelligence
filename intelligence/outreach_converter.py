"""Creator outreach converter — turn the top scored leads into send-ready emails.

Pulls the top 15 not-yet-contacted creators by composite score from Supabase,
fills the v2 outreach templates with real creator data (a single grounded Claude
call supplies each creator's content-topic phrase + one-line fit note), and
writes one copy-paste-ready email per creator plus a SEND_THESE_NOW.txt index.

This is not scoring — it's the emails, ready to send today.

Output: docs/outreach_ready/<creator_slug>.txt  +  docs/outreach_ready/SEND_THESE_NOW.txt

Run from repo root:  python intelligence/outreach_converter.py
"""

import json
import os
import re
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from creators import database

_OUT_DIR = _ROOT / "docs" / "outreach_ready"
_TEMPLATES = _ROOT / "docs" / "outreach_email_templates_v2.txt"
_MODEL = "claude-sonnet-4-6"
_TOP_N = 15

# segment_tag -> template segment key; general falls back to crypto-curious.
_SEG_MAP = {"remittance": "REMITTANCE", "crypto-curious": "CRYPTO-CURIOUS",
            "iGaming": "IGAMING", "general": "CRYPTO-CURIOUS"}


# ------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------

def parse_templates() -> dict[str, dict]:
    """Return {SEGMENT: {'subject': str, 'body': str}} from the v2 templates file."""
    raw = _TEMPLATES.read_text(encoding="utf-8")
    blocks = re.split(r"SEGMENT:\s*", raw)[1:]
    out = {}
    for b in blocks:
        seg = b.splitlines()[0].strip().upper()
        subj_m = re.search(r"SUBJECT:\s*(.+)", b)
        subject = subj_m.group(1).strip() if subj_m else ""
        # Body = everything after the subject line up to the divider row.
        after = b[subj_m.end():] if subj_m else b
        body = re.split(r"\n-\s?-\s?-", after)[0].strip("\n")
        out[seg] = {"subject": subject, "body": body}
    return out


# ------------------------------------------------------------------
# Creator data
# ------------------------------------------------------------------

def _clean_name(name: str) -> str:
    """Display name: drop trailing '@handle' and platform noise."""
    n = re.split(r"\s+@", name)[0]
    n = re.split(r"\s+[-–—]\s+", n)[0] if "@" not in n else n
    return n.strip() or name.strip()


def _fit_score(tags: list) -> int:
    for t in tags or []:
        m = re.fullmatch(r"fit_([1-5])", str(t).strip().lower())
        if m:
            return int(m.group(1))
    return 0


def _content_focus(c: dict) -> str:
    """The creator's real content focus: Mimanshi reasoning prefix, else tags."""
    reasoning = c.get("reasoning") or ""
    if "mimanshi_list" in (c.get("niche_tags") or []) and "|" in reasoning:
        return reasoning.split("|")[0].strip()
    tags = [t for t in (c.get("niche_tags") or [])
            if t not in ("mimanshi_list", "Mexico", "Brazil")
            and not str(t).startswith("fit_")]
    return ", ".join(tags[:4])


def top_creators() -> list[dict]:
    rows = database.get_all_creators()  # composite desc
    picked = [r for r in rows if r.get("outreach_status") == "not_contacted"][:_TOP_N]
    out = []
    for r in picked:
        tags = r.get("niche_tags") or []
        out.append({
            "id": r["id"], "raw_name": r["name"], "name": _clean_name(r["name"]),
            "platform": r["platform"], "segment": r.get("segment_tag", "general"),
            "followers": int(r.get("followers", 0) or 0),
            "tags": [t for t in tags if not str(t).startswith("fit_")
                     and t not in ("mimanshi_list",)],
            "fit_score": _fit_score(tags),
            "mimanshi": "mimanshi_list" in tags,
            "composite": round(float(r.get("composite_score", 0) or 0), 1),
            "content_focus": _content_focus(r),
        })
    return out


# ------------------------------------------------------------------
# Claude: grounded topic + fit note (one batched call)
# ------------------------------------------------------------------

def enrich(creators: list[dict]) -> dict[int, dict]:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    lines = []
    for i, c in enumerate(creators):
        lines.append(
            f"{i}. name={c['name']} | platform={c['platform']} | segment={c['segment']} | "
            f"followers={c['followers']} | content_focus={c['content_focus'] or '(unknown)'} | "
            f"tags={c['tags'][:5]}"
        )
    prompt = (
        "For each creator below, using ONLY the given data, produce two short grounded strings:\n"
        "  topic     — a natural-language phrase naming what their content is about, to drop "
        "into '<their> video/content on ___'. 3-6 words, lowercase, no invented specifics.\n"
        "  fit_note  — one line (<=16 words) on why they're a strong Speed Wallet partner for "
        "their segment (remittance=zero-fee sends, crypto-curious=simplicity, iGaming=instant deposits).\n\n"
        "Return ONLY a JSON array: [{\"index\":0,\"topic\":\"\",\"fit_note\":\"\"}, ...]. "
        "One object per creator, same indexes.\n\n"
        + "\n".join(lines)
    )
    resp = client.messages.create(
        model=_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`"); text = text[text.find("["):]
    try:
        arr = json.loads(text[text.find("["): text.rfind("]") + 1])
    except (json.JSONDecodeError, ValueError):
        arr = []
    return {int(o["index"]): o for o in arr if "index" in o}


# ------------------------------------------------------------------
# Fill + write
# ------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "creator"


def _fill(text: str, c: dict, topic: str) -> str:
    return (text.replace("{creator_name}", c["name"])
                .replace("{recent_video_topic}", topic)
                .replace("{platform}", c["platform"])
                .replace("{corridor}", "your community"))


def run() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    templates = parse_templates()
    creators = top_creators()
    if not creators:
        print("No not_contacted creators found."); return
    print(f"Top {len(creators)} not-contacted creators. Enriching via Claude...")
    enriched = enrich(creators)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for i, c in enumerate(creators):
        seg_key = _SEG_MAP.get(c["segment"], "CRYPTO-CURIOUS")
        tpl = templates.get(seg_key) or templates["CRYPTO-CURIOUS"]
        meta = enriched.get(i, {})
        topic = (meta.get("topic") or c["content_focus"] or "your recent content").strip()
        fit_note = (meta.get("fit_note") or f"Strong {c['segment']} fit.").strip()

        subject = _fill(tpl["subject"], c, topic)
        body = _fill(tpl["body"], c, topic)
        fit_line = f" · Mimanshi fit {c['fit_score']}/5" if c["mimanshi"] and c["fit_score"] else ""

        email = (
            f"TO:       [{c['name']} — {c['platform']}]\n"
            f"SEGMENT:  {c['segment']}{fit_line} · {c['followers']:,} followers · composite {c['composite']}\n"
            f"SUBJECT:  {subject}\n"
            f"{'-' * 60}\n\n{body}\n"
        )
        slug = _slug(c["name"])
        (_OUT_DIR / f"{i+1:02d}_{slug}.txt").write_text(email, encoding="utf-8")
        index_rows.append(
            f"{i+1:>2}. {c['name'][:30]:30} [{c['segment']:<14}] {c['followers']:>9,}f  "
            f"{c['platform']:<9}\n"
            f"     SUBJECT: {subject}\n"
            f"     WHY:     {fit_note}\n"
            f"     FILE:    outreach_ready/{i+1:02d}_{slug}.txt\n"
        )

    summary = (
        "=" * 70 + "\nSPEED WALLET — SEND THESE NOW (top 15 outreach, ready to go)\n"
        + "=" * 70 + "\n\n"
        f"{len(creators)} personalized emails generated from the v2 templates.\n"
        "Each file below is copy-paste ready — swap [Your name] and hit send.\n\n"
        + "\n".join(index_rows)
    )
    (_OUT_DIR / "SEND_THESE_NOW.txt").write_text(summary, encoding="utf-8")
    print(f"Wrote {len(creators)} emails + SEND_THESE_NOW.txt to {_OUT_DIR.relative_to(_ROOT)}/")
    print("\n" + summary[:1600])


if __name__ == "__main__":
    try:
        run()
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
