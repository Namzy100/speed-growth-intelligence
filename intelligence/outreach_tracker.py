"""CLI to track creator outreach status against the live Supabase table.

Commands:
  list     [--segment S] [--status S]   List creators with status/segment/score/updated.
  update   "<name>" <status>            Set a creator's outreach status (matched by name).
  summary                               Count of creators in each outreach stage.

Statuses: not_contacted, contacted, responded, in_negotiation, declined, confirmed.

Run from repo root, e.g.:
  python intelligence/outreach_tracker.py list
  python intelligence/outreach_tracker.py update "Matt's Crypto" contacted
  python intelligence/outreach_tracker.py summary
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from creators import database

# Display order for stages (funnel order, not alphabetical).
_STAGE_ORDER = [
    "not_contacted", "contacted", "responded",
    "in_negotiation", "confirmed", "declined",
]


def _fmt_updated(ts) -> str:
    """Trim an ISO timestamp to 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return "—"
    s = str(ts).replace("T", " ")
    return s[:16]


def _stage_sort_key(status: str) -> int:
    return _STAGE_ORDER.index(status) if status in _STAGE_ORDER else len(_STAGE_ORDER)


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

def cmd_list(segment: str | None, status: str | None) -> None:
    rows = database.get_all_creators()  # ordered by composite_score desc
    if segment:
        rows = [r for r in rows if r.get("segment_tag") == segment]
    if status:
        rows = [r for r in rows if r.get("outreach_status") == status]

    if not rows:
        print("No creators match.")
        return

    print(f"{'STATUS':<15} {'SEGMENT':<15} {'SCORE':>6}  {'UPDATED':<16}  NAME")
    print("-" * 88)
    for r in rows:
        print(f"{r.get('outreach_status', '—'):<15} "
              f"{r.get('segment_tag', '—'):<15} "
              f"{r.get('composite_score', 0):>6}  "
              f"{_fmt_updated(r.get('updated_at')):<16}  "
              f"{r.get('name', '')}")
    print(f"\n{len(rows)} creator(s).")


def cmd_update(name: str, status: str) -> None:
    if status not in database._VALID_STATUSES:
        print(f"Invalid status '{status}'. Valid: {sorted(database._VALID_STATUSES)}")
        sys.exit(1)

    rows = database.get_all_creators()
    nl = name.strip().lower()
    matches = [r for r in rows if r.get("name", "").lower() == nl]
    if not matches:
        matches = [r for r in rows if nl in r.get("name", "").lower()]

    if not matches:
        print(f"No creator found matching '{name}'.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"'{name}' is ambiguous — {len(matches)} matches. Be more specific:")
        for r in matches:
            print(f"  - {r['name']} [{r['platform']}] ({r.get('outreach_status')})")
        sys.exit(1)

    r = matches[0]
    old = r.get("outreach_status")
    try:
        updated = database.update_outreach_status(r["id"], status)
    except Exception as e:  # noqa: BLE001 — surface DB/constraint errors cleanly
        print(f"Update failed for '{r['name']}': {e}")
        print("If the status is 'declined'/'confirmed', the DB CHECK constraint may "
              "not be migrated yet — run database.ADD_OUTREACH_STATUSES_SQL first.")
        sys.exit(1)

    print(f"Updated '{r['name']}' [{r['platform']}]: {old} -> "
          f"{updated.get('outreach_status', status)}")


def cmd_summary() -> None:
    rows = database.get_all_creators()
    counts = {s: 0 for s in _STAGE_ORDER}
    for r in rows:
        s = r.get("outreach_status", "not_contacted")
        counts[s] = counts.get(s, 0) + 1

    print("OUTREACH PIPELINE SUMMARY")
    print("-" * 32)
    for stage in sorted(counts, key=_stage_sort_key):
        print(f"  {stage:<16} {counts[stage]:>4}")
    print("-" * 32)
    print(f"  {'total':<16} {len(rows):>4}")


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Track creator outreach status.")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List creators with outreach status.")
    p_list.add_argument("--segment", default=None, help="Filter by segment_tag.")
    p_list.add_argument("--status", default=None, help="Filter by outreach_status.")

    p_update = sub.add_parser("update", help="Update a creator's outreach status.")
    p_update.add_argument("name", help="Creator name (exact, else substring match).")
    p_update.add_argument("status", help=f"One of: {sorted(database._VALID_STATUSES)}")

    sub.add_parser("summary", help="Count creators per outreach stage.")

    args = parser.parse_args()

    if args.command == "update":
        cmd_update(args.name, args.status)
    elif args.command == "summary":
        cmd_summary()
    else:  # default / "list"
        seg = getattr(args, "segment", None)
        st = getattr(args, "status", None)
        cmd_list(seg, st)


if __name__ == "__main__":
    try:
        main()
    except EnvironmentError as e:
        print(f"Config error: {e}")
        sys.exit(1)
