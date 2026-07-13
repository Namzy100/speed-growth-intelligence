"""Manually record AUDIENCE-location data for a creator, collected during outreach.

Audience geography CANNOT be scraped or inferred — it lives in the creator's own
analytics, which only they can share (e.g. a screenshot during vetting). This is
the entry point for that real, human-collected data. It is never auto-filled;
empty means "not yet collected", full stop.

(The dashboards are static HTML with no backend, so they can't save an input
directly — this CLI is the reachable write path until a backend exists. See the
handover notes.)

Usage:
  python creators/set_audience_location.py --show "Creator Name"
  python creators/set_audience_location.py "Creator Name" "e.g. 62% US, 14% MX, 9% BR (from creator's YT Analytics screenshot, 2026-07)"
  python creators/set_audience_location.py --clear "Creator Name"
Add --platform YouTube to disambiguate if the same name exists on two platforms.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database


def _find(name: str, platform: str | None):
    matches = [r for r in database.get_all_creators()
               if r.get("name", "").strip().lower() == name.strip().lower()
               and (platform is None or r.get("platform") == platform)]
    return matches


def main(argv: list) -> None:
    show = "--show" in argv
    clear = "--clear" in argv
    platform = None
    if "--platform" in argv:
        platform = argv[argv.index("--platform") + 1]
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--") and argv[i - 1] != "--platform"]
    if not positional:
        print(__doc__); sys.exit(1)
    name = positional[0]

    matches = _find(name, platform)
    if not matches:
        sys.exit(f"No creator named '{name}'" + (f" on {platform}" if platform else "") + ".")
    if len(matches) > 1:
        plats = ", ".join(sorted({m.get("platform", "?") for m in matches}))
        sys.exit(f"'{name}' exists on multiple platforms ({plats}). Add --platform <X> to disambiguate.")
    r = matches[0]

    if show:
        val = r.get("audience_location_data")
        print(f"{r['name']} ({r['platform']}): audience_location_data = "
              f"{repr(val) if val else 'not yet collected'}")
        return

    if clear:
        new_val = None
    else:
        if len(positional) < 2:
            sys.exit("Provide the audience-location text as the second argument (or use --clear).")
        new_val = positional[1]

    database._client().table(database._TABLE).update(
        {"audience_location_data": new_val, "updated_at": database._now()}
    ).eq("id", r["id"]).execute()
    print(f"{'Cleared' if clear else 'Set'} audience_location_data for {r['name']} ({r['platform']})."
          + ("" if clear else f"\n  -> {new_val!r}"))


if __name__ == "__main__":
    main(sys.argv[1:])
