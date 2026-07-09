"""Rescore the existing creator pipeline after the 2026-07 scoring audit.

Two structural fixes (see creators/scorer.py):
  1. sponsorship_history is included in the composite ONLY where real sponsorship
     data was measured. For the CURRENT stored data the only fetcher that measured
     it is TikTok (isSponsored/isAd flags); YouTube hardcoded 0 and Instagram/X are
     imports — so those are treated as "no data": sponsorship is excluded and its
     weight redistributed (composite renormalised to /100 over the real dims).
  2. deposit_relevance is REMOVED from the composite (it was a 0.5 constant + recycled
     dimensions). deposit_relevance_score is set to NULL so it can't read as a real 0.

Recomputes composite from the STORED per-dimension scores (audience_fit,
engagement_quality_score, content_alignment, sponsorship_score) — those dimensions
are unchanged by the audit, so no re-fetch is needed. Prints a before/after
comparison. Pass --write to persist; default is a dry run.

Run:  python creators/rescore_existing.py [--write]
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database

# Only the TikTok fetcher actually measured sponsorship in the current data.
_MEASURED_SPONSORSHIP_PLATFORMS = {"TikTok"}


def _f(r, k):
    return float(r.get(k) or 0)


def _recompute(r: dict) -> tuple[float, bool, list[str]]:
    af, eq, ca = _f(r, "audience_fit"), _f(r, "engagement_quality_score"), _f(r, "content_alignment")
    sp = _f(r, "sponsorship_score")
    spons_avail = r.get("platform") in _MEASURED_SPONSORSHIP_PLATFORMS
    dims = [af, eq, ca]
    basis = ["audience_fit", "engagement", "content_alignment"]
    if spons_avail:
        dims.append(sp)
        basis.append("sponsorship")
    new = round(sum(dims) * 100.0 / (20.0 * len(dims)), 1)
    return new, spons_avail, basis


def main(write: bool) -> None:
    rows = database.get_all_creators()
    for r in rows:
        r["_old"] = round(_f(r, "composite_score"), 1)
        r["_new"], r["_avail"], _ = _recompute(r)

    old_rank = {r["id"]: i for i, r in enumerate(
        sorted(rows, key=lambda r: (r["_old"], _f(r, "followers")), reverse=True))}
    new_sorted = sorted(rows, key=lambda r: (r["_new"], _f(r, "followers")), reverse=True)
    new_rank = {r["id"]: i for i, r in enumerate(new_sorted)}

    top20_old = {r["id"] for r in sorted(rows, key=lambda r: (r["_old"], _f(r, "followers")), reverse=True)[:20]}
    top20_new = {r["id"] for r in new_sorted[:20]}
    moves = [abs(new_rank[r["id"]] - old_rank[r["id"]]) for r in rows]
    deltas = [r["_new"] - r["_old"] for r in rows]

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"{'DRY RUN — no writes' if not write else 'WRITING'} · {len(rows)} creators\n")
    print(f"sponsorship data available (measured): {sum(1 for r in rows if r['_avail'])} "
          f"(TikTok); excluded/renormalised: {sum(1 for r in rows if not r['_avail'])}\n")
    print("=== TOP 20 ===")
    print(f"  overlap: {len(top20_old & top20_new)}/20 · dropped out: {len(top20_old - top20_new)} · "
          f"new entrants: {len(top20_new - top20_old)}")
    print("\n=== RANK MOVEMENT (all creators) ===")
    print(f"  mean |Δrank|: {mean(moves):.1f} · max: {max(moves)} · unchanged: {sum(1 for m in moves if m == 0)} · "
          f"moved >20: {sum(1 for m in moves if m > 20)}")
    print("\n=== SCORE Δ ===")
    yt = [d for r, d in zip(rows, deltas) if r.get("platform") == "YouTube"]
    tt = [d for r, d in zip(rows, deltas) if r.get("platform") == "TikTok"]
    print(f"  overall mean: {mean(deltas):+.1f} · YouTube: {mean(yt):+.1f} · TikTok: {mean(tt):+.1f}")
    print("\n=== NEW TOP 15 (name | old→new | rank old→new | spons) ===")
    for r in new_sorted[:15]:
        print(f"  {r['name'][:32]:32} {r['_old']:5.1f}→{r['_new']:5.1f}  "
              f"#{old_rank[r['id']]+1:>3}→#{new_rank[r['id']]+1:<3} "
              f"{'measured' if r['_avail'] else 'no-data'}")
    # biggest movers
    movers = sorted(rows, key=lambda r: abs(new_rank[r["id"]] - old_rank[r["id"]]), reverse=True)[:10]
    print("\n=== 10 BIGGEST RANK MOVERS ===")
    for r in movers:
        d = new_rank[r["id"]] - old_rank[r["id"]]
        print(f"  {r['name'][:32]:32} {r['_old']:5.1f}→{r['_new']:5.1f}  "
              f"#{old_rank[r['id']]+1:>3}→#{new_rank[r['id']]+1:<3} ({d:+d}) {r.get('platform')}")

    if not write:
        print("\n(dry run — pass --write to persist composite_score + null out deposit_relevance_score)")
        return

    sb = database._client()
    n = 0
    for r in rows:
        sb.table(database._TABLE).update({
            "composite_score": r["_new"],
            "deposit_relevance_score": None,   # removed from composite; not a real 0
            "updated_at": database._now(),
        }).eq("id", r["id"]).execute()
        n += 1
    print(f"\nWrote {n} updated composite scores.")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
