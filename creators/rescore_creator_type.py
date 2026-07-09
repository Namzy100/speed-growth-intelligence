"""Rescore is_influencer (Individual vs Brand/Media) after the 2026-07 audit.

The individual/brand badge now reads directly off is_influencer, so the old
keyword name-heuristic's precision matters. It failed in both directions:
brand/media accounts with no trigger word read as Individual (BTC-ECHO, Altcoin
Daily, Crypto Wall Street, ...), and real people whose names contain a trigger
word read as Brand (Jaime Merino / "trading", Rob Wallace / "news"). Keyword
matching cannot separate surface-identical names ("Crypto Wall Street" brand vs
"Crypto Casey" person) — a world-knowledge call — so the individual/brand half of
detect_influencer now uses a cheap Haiku classifier (CreatorScorer._detect_influencer,
gated behind use_llm_fallback; keyword heuristic remains the offline fallback).

The engagement gate is unchanged. is_influencer is recomputed from stored fields
(name, tags, engagement_quality) and cached in the column — no re-fetch.

Discipline gate: the 10 known audit cases (7 brands + 3 individuals) are tested
FIRST; the full 347 run is refused unless all 10 resolve correctly.

Run:  python creators/rescore_creator_type.py [--write]
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

# 7 false negatives (want BRAND -> is_influencer False) + 3 false positives
# (want INDIVIDUAL -> is_influencer True). Matched by case-insensitive substring.
_WANT_BRAND = [
    "SMART CRYPTO WALLET", "BTC-ECHO", "Altcoin Daily", "Crypto Wall Street",
    "Crypto Wallets Info", "Crypto Warehouse", "CryptoLabs Research",
]
_WANT_INDIVIDUAL = ["Trading Latino", "Rob Wallace", "Wealth Transfer with TC"]


def _find(rows, substr):
    for r in rows:
        if substr.lower() in (r.get("name", "") or "").lower():
            return r
    return None


def _test_ten(scorer, rows) -> bool:
    """Run the 10 known cases through the classifier. Return True iff all pass."""
    print("=== 10-case discipline gate (individual/brand classifier) ===")
    ok = 0
    print("FALSE NEGATIVES (want BRAND):")
    for s in _WANT_BRAND:
        r = _find(rows, s)
        if not r:
            print(f"  {s[:34]:34} NOT FOUND"); continue
        v = scorer._classify_individual_brand(r)   # True=individual, False=brand
        good = v is False
        ok += good
        print(f"  {r.get('name','')[:34]:34} -> {'BRAND' if v is False else 'INDIVIDUAL' if v else 'None':11} {'OK' if good else 'WRONG'}")
    print("FALSE POSITIVES (want INDIVIDUAL):")
    for s in _WANT_INDIVIDUAL:
        r = _find(rows, s)
        if not r:
            print(f"  {s[:34]:34} NOT FOUND"); continue
        v = scorer._classify_individual_brand(r)
        good = v is True
        ok += good
        print(f"  {r.get('name','')[:34]:34} -> {'INDIVIDUAL' if v is True else 'BRAND' if v is False else 'None':11} {'OK' if good else 'WRONG'}")
    total = len(_WANT_BRAND) + len(_WANT_INDIVIDUAL)
    print(f"=== {ok}/{total} correct ===\n")
    return ok == total


def main(write: bool) -> None:
    rows = database.get_all_creators()
    scorer = CreatorScorer(use_llm_fallback=True)

    if not _test_ten(scorer, rows):
        print("ABORT: not all 10 known cases resolved correctly — refusing the "
              "full run. Fix the classifier before rescoring the 347.")
        sys.exit(1)

    print(f"{'WRITING' if write else 'DRY RUN — no writes'} · recomputing "
          f"is_influencer for {len(rows)} creators (LLM individual/brand)...\n")

    flips = []
    old_true = new_true = 0
    for r in rows:
        old = bool(r.get("is_influencer", False))
        new = scorer._detect_influencer(r)
        r["_new_infl"] = new
        old_true += old
        new_true += new
        if old != new:
            flips.append((r, old, new))

    print("=== INDIVIDUAL / BRAND SPLIT ===")
    print(f"  Individual: {old_true} -> {new_true}   Brand/Media: {len(rows)-old_true} -> {len(rows)-new_true}")
    print(f"  total flips: {len(flips)}  "
          f"(Individual->Brand: {sum(1 for _,o,n in flips if o and not n)}, "
          f"Brand->Individual: {sum(1 for _,o,n in flips if not o and n)})\n")

    print("=== FLIPS (name | was -> now) ===")
    for r, o, n in sorted(flips, key=lambda x: (x[1], x[0].get("name", "").lower())):
        print(f"  {r.get('name','')[:40]:40} {('Individual' if o else 'Brand/Media'):11} -> {'Individual' if n else 'Brand/Media'}")

    if not write:
        print("\n(dry run — pass --write to persist is_influencer)")
        return

    sb = database._client()
    n = 0
    for r in rows:
        sb.table(database._TABLE).update({
            "is_influencer": r["_new_infl"],
        }).eq("id", r["id"]).execute()
        n += 1
    print(f"\nWrote is_influencer for {n} rows.")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
