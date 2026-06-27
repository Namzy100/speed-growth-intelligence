"""Budget-matching scoring for Speed Wallet creator partnerships.

For each creator (from Supabase) estimates: follower tier (nano/micro/mid/macro),
a likely rate range, what Speed should offer (flat fee / rev-share / hybrid), and
a 0-100 "match score" for how well their audience size + engagement fit typical
fintech partnership budgets. Writes a ranked list to
docs/creator_budget_matching_<date>.txt.

Rule-based (no LLM). All budget ranges and weights are PLACEHOLDERS in the
CONFIG block below — tune them to Speed's real numbers.

Run from repo root:  python intelligence/creator_budget_matcher.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from creators import database

_DOCS_DIR = _ROOT / "docs"

# ------------------------------------------------------------------
# CONFIG — placeholder budgets/weights; tune to Speed's real numbers.
# ------------------------------------------------------------------
# (tier, min_followers, max_followers, (rate_low_usd, rate_high_usd))
_TIERS = [
    ("nano",  0,          10_000,     (50,     250)),
    ("micro", 10_000,     100_000,    (250,    2_000)),
    ("mid",   100_000,    1_000_000,  (2_000,  15_000)),
    ("macro", 1_000_000,  float("inf"), (15_000, 75_000)),
]
# Sweet spot for fintech partnership budgets: micro/mid (efficient + affordable).
_TIER_FIT = {"nano": 55, "micro": 100, "mid": 85, "macro": 60}
_ENGAGEMENT_CAP = 0.10  # ER at/above this normalizes to 100
_WEIGHTS = {"composite": 0.5, "engagement": 0.3, "tier_fit": 0.2}
# Segments whose payoff is directly measurable (installs/deposits) → performance deals.
_PERF_SEGMENTS = {"remittance", "iGaming"}


def _tier(followers: int):
    for name, lo, hi, rng in _TIERS:
        if lo <= followers < hi:
            return name, rng
    return "macro", _TIERS[-1][3]


def _recommend_offer(tier: str, segment: str) -> str:
    perf = segment in _PERF_SEGMENTS
    if tier == "macro":
        return "flat fee" + (" + rev-share bonus" if perf else "")
    if tier == "mid":
        return "hybrid (flat base + rev-share)"
    # nano / micro — low cash risk, lean performance
    return "affiliate / rev-share" if perf else "flat fee (small) or affiliate"


def _match_score(composite: float, engagement: float, tier: str) -> float:
    eng_norm = min(engagement / _ENGAGEMENT_CAP, 1.0) * 100 if _ENGAGEMENT_CAP else 0
    return round(
        _WEIGHTS["composite"] * composite
        + _WEIGHTS["engagement"] * eng_norm
        + _WEIGHTS["tier_fit"] * _TIER_FIT.get(tier, 60),
        1,
    )


def build_matches() -> list[dict]:
    rows = database.get_all_creators()
    out = []
    for r in rows:
        followers = int(r.get("followers", 0) or 0)
        composite = float(r.get("composite_score", 0) or 0)
        engagement = float(r.get("engagement_rate", 0) or 0)
        segment = str(r.get("segment_tag", "general"))
        tier, (lo, hi) = _tier(followers)
        # Suggested point in the range scales with partnership quality (composite).
        point = round(lo + (hi - lo) * min(composite, 100) / 100)
        out.append({
            "name": str(r.get("name", "")),
            "platform": str(r.get("platform", "")),
            "segment": segment,
            "followers": followers,
            "tier": tier,
            "composite": round(composite, 1),
            "engagement": round(engagement, 4),
            "rate_low": lo, "rate_high": hi, "rate_point": point,
            "offer": _recommend_offer(tier, segment),
            "match_score": _match_score(composite, engagement, tier),
        })
    out.sort(key=lambda c: c["match_score"], reverse=True)
    return out


def render(matches: list[dict]) -> str:
    from collections import Counter
    bar = "=" * 100
    lines = [
        bar,
        "SPEED WALLET — CREATOR BUDGET MATCHING",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(matches)} creators · ranked by match score",
        "Budget ranges are PLACEHOLDERS (see CONFIG in creator_budget_matcher.py).",
        bar,
        "",
        "Tier rate ranges (USD): " + " | ".join(
            f"{t}={lo:,}-{hi:,}" for t, lo, hi, (lo_, hi_) in
            [(t, rng[0], rng[1], rng) for t, _a, _b, rng in _TIERS]
        ),
        "",
        f"{'MATCH':>5}  {'SCORE':>5}  {'TIER':<6} {'SEGMENT':<14} {'FOLLOWERS':>10}  "
        f"{'ER':>6}  {'RATE RANGE (pt)':<22} {'OFFER':<28} NAME [PLATFORM]",
        "-" * 100,
    ]
    for m in matches:
        rate = f"${m['rate_low']:,}-${m['rate_high']:,} (${m['rate_point']:,})"
        lines.append(
            f"{m['match_score']:>5}  {m['composite']:>5}  {m['tier']:<6} "
            f"{m['segment']:<14} {m['followers']:>10,}  {m['engagement']*100:>5.1f}%  "
            f"{rate:<22} {m['offer']:<28} {m['name']} [{m['platform']}]"
        )
    # Tier + offer distribution summary.
    lines.append("")
    lines.append("DISTRIBUTION")
    lines.append("-" * 12)
    for t, n in Counter(m["tier"] for m in matches).most_common():
        lines.append(f"  tier {t:<6} {n}")
    for o, n in Counter(m["offer"] for m in matches).most_common():
        lines.append(f"  offer: {o:<30} {n}")
    return "\n".join(lines)


def run() -> str:
    print("Reading creators from Supabase...")
    matches = build_matches()
    print(f"  matched {len(matches)} creators")
    text = render(matches)
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    path = _DOCS_DIR / f"creator_budget_matching_{today}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"Saved: {path.relative_to(_ROOT)}")
    # Print the top of the ranking for a quick look.
    print("\n".join(text.splitlines()[:25]))
    return text


if __name__ == "__main__":
    run()
