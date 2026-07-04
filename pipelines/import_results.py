"""Auto-import real performance results into the trend dashboard state.

Fills the `results` block of POSTED paid briefs in docs/dashboard_state.json from
the EXISTING Adjust + Meta pipelines — so paid results stop needing manual entry.

Reuses (does NOT re-integrate) pipelines/meta.py (MetaPipeline) and
pipelines/adjust.py (AdjustPipeline).

Matching (see the module note below): a dashboard item carries no campaign/ad id,
so paid briefs are matched by an `ad_ref` field — the Meta campaign/ad NAME the
marketer launched the brief under (set when it moves to in_production). We match
Meta rows where ad_ref is a case-insensitive substring of campaign_name or
ad_name, aggregate spend/impressions/installs, and compute CPI = spend / installs.

Scope / what is NOT automated:
  * ORGANIC posts have no auto path. Adjust has no organic data, and there is no
    UTM scheme or post-ID tagging in this repo. Organic results stay MANUAL.
  * Paid briefs WITHOUT an ad_ref are skipped (nothing to match on).

Flags:
  --dry-run   show what would change, write nothing
  --force     overwrite results even if they were entered manually
  --days N    Meta/Adjust lookback window (default 30)

Run from repo root:  python pipelines/import_results.py [--dry-run] [--force]
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

import json

from pipelines.adjust import AdjustPipeline
from pipelines.meta import MetaPipeline

_STATE_FILE = _ROOT / "docs" / "dashboard_state.json"
_STATUS_FLOW = ["suggested", "briefed", "in_production", "posted", "results_in"]
_POSTED_INDEX = _STATUS_FLOW.index("posted")   # import for 'posted' or later


def _is_posted_or_later(status: str) -> bool:
    return status in _STATUS_FLOW and _STATUS_FLOW.index(status) >= _POSTED_INDEX


def _has_manual_results(item: dict) -> bool:
    """True if the item carries hand-entered results (present, not importer-set)."""
    if item.get("results_source") == "auto":
        return False
    r = item.get("results") or {}
    return any(r.get(k) not in (None, "") for k in ("views", "er", "saves", "spend", "installs", "cpi"))


def _match_meta(ad_ref: str, meta_df) -> dict | None:
    ref = ad_ref.strip().lower()
    if not ref:
        return None
    m = meta_df[
        meta_df["campaign_name"].fillna("").str.lower().str.contains(ref, regex=False)
        | meta_df["ad_name"].fillna("").str.lower().str.contains(ref, regex=False)
    ]
    if m.empty:
        return None
    spend = float(m["spend"].fillna(0).sum())
    installs = int(m["mobile_app_install"].fillna(0).sum())
    impressions = int(m["impressions"].fillna(0).sum())
    return {
        "spend": round(spend, 2), "installs": installs, "impressions": impressions,
        "cpi": round(spend / installs, 2) if installs else None,
        "matched_ads": int(len(m)),
        "campaigns": sorted(x for x in m["campaign_name"].dropna().unique().tolist()),
    }


def _match_adjust_installs(ad_ref: str, adjust_df) -> int | None:
    """Corroborating install count from Adjust (matched on campaign_network name)."""
    if adjust_df is None or adjust_df.empty or "campaign_network" not in adjust_df.columns:
        return None
    ref = ad_ref.strip().lower()
    m = adjust_df[adjust_df["campaign_network"].fillna("").str.lower().str.contains(ref, regex=False)]
    if m.empty:
        return None
    return int(m["installs"].fillna(0).sum())


def run(dry_run: bool = False, force: bool = False, days: int = 30) -> None:
    if not _STATE_FILE.exists():
        print(f"No state file at {_STATE_FILE} — run build_trend_dashboard first.")
        return
    state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    items = state.get("items", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    paid_posted = [it for it in items.values()
                   if it.get("type") == "paid" and _is_posted_or_later(it.get("status", ""))]
    organic_posted = [it for it in items.values()
                      if it.get("type") == "organic" and _is_posted_or_later(it.get("status", ""))]

    print(f"Posted+ items — paid: {len(paid_posted)}, organic: {len(organic_posted)}")
    if organic_posted:
        print(f"  {len(organic_posted)} organic post(s) posted — NOT auto-importable "
              "(no organic tracking); results stay manual.")

    if not paid_posted:
        print("No posted paid briefs to import. Nothing to do.")
        return

    print(f"Pulling Meta creative + Adjust campaign data (last {days}d)...")
    meta_df = MetaPipeline().get_creative_performance(days=days)
    try:
        adjust_df = AdjustPipeline().get_installs_by_campaign(days=days)
    except Exception as e:
        print(f"  Adjust lookup unavailable ({e}); proceeding with Meta only.")
        adjust_df = None

    filled, skipped = 0, 0
    for it in paid_posted:
        iid = it["id"]
        ad_ref = (it.get("ad_ref") or "").strip()
        if not ad_ref:
            print(f"  SKIP [{iid[:44]}] — no ad_ref set (assign the Meta campaign/ad name at in_production).")
            skipped += 1
            continue
        if _has_manual_results(it) and not force:
            print(f"  KEEP [{iid[:44]}] — manual results present; not overwriting (use --force).")
            skipped += 1
            continue

        match = _match_meta(ad_ref, meta_df)
        if not match:
            print(f"  MISS [{iid[:44]}] — ad_ref {ad_ref!r} matched no Meta ads.")
            skipped += 1
            continue

        adj = _match_adjust_installs(ad_ref, adjust_df)
        results = {
            "views": None, "er": None, "saves": None,     # organic-oriented manual fields
            "spend": match["spend"], "impressions": match["impressions"],
            "installs": match["installs"], "cpi": match["cpi"],
            "adjust_installs": adj, "matched_ads": match["matched_ads"],
            "imported_at": today,
        }
        cpi_str = f"${match['cpi']:.2f}" if match["cpi"] is not None else "n/a"
        adj_str = f", adjust installs {adj}" if adj is not None else ""
        print(f"  AUTO [{iid[:44]}] ad_ref={ad_ref!r} -> {match['matched_ads']} ads "
              f"({', '.join(match['campaigns'])}): ${match['spend']:,.2f} spend, "
              f"{match['installs']} installs, CPI {cpi_str}{adj_str}")
        if not dry_run:
            it["results"] = results
            it["results_source"] = "auto"
        filled += 1

    if dry_run:
        print(f"\nDRY RUN — would fill {filled}, skip {skipped}. No file written.")
        return

    state["updated_at"] = today
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\nFilled {filled} paid item(s), skipped {skipped}. Wrote {_STATE_FILE.relative_to(_ROOT)}.")


if __name__ == "__main__":
    days = 30
    for a in sys.argv[1:]:
        if a.startswith("--days="):
            days = int(a.split("=", 1)[1])
    run(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv, days=days)
