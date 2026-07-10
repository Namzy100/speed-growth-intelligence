"""Re-fetch existing YouTube creators with REAL scraped data + the paid-placement
flag (2026-07). Closes the gap where 217 YouTube creators were scored on pre-flag
data, and the Mimanshi YouTube subset was scored on spreadsheet placeholders
(engagement_quality=7, crypto_content_pct=0.8, engagement_rate=0.03).

We store no channel id, and youtube.py has no by-id fetch — so each creator is
resolved by NAME via search() (type=channel, 100 quota units each). At ~103
units/creator against a 10,000/day quota, ~90 creators fit per day, so this is
DELIBERATELY resumable and run in daily batches: it targets YouTube creators whose
sponsorship_data_available is still False (i.e. not yet re-fetched), and each
successful save flips that True, so the next run continues where this stopped.

Correctness:
  * Saves under the STORED name (not the freshly-fetched title) so save_creator
    upserts the existing row instead of inserting a duplicate.
  * Preserves curation tags (mimanshi_list, fit_N, country) that the YouTube fetch
    would otherwise overwrite — so Mimanshi vetting survives the re-fetch.
  * Verifies the top search result's name reasonably matches the stored name;
    skips + logs otherwise rather than writing the wrong channel's stats.

Run:  python creators/refetch_youtube.py [--limit N] [--write]
      (dry run by default — shows what it WOULD fetch without spending quota
       beyond the searches; pass --write to persist. --limit caps creators.)
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database
from creators.scorer import CreatorScorer
from creators.youtube import YouTubeCreatorFetcher, QuotaExceededError

_UNIT_BUDGET = 9_500          # stay under the 10k/day YouTube quota
_UNITS_PER_CREATOR = 103      # search(100) + channels(1) + playlistItems(1) + videos(1)
_CURATION_TAG_RE = re.compile(r"^(mimanshi_list|fit_[1-5]|mexico|brazil)$", re.I)


def _is_curation_tag(t: str) -> bool:
    return bool(_CURATION_TAG_RE.match(str(t).strip()))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(stored: str, fetched: str) -> bool:
    a, b = _norm(stored), _norm(fetched)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = set(re.findall(r"[a-z0-9]+", stored.lower())), set(re.findall(r"[a-z0-9]+", fetched.lower()))
    if not ta or not tb:
        return False
    jacc = len(ta & tb) / len(ta | tb)
    return jacc >= 0.4


def main(write: bool, limit: int | None) -> None:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("YOUTUBE_API_KEY not set"); sys.exit(1)

    rows = database.get_all_creators()
    targets = [r for r in rows
               if r.get("platform") == "YouTube"
               and not bool(r.get("sponsorship_data_available", False))]
    if limit:
        targets = targets[:limit]

    print(f"{'WRITING' if write else 'DRY RUN'} · {len(targets)} YouTube creators still on "
          f"old/placeholder data (of {sum(1 for r in rows if r.get('platform')=='YouTube')} total YouTube)\n")

    fetcher = YouTubeCreatorFetcher(api_key)
    scorer = CreatorScorer(use_llm_fallback=False)

    units = 0
    stats = {"searched": 0, "matched": 0, "saved": 0, "no_result": 0, "mismatch": 0, "quota_stop": False}
    mismatches = []

    for r in targets:
        if units + _UNITS_PER_CREATOR > _UNIT_BUDGET:
            print(f"\n[budget] stopping before creator '{r.get('name')}' to stay under "
                  f"{_UNIT_BUDGET} units this run.")
            break
        stored_name = r.get("name", "")
        try:
            results = fetcher.search(stored_name, max_results=1)
            units += _UNITS_PER_CREATOR
            stats["searched"] += 1
        except QuotaExceededError:
            # youtube._get retries transient per-100s burst limits with backoff;
            # QuotaExceededError now means the DAILY quota is genuinely spent.
            stats["quota_stop"] = True
            print("\n[quota] YouTube DAILY quota exhausted — stopping, progress saved. "
                  "Resume AFTER the midnight-Pacific reset (only ~one batch fits per "
                  "Pacific day; running twice in the same PT day shares the same 10k).")
            break

        if not results:
            stats["no_result"] += 1
            continue
        fetched = results[0]
        if not _name_matches(stored_name, fetched.get("name", "")):
            stats["mismatch"] += 1
            mismatches.append((stored_name, fetched.get("name", "")))
            continue
        stats["matched"] += 1

        # Preserve curation tags the YouTube fetch would overwrite.
        preserved = [t for t in (r.get("niche_tags") or []) if _is_curation_tag(t)]
        fetched["niche_tags"] = list(dict.fromkeys(preserved + (fetched.get("niche_tags") or [])))
        fetched["name"] = stored_name  # keep identity so upsert updates the existing row

        old = (r.get("engagement_quality"), r.get("crypto_content_pct"), r.get("engagement_rate"))
        new = (fetched.get("engagement_quality"), fetched.get("crypto_content_pct"), fetched.get("engagement_rate"))
        tag = " [MIMANSHI]" if any(str(t).lower() == "mimanshi_list" for t in preserved) else ""
        print(f"  {stored_name[:34]:34}{tag} eq/crypto/er {old} -> {new}  spons_count={fetched.get('sponsorship_count')}")

        if write:
            score = scorer.score(fetched)
            # Preserve the LLM-classified is_influencer. This scorer runs with
            # use_llm_fallback=False (deterministic bulk), so its _detect_influencer
            # would fall back to the keyword rule and REGRESS the LLM classification.
            # Re-fetching doesn't change identity, so the prior LLM verdict holds.
            if r.get("is_influencer") is not None:
                score["is_influencer"] = bool(r.get("is_influencer"))
            database.save_creator(fetched, score)
            stats["saved"] += 1

    print(f"\n=== batch summary ===")
    print(f"  searched: {stats['searched']} · matched: {stats['matched']} · "
          f"saved: {stats['saved']} · no-result: {stats['no_result']} · name-mismatch: {stats['mismatch']}")
    print(f"  YouTube quota units used this run: ~{units} of {_UNIT_BUDGET} budget "
          f"(10,000/day cap). Monetary cost: $0 (YouTube Data API is free within quota).")
    print(f"  creators still needing re-fetch after this run: re-run on the NEXT "
          f"Pacific day to continue (quota resets midnight PT). One batch (~90) per day.")
    if mismatches:
        print(f"\n  name mismatches skipped (stored -> top result), for manual review:")
        for s, f in mismatches[:20]:
            print(f"    {s[:34]:34} -> {f[:34]}")
    if not write:
        print("\n(dry run — pass --write to persist. Dry run still spends search quota.)")


if __name__ == "__main__":
    args = sys.argv[1:]
    lim = None
    if "--limit" in args:
        lim = int(args[args.index("--limit") + 1])
    main(write="--write" in args, limit=lim)
