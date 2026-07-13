"""Backfill creator_country (the creator's STATED country, not audience geography).

Real signal only, never inferred:
  * Mimanshi market tag (Mexico/Brazil) -> MX/BR. FREE (from stored niche_tags).
  * YouTube snippet.country -> ISO code. Requires re-resolving the channel by name
    (we store no channel id), which is a 100-unit search each — quota-limited and
    resumable, exactly like refetch_youtube.py. Rides for free on any normal
    re-fetch going forward; this script is for backfilling the already-fetched set.
  * Everything else (all TikTok, X without a signal, YouTube with null country) ->
    stays 'unknown'. Never guessed.

Run:
  python creators/backfill_creator_country.py [--write]            # free Mimanshi pass
  python creators/backfill_creator_country.py --youtube [--write]  # + YouTube re-resolve (quota)

Requires the creator_country column (run ADD_CREATOR_COUNTRY_COLUMN_SQL first).
Dry run by default; --write persists.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database
from creators.database import derive_creator_country
from creators.youtube import YouTubeCreatorFetcher, QuotaExceededError

_UNIT_BUDGET = 9_500
_UNITS_PER_CREATOR = 103


def _has_mimanshi_country(r: dict) -> bool:
    return any(str(t).strip().lower() in ("mexico", "brazil") for t in (r.get("niche_tags") or []))


def _name_norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main(write: bool, do_youtube: bool) -> None:
    rows = database.get_all_creators()
    sb = database._client()

    # --- Pass 1: FREE Mimanshi market-tag backfill (MX/BR) ---
    mim_updates = 0
    for r in rows:
        if _has_mimanshi_country(r):
            code = derive_creator_country(r.get("niche_tags"), None)  # Mimanshi tag wins
            if r.get("creator_country") != code:
                if write:
                    sb.table(database._TABLE).update({"creator_country": code}).eq("id", r["id"]).execute()
                r["creator_country"] = code
                mim_updates += 1
    print(f"Mimanshi market-tag backfill: {mim_updates} set from MX/BR tags "
          f"({'written' if write else 'dry run'}).")

    # --- Pass 2 (optional): YouTube snippet.country re-resolution (quota-limited) ---
    if do_youtube:
        api_key = os.getenv("YOUTUBE_API_KEY")
        if not api_key:
            print("YOUTUBE_API_KEY not set — skipping YouTube pass."); do_youtube = False
    if do_youtube:
        fetcher = YouTubeCreatorFetcher(api_key)
        targets = [r for r in rows if r.get("platform") == "YouTube"
                   and (r.get("creator_country") or "unknown") == "unknown"
                   and not _has_mimanshi_country(r)]
        print(f"\nYouTube re-resolution: {len(targets)} YouTube creators still 'unknown'.")
        units = 0
        got = skipped = 0
        for r in targets:
            if units + _UNITS_PER_CREATOR > _UNIT_BUDGET:
                print(f"[budget] stopping to stay under {_UNIT_BUDGET} units this run."); break
            try:
                results = fetcher.search(r.get("name", ""), max_results=1)
                units += _UNITS_PER_CREATOR
            except QuotaExceededError:
                print("[quota] YouTube DAILY quota exhausted — stopping; resume next Pacific day."); break
            except Exception as e:
                skipped += 1
                print(f"  !! skipped '{r.get('name')}' — {str(e).replace(api_key, '***')[:120]}")
                continue
            if not results:
                continue
            f = results[0]
            if _name_norm(r.get("name", "")) not in _name_norm(f.get("name", "")) \
               and _name_norm(f.get("name", "")) not in _name_norm(r.get("name", "")):
                continue
            code = derive_creator_country(r.get("niche_tags"), f.get("channel_country"))
            if code != "unknown":
                if write:
                    sb.table(database._TABLE).update({"creator_country": code}).eq("id", r["id"]).execute()
                r["creator_country"] = code
                got += 1
                print(f"  {r.get('name','')[:34]:34} -> {code}")
        print(f"YouTube pass: {got} resolved to a real country, {skipped} skipped, ~{units} units used.")

    _report(database.get_all_creators() if write else rows)


def _report(rows: list) -> None:
    from collections import Counter
    n = len(rows)
    src = Counter()
    for r in rows:
        cc = r.get("creator_country") or "unknown"
        if cc == "unknown":
            src["unknown"] += 1
        elif _has_mimanshi_country(r):
            src["Mimanshi tag"] += 1
        else:
            src["platform (YouTube snippet.country)"] += 1
    real = n - src["unknown"]
    print(f"\n=== creator_country coverage: {real}/{n} have a real country "
          f"({100*real/n:.0f}%), {src['unknown']} unknown ===")
    for k, v in src.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    args = sys.argv[1:]
    main(write="--write" in args, do_youtube="--youtube" in args)
