"""Rescore all creators on the new 4-dimension composite (2026-07 redesign).

Composite = audience_fit + engagement + reach + sponsorship(where measured),
each /20, renormalised to /100 over the dims backed by real data. Dropped:
content_alignment (redundant with audience_fit). Reach is now a real dimension
(pure follower size, Curve B). See creators/scorer.py.

Re-scores from the STORED RAW INPUTS (followers, engagement_quality,
crypto_content_pct, niche_tags, sponsorship_count, sponsorship_data_available) by
running CreatorScorer.score() — NOT by recomputing from stored dimension scores,
because reach must apply the new follower curve and the scraped-data flag must be
applied per creator.

scraped_data_available is derived: a Mimanshi creator that has NOT been re-fetched
(mimanshi_list tag AND sponsorship_data_available == False) is still on spreadsheet
placeholders (the Instagram/X set with no fetcher). For those, audience_fit +
engagement are fabricated, so they are excluded — composite = reach (+ sponsorship
if any). Their fit rating carries vetting separately.

Does NOT touch is_influencer (that is the LLM classifier's column; recomputing it
here with the deterministic scorer would regress it).

Run:  python creators/rescore_4dim.py [--write]
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database
from creators.scorer import CreatorScorer


def _is_mimanshi(r: dict) -> bool:
    return any(str(t).lower() == "mimanshi_list" for t in (r.get("niche_tags") or []))


def _f(r, k):
    return float(r.get(k) or 0)


def _creator_dict(r: dict) -> dict:
    """Reconstruct a CreatorScorer input dict from a stored DB row."""
    unscraped = _is_mimanshi(r) and not bool(r.get("sponsorship_data_available", False))
    return {
        "name": r.get("name", ""),
        "platform": r.get("platform", ""),
        "followers": int(r.get("followers", 0) or 0),
        "engagement_rate": _f(r, "engagement_rate"),
        "engagement_quality": r.get("engagement_quality"),
        "crypto_content_pct": _f(r, "crypto_content_pct"),
        "fintech_content_pct": _f(r, "fintech_content_pct"),
        "sponsorship_count": int(r.get("sponsorship_count", 0) or 0),
        "niche_tags": r.get("niche_tags") or [],
        "sponsorship_data_available": bool(r.get("sponsorship_data_available", False)),
        "scraped_data_available": not unscraped,
    }


def main(write: bool) -> None:
    rows = database.get_all_creators()
    scorer = CreatorScorer(use_llm_fallback=False)   # deterministic; does not touch is_influencer here

    for r in rows:
        r["_old"] = round(_f(r, "composite_score"), 1)
        out = scorer.score(_creator_dict(r))
        r["_new"] = out["composite_score"]
        r["_basis"] = out["composite_basis"]
        r["_reach"] = out["scores"]["acquisition_potential"]
        r["_infl_score"] = out["influencer_score"]
        r["_scores"] = out["scores"]

    def rank(key):
        return {c["id"]: i for i, c in enumerate(
            sorted(rows, key=lambda r: (r[key], _f(r, "followers")), reverse=True))}
    old_rank, new_rank = rank("_old"), rank("_new")
    new_sorted = sorted(rows, key=lambda r: (r["_new"], _f(r, "followers")), reverse=True)
    top20_old = {c["id"] for c in sorted(rows, key=lambda r: (r["_old"], _f(r, "followers")), reverse=True)[:20]}
    top20_new = {c["id"] for c in new_sorted[:20]}
    moves = [abs(new_rank[c["id"]] - old_rank[c["id"]]) for c in rows]

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"{'WRITING' if write else 'DRY RUN — no writes'} · {len(rows)} creators · new 4-dim composite\n")
    print("=== TOP 20 ===")
    print(f"  overlap: {len(top20_old & top20_new)}/20 · dropped: {len(top20_old - top20_new)} · "
          f"new: {len(top20_new - top20_old)}")
    print("=== RANK MOVEMENT ===")
    print(f"  mean |Δrank|: {mean(moves):.1f} · max: {max(moves)} · unchanged: {sum(1 for m in moves if m==0)}")

    # Mimanshi uniformity check — the whole point of tonight's fix.
    mim = [r for r in rows if _is_mimanshi(r)]
    mim_yt = [r for r in mim if r.get("platform") == "YouTube"]
    mim_igx = [r for r in mim if r.get("platform") in ("Instagram", "X")]
    import statistics as st
    def spread(rs, key):
        vals = [r[key] for r in rs]
        return (len(set(round(v, 1) for v in vals)), round(st.pstdev(vals), 1) if len(vals) > 1 else 0.0,
                round(min(vals), 1), round(max(vals), 1))
    print("\n=== MIMANSHI UNIFORMITY (old 76.3 cluster) ===")
    du, so, lo, hi = spread(mim, "_old"); print(f"  OLD composite: distinct={du}/{len(mim)}  std={so}  range {lo}-{hi}")
    du, sn, lo, hi = spread(mim, "_new"); print(f"  NEW composite: distinct={du}/{len(mim)}  std={sn}  range {lo}-{hi}")
    print(f"  Mimanshi YouTube (re-fetched, real data): n={len(mim_yt)}, "
          f"distinct_new={len(set(round(r['_new'],1) for r in mim_yt))}")
    print(f"  Mimanshi IG/X (unscraped, reach-only): n={len(mim_igx)}, "
          f"distinct_new={len(set(round(r['_new'],1) for r in mim_igx))}")

    print("\n=== sample: Mimanshi new composite vs followers (should now VARY) ===")
    for r in sorted(mim, key=lambda r:-(r.get('followers') or 0))[:8]:
        print(f"  {(r.get('name','')or'')[:28]:28} followers={int(r.get('followers')or 0):>8} "
              f"plat={r.get('platform','')[:3]} old={r['_old']:5.1f} new={r['_new']:5.1f} basis={r['_basis']}")

    if not write:
        print("\n(dry run — pass --write to persist composite + dims; is_influencer untouched)")
        return

    sb = database._client()
    n = 0
    for r in rows:
        s = r["_scores"]
        sb.table(database._TABLE).update({
            "composite_score": r["_new"],
            "audience_fit": s["audience_fit"],
            "engagement_quality_score": s["engagement_quality"],
            "content_alignment": s["content_alignment"],
            "acquisition_potential": s["acquisition_potential"],   # = reach
            "sponsorship_score": s["sponsorship_history"],
            "deposit_relevance_score": None,
            "influencer_score": r["_infl_score"],
            "updated_at": database._now(),
        }).eq("id", r["id"]).execute()
        n += 1
    print(f"\nWrote new 4-dim composite + dims for {n} creators (is_influencer left untouched).")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
