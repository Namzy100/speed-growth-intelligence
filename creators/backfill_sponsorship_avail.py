"""One-time backfill for the sponsorship_data_available column (2026-07).

Run ONCE after the SQL migration that adds the column:

    ALTER TABLE public.creators
        ADD COLUMN IF NOT EXISTS sponsorship_data_available BOOLEAN DEFAULT false;

Going forward the fetchers write this flag directly (creators/youtube.py,
creators/apify_tiktok.py -> database._merge_record). This script only sets the
correct value for the LEGACY rows that predate the column.

Backfill rule (per source, matching what each fetcher actually measures):
  - TikTok  -> True. The Apify fetcher has always measured sponsorship via the
    isSponsored/isAd flags, so every stored TikTok row has real data.
  - YouTube -> True ONLY with positive evidence the row was fetched with the new
    paidProductPlacementDetails flag. The only row-level signature of that is a
    non-zero sponsorship_count (the old YouTube path hardcoded 0). The flag code
    landed 2026-07-09 (commit e24ac31) and has not been run against the existing
    set, so 0 legacy YouTube rows qualify -> all False. A False here is correct,
    not a gap: it means "no measured data", so sponsorship is excluded from the
    composite rather than counted as a real 0.
  - Everything else (Instagram / X imports, any future non-measuring source)
    -> False.

Prints the YouTube old-vs-new split and a before/after check against the OLD
dashboard proxy (platform == 'TikTok') so you can see exactly which rows move.
Dry run by default; pass --write to persist.

Run:  python creators/backfill_sponsorship_avail.py [--write]
"""

import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database


def _target_flag(r: dict) -> bool:
    """The correct sponsorship_data_available value for a legacy row."""
    platform = r.get("platform")
    if platform == "TikTok":
        return True  # Apify fetcher always measured isSponsored/isAd.
    if platform == "YouTube":
        # True only with positive evidence of a new-flag (re-)fetch. The old path
        # hardcoded sponsorship_count=0, so a non-zero count is the only signature.
        return int(r.get("sponsorship_count", 0) or 0) > 0
    return False  # Instagram / X / anything that doesn't measure sponsorship.


def main(write: bool) -> None:
    rows = database.get_all_creators()

    # OLD dashboard proxy that this column replaces.
    def old_proxy(r):
        return r.get("platform") == "TikTok"

    changes = []          # rows whose availability differs from the old proxy
    by_platform_true = Counter()
    yt_new = yt_old = 0

    for r in rows:
        target = _target_flag(r)
        if target:
            by_platform_true[r.get("platform")] += 1
        if r.get("platform") == "YouTube":
            if target:
                yt_new += 1
            else:
                yt_old += 1
        if target != old_proxy(r):
            changes.append((r, old_proxy(r), target))

    plat = Counter(r.get("platform") for r in rows)
    print(f"{'DRY RUN — no writes' if not write else 'WRITING'} · {len(rows)} creators")
    print(f"platforms: {dict(plat)}\n")

    print("=== YouTube: old vs. new data (the real 'is it fixed' answer) ===")
    print(f"  on NEW-flag data (sponsorship measured): {yt_new}")
    print(f"  on OLD data (no measured sponsorship):   {yt_old}")
    print(f"  -> {yt_new}/{plat.get('YouTube', 0)} YouTube creators are on new data.\n")

    print("=== Backfill target (sponsorship_data_available = True) ===")
    print(f"  by platform: {dict(by_platform_true)}")
    print(f"  total True: {sum(by_platform_true.values())} · "
          f"total False: {len(rows) - sum(by_platform_true.values())}\n")

    print("=== Before/after vs OLD proxy (platform == 'TikTok') ===")
    print(f"  rows that MOVE: {len(changes)}")
    for r, was, now in changes[:50]:
        print(f"    {r.get('name','')[:36]:36} {r.get('platform',''):9} {was} -> {now}")
    if not changes:
        print("    (none — every correctly-marked TikTok/YouTube row is unchanged; "
              "the real column matches the old proxy on today's data)")

    if not write:
        print("\n(dry run — pass --write to persist sponsorship_data_available)")
        return

    sb = database._client()
    n = 0
    for r in rows:
        sb.table(database._TABLE).update({
            "sponsorship_data_available": _target_flag(r),
        }).eq("id", r["id"]).execute()
        n += 1
    print(f"\nWrote sponsorship_data_available for {n} rows.")


if __name__ == "__main__":
    main(write="--write" in sys.argv)
