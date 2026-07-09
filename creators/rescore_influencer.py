"""Rescore influencer_score for all creators after the 2026-07 influencer audit.

Structural fix (see creators/scorer.py influencer_score):
  The old formula was engagement 30% + personal-brand 40% + audience 30%. The 40%
  personal-brand term was dropped: half of it (a media/company name string-match)
  just recycled the is_influencer classifier already shown in the UI, and the
  other half (a +40 lifestyle-tag bonus) was driven by YouTube auto
  topic-categories, not real lifestyle signal — handing the max score to
  brand/media accounts (BTC-ECHO, Altcoin Daily, SMART CRYPTO WALLET, ...).

  New formula: engagement 50% + audience-size 50%. is_influencer (unchanged) still
  carries the individual-vs-brand call and drives the dashboard badge + filter.

influencer_score depends only on stored engagement_quality + followers, so this
recomputes from stored data — no re-fetch. Prints before/after movement. Dry run
by default; pass --write to persist.

Run:  python creators/rescore_influencer.py [--write]
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database
from creators.scorer import influencer_score

# Brand/media accounts the audit flagged as scoring artificially high under the
# old personal-brand bonus — called out explicitly in the report.
_FLAGGED_BRANDS = (
    "BTC-ECHO", "Altcoin Daily", "SMART CRYPTO WALLET", "Crypto Warehouse",
    "Crypto Wall Street", "Crypto Wallets Info",
)


def _f(r, k):
    return float(r.get(k) or 0)


def main(write: bool) -> None:
    rows = database.get_all_creators()
    for r in rows:
        r["_old"] = round(_f(r, "influencer_score"), 1)
        r["_new"] = influencer_score(r)

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

    print(f"{'DRY RUN — no writes' if not write else 'WRITING'} · {len(rows)} creators "
          f"(influencer_score only; is_influencer NOT touched)\n")
    print("=== TOP 20 (by influencer_score) ===")
    print(f"  overlap: {len(top20_old & top20_new)}/20 · dropped out: {len(top20_old - top20_new)} · "
          f"new entrants: {len(top20_new - top20_old)}")
    print("\n=== RANK MOVEMENT (all creators) ===")
    print(f"  mean |Δrank|: {mean(moves):.1f} · max: {max(moves)} · unchanged: {sum(1 for m in moves if m == 0)} · "
          f"moved >20: {sum(1 for m in moves if m > 20)}")
    print("\n=== SCORE Δ ===")
    print(f"  mean: {mean(deltas):+.1f} · min: {min(deltas):+.1f} · max: {max(deltas):+.1f}")

    print("\n=== FLAGGED BRAND/MEDIA ACCOUNTS (should drop off the influencer dimension) ===")
    by_name = {r.get("name", ""): r for r in rows}
    for nm in _FLAGGED_BRANDS:
        r = by_name.get(nm)
        if not r:
            print(f"  {nm[:34]:34} (not found in current data)")
            continue
        print(f"  {nm[:34]:34} infl {r['_old']:5.1f}→{r['_new']:5.1f}  "
              f"#{old_rank[r['id']]+1:>3}→#{new_rank[r['id']]+1:<3}  is_influencer={r.get('is_influencer')}")

    print("\n=== NEW TOP 15 (name | old→new | rank old→new | is_influencer) ===")
    for r in new_sorted[:15]:
        print(f"  {r['name'][:32]:32} {r['_old']:5.1f}→{r['_new']:5.1f}  "
              f"#{old_rank[r['id']]+1:>3}→#{new_rank[r['id']]+1:<3} "
              f"{'individual' if r.get('is_influencer') else 'brand/media'}")

    if not write:
        print("\n(dry run — pass --write to persist influencer_score)")
        return

    sb = database._client()
    n = 0
    for r in rows:
        sb.table(database._TABLE).update({
            "influencer_score": r["_new"],
        }).eq("id", r["id"]).execute()
        n += 1
    print(f"\nWrote {n} updated influencer scores.")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
