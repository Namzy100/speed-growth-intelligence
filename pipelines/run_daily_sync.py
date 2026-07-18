"""Daily orchestration: syncs Adjust install/retention and Meta campaign data to
Google Sheets, then rebuilds the creative performance dashboard.

Scope: this orchestrator syncs the Adjust pipeline (channel overview, campaign
installs, installs by country, retention) and the Meta pipeline (campaign-level spend, impressions,
clicks, mobile app installs), writes a "Last Updated" timestamp, and — as a
final step — rebuilds the self-contained creative dashboard HTML
(docs/creative_dashboard.html) from the freshly-synced sheet data, so it stays
current on every run. The other Speed data sources are standalone and are NOT
yet wired into this orchestrator:

  - creators/      — TikTok/YouTube creator discovery + scoring (persisted to
                     Supabase, run on their own)
  - eu/            — European market analysis (currently unimplemented)
  - intelligence/  — weekly_brief.py and competitor_analysis.py (run manually)

Add them here as separate steps in run() when they are ready for automation.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Allow running directly from any working directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv()

from pipelines import build_creative_dashboard
from pipelines import build_creator_dashboard
from pipelines import build_strategy_dashboard
from pipelines import build_trend_dashboard
from pipelines.adjust import AdjustPipeline
from pipelines.meta import MetaPipeline
from pipelines.sheets import (
    create_sheet_if_missing,
    write_all_adjust_data,
    write_all_meta_data,
    write_country_installs,
    write_dataframe,
    write_meta_creatives,
)


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------------------------------------------
# GitHub Pages deploy target
# ------------------------------------------------------------------
# Dashboards publish to a dedicated Pages branch (default 'gh-pages'), NOT to
# main. Reason: the TrySpeed org enforces a `require-linked-issue` ruleset on
# main/development/master, so a bot pushing generated output straight to main is
# rejected. gh-pages sits outside that ruleset, needs no policy exception, and
# keeps generated dashboard output out of main's history. The branch mirrors the
# docs/ directory at its ROOT (so Pages serves gh-pages / root == today's URLs).
_PAGES_BRANCH = os.getenv("PAGES_BRANCH", "gh-pages")


def _trend_rebuild_active() -> bool:
    """True only when the weekly trend rebuild actually runs this cycle (flag set
    AND Monday). Gates both the rebuild itself and whether the regenerated
    dashboard_state.json is pushed to the Pages branch — on any other day the
    working-tree copy is a stale CI checkout and must NOT overwrite the Pages one."""
    return bool(os.getenv("TREND_DASHBOARD_REBUILD")) and \
        datetime.now(timezone.utc).weekday() == 0  # 0 = Monday


def _hydrate_pages_state() -> None:
    """Pull the authoritative dashboard_state.json from the Pages branch into the
    working tree before the trend rebuild reads it. Since that state now lives on
    gh-pages (not main), a fresh CI checkout of main would otherwise read a stale
    copy and reset the trend kanban/results. Best-effort: on the very first run
    (no Pages branch yet) it leaves the working-tree copy untouched."""
    try:
        if subprocess.run(["git", "fetch", "origin", _PAGES_BRANCH],
                          cwd=_ROOT, capture_output=True).returncode != 0:
            _log("Trend state: no Pages branch yet — using working-tree state.")
            return
        # gh-pages mirrors docs/ at root, so the file is at '<branch>:dashboard_state.json'.
        blob = subprocess.run(["git", "show", f"origin/{_PAGES_BRANCH}:dashboard_state.json"],
                              cwd=_ROOT, capture_output=True, text=True)
        if blob.returncode == 0 and blob.stdout.strip():
            (_ROOT / "docs" / "dashboard_state.json").write_text(blob.stdout, encoding="utf-8")
            _log("Trend state: hydrated dashboard_state.json from the Pages branch.")
    except Exception as e:  # never block the rebuild on hydration
        _log(f"Trend state: hydrate skipped ({e})")


# ------------------------------------------------------------------
# Pipeline steps
# ------------------------------------------------------------------

def _sync_adjust(spreadsheet_id: str) -> bool:
    """Pull Adjust data and write each report to its sheet tab.

    Returns True if all writes succeeded, False if any step failed.
    Individual sheet failures are logged but do not abort the others.
    """
    _log("Adjust: pulling last 30 days...")
    try:
        data = AdjustPipeline().get_all(days=30)
    except Exception as e:
        _log(f"Adjust: pull FAILED — {e}")
        return False

    # Single source of truth for the write loop lives in sheets.py.
    return write_all_adjust_data(data, spreadsheet_id, log=_log)


def _sync_country_installs(spreadsheet_id: str) -> bool:
    """Pull Adjust installs broken down by country and write the Country Installs tab.

    Returns True if the write succeeded (or was an empty no-op), False on failure.
    """
    _log("Adjust (country): pulling installs by country, last 30 days...")
    try:
        df = AdjustPipeline().get_installs_by_country(days=30)
    except Exception as e:
        _log(f"Adjust (country): pull FAILED — {e}")
        return False

    return write_country_installs(df, spreadsheet_id, log=_log)


_META_STATUS = _ROOT / "data" / "processed" / "meta_sync_status.json"


def _record_meta_status(success: bool) -> None:
    """Persist Meta sync status so the dashboard can show a staleness label.

    Keeps the last *successful* sync date so the creative dashboard can render
    'Data as of <date> — live sync pending' when the integration is down.
    """
    import json
    prev = {}
    try:
        prev = json.loads(_META_STATUS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = {
        "last_attempt": now,
        "success": success,
        "last_success": now if success else prev.get("last_success"),
    }
    _META_STATUS.parent.mkdir(parents=True, exist_ok=True)
    _META_STATUS.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _sync_meta(spreadsheet_id: str) -> bool:
    """Pull Meta campaign data and write it to the Meta Campaigns tab.

    Returns True if all writes succeeded, False if any step failed.
    Individual sheet failures are logged but do not abort the others.
    Records Meta sync status for the dashboard staleness label.
    """
    _log("Meta: pulling last 30 days...")
    try:
        data = MetaPipeline().get_all(days=30)
    except Exception as e:
        _log(f"Meta: pull FAILED — {e}")
        _record_meta_status(False)
        return False

    # Single source of truth for the write loop lives in sheets.py.
    ok = write_all_meta_data(data, spreadsheet_id, log=_log)
    _record_meta_status(ok)
    return ok


def _sync_meta_creatives(spreadsheet_id: str) -> bool:
    """Pull Meta ad-level (creative) data and write it to the Meta Creatives tab.

    Runs after the campaign-level Meta step. Returns True if the write succeeded
    (or was an empty no-op), False on failure. Like the other steps, a failure is
    logged but does not abort the remaining sync.
    """
    _log("Meta (creatives): pulling ad-level data, last 30 days...")
    try:
        df = MetaPipeline().get_creative_performance(days=30)
    except Exception as e:
        _log(f"Meta (creatives): pull FAILED — {e}")
        return False

    return write_meta_creatives(df, spreadsheet_id, log=_log)


def _write_last_updated(spreadsheet_id: str) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    df = pd.DataFrame({"last_sync": [now_str]})
    create_sheet_if_missing(spreadsheet_id, "Last Updated")
    write_dataframe(df, spreadsheet_id, "Last Updated")
    _log(f"Last Updated: '{now_str}'")


def _rebuild_dashboard() -> bool:
    """Rebuild the creative dashboard HTML from the freshly-synced sheet data.

    Runs last so it reflects the Adjust writes and the new Last Updated stamp.
    Pulls the sheet again and regenerates docs/creative_dashboard.html.
    """
    _log("Dashboard: rebuilding docs/creative_dashboard.html from latest data...")
    try:
        build_creative_dashboard.main()
        _log("Dashboard: rebuilt successfully")
        return True
    except Exception as e:
        _log(f"Dashboard: rebuild FAILED — {e}")
        return False


def _rebuild_creator_dashboard() -> bool:
    """Rebuild the creator dashboard HTML from live Supabase data.

    Runs after the creative dashboard so both stay fresh on each daily sync.
    """
    _log("Creator dashboard: rebuilding docs/creator_dashboard.html from Supabase...")
    try:
        build_creator_dashboard.main()
        _log("Creator dashboard: rebuilt successfully")
        return True
    except Exception as e:
        _log(f"Creator dashboard: rebuild FAILED — {e}")
        return False


def _rebuild_strategy_dashboard() -> bool:
    """Rebuild the strategy & market-intelligence dashboard every day.

    Runs daily like the creative and creator dashboards — measured at ~21s and 4
    Claude calls, so the daily cost is small. The source docs (eu_gtm_plan,
    eu_channel_strategy, competitor analyses, fintech strategies) change
    infrequently, so most days the extracted content is similar; the value is a
    fresh sync timestamp and automatically picking up any new/updated source docs.
    A failure is logged but never aborts the sync.
    """
    _log("Strategy dashboard: rebuilding docs/strategy_dashboard.html via Claude...")
    try:
        build_strategy_dashboard.main()
        _log("Strategy dashboard: rebuilt successfully")
        return True
    except Exception as e:
        _log(f"Strategy dashboard: rebuild FAILED — {e}")
        return False


def _rebuild_trend_dashboard() -> bool:
    """Rebuild the trend-intelligence dashboard — weekly (Mondays only).

    Gated behind TREND_DASHBOARD_REBUILD (it makes Apify + YouTube + Claude calls),
    and only fires on Mondays since the trend data is a weekly window. When the flag
    is unset or it's not Monday, the step is skipped and counts as a success.
    """
    if not os.getenv("TREND_DASHBOARD_REBUILD"):
        _log("Trend dashboard: skipped (set TREND_DASHBOARD_REBUILD=1 to enable).")
        return True
    if datetime.now(timezone.utc).weekday() != 0:  # 0 = Monday
        _log("Trend dashboard: skipped (only rebuilds on Mondays).")
        return True
    # Trend state (dashboard_state.json) now persists on the Pages branch, not
    # main. Hydrate the latest copy BEFORE the rebuild reads it, so a fresh CI
    # checkout of main doesn't merge onto stale state.
    _hydrate_pages_state()
    # Auto-fill posted paid-brief results from Meta/Adjust BEFORE the rebuild, so
    # the imported numbers are already in dashboard_state.json when it bakes.
    try:
        from pipelines import import_results
        _log("Trend results: importing posted paid results from Meta/Adjust...")
        import_results.run()
    except Exception as e:  # best-effort — never block the rebuild
        _log(f"Trend results: import skipped ({e})")

    _log("Trend dashboard: rebuilding docs/trend_dashboard.html (YouTube+TikTok+Claude)...")
    try:
        build_trend_dashboard.main()
        _log("Trend dashboard: rebuilt successfully")
        ok = True
    except Exception as e:
        _log(f"Trend dashboard: rebuild FAILED — {e}")
        ok = False

    # Outcomes-graded quality check of the relevance/fit judgments — rides this
    # same Monday cadence (no separate scheduler). Best-effort: it runs a Managed
    # Agent session (~5-12 min) and NEVER blocks or fails the sync. Verdict + full
    # revision trace are logged under docs/trend_checker_log/.
    try:
        from intelligence import trend_checker
        _log("Trend checker: grading relevance/fit judgments via Outcomes...")
        v = trend_checker.check_pipeline_output()
        _log(f"Trend checker: verdict={v.get('verdict')} "
             f"(grader iterations={v.get('iterations')}) — see docs/trend_checker_log/")
    except Exception as e:  # never block the sync on the checker
        _log(f"Trend checker: skipped ({e})")
    return ok


def _deploy_dashboard() -> bool:
    """Publish the rebuilt dashboards to the GitHub Pages branch (gh-pages).

    Gated behind DASHBOARD_AUTODEPLOY. Pushes to _PAGES_BRANCH (not main), via a
    throwaway git worktree so main's checkout is never touched. On the FIRST run
    the branch doesn't exist yet, so it's seeded (orphan) from the FULL docs/ tree
    — every routing index.html + asset the site needs, not just the dashboards.
    On later runs only the freshly rebuilt files are updated. dashboard_state.json
    is pushed ONLY on a real weekly trend rebuild (see _trend_rebuild_active),
    otherwise the stale CI copy would clobber the persisted state on the branch.
    Best-effort: any git/push failure is logged but never aborts the data sync.
    """
    if not os.getenv("DASHBOARD_AUTODEPLOY"):
        _log("Auto-deploy: skipped (set DASHBOARD_AUTODEPLOY=1 to publish dashboards).")
        return True

    import shutil
    import tempfile

    branch = _PAGES_BRANCH
    docs = _ROOT / "docs"
    built = ["creative_dashboard.html", "creator_dashboard.html",
             "strategy_dashboard.html", "trend_dashboard.html"]
    if _trend_rebuild_active():
        built.append("dashboard_state.json")  # regenerated this cycle → safe to publish

    worktree = Path(tempfile.mkdtemp(prefix="pages-deploy-"))
    try:
        subprocess.run(["git", "fetch", "origin", branch], cwd=_ROOT, capture_output=True)
        exists = subprocess.run(["git", "rev-parse", "--verify", f"origin/{branch}"],
                                cwd=_ROOT, capture_output=True).returncode == 0
        if exists:
            subprocess.run(["git", "worktree", "add", "--force", str(worktree),
                            "-B", branch, f"origin/{branch}"], cwd=_ROOT, check=True)
            for f in built:  # update only the freshly built files
                if (docs / f).exists():
                    shutil.copy2(docs / f, worktree / f)
        else:
            # First publish: orphan branch seeded with the whole site scaffolding.
            subprocess.run(["git", "worktree", "add", "--force", "--detach", str(worktree)],
                           cwd=_ROOT, check=True)
            subprocess.run(["git", "checkout", "--orphan", branch], cwd=worktree, check=True)
            subprocess.run(["git", "rm", "-rf", "."], cwd=worktree, capture_output=True)
            for item in docs.iterdir():
                dest = worktree / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            _log(f"Auto-deploy: seeding new '{branch}' from the full docs/ tree.")

        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=worktree).returncode == 0:
            _log("Auto-deploy: no dashboard changes to publish.")
            return True
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "commit", "-m", f"chore: auto-deploy dashboard refresh {stamp}"],
                       cwd=worktree, check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree, check=True)
        _log(f"Auto-deploy: published dashboards to '{branch}' — GitHub Pages will redeploy.")
        return True
    except Exception as e:  # noqa: BLE001 — deploy must never break the data sync
        _log(f"Auto-deploy: FAILED (non-fatal) — {e}")
        return False
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=_ROOT, capture_output=True)


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def run() -> None:
    _log("Starting daily sync")

    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        _log("FATAL: GOOGLE_SHEETS_ID must be set in .env")
        sys.exit(1)

    results: dict[str, bool] = {}

    # Adjust
    results["Adjust"] = _sync_adjust(spreadsheet_id)

    # Adjust installs-by-country — feeds the EU market analysis.
    results["Country Installs"] = _sync_country_installs(spreadsheet_id)

    # Meta — refreshes alongside Adjust each day.
    results["Meta"] = _sync_meta(spreadsheet_id)

    # Meta creatives — ad-level breakdown, pulled after the campaign-level step.
    results["Meta Creatives"] = _sync_meta_creatives(spreadsheet_id)

    # Last Updated timestamp
    try:
        _write_last_updated(spreadsheet_id)
        results["Last Updated"] = True
    except Exception as e:
        _log(f"Last Updated: FAILED — {e}")
        results["Last Updated"] = False

    # Creative dashboard — rebuilt from the freshly-synced data.
    results["Dashboard"] = _rebuild_dashboard()

    # Creator dashboard — rebuilt from live Supabase data so both stay fresh.
    results["Creator Dashboard"] = _rebuild_creator_dashboard()

    # Strategy dashboard — rebuilt daily (~21s, 4 Claude calls) so its timestamp
    # stays fresh and new/updated source docs are picked up automatically.
    results["Strategy Dashboard"] = _rebuild_strategy_dashboard()

    # Trend dashboard — weekly (Mondays), gated behind TREND_DASHBOARD_REBUILD.
    results["Trend Dashboard"] = _rebuild_trend_dashboard()

    # Auto-deploy: publish the refreshed dashboards to the GitHub Pages branch (opt-in).
    results["Deploy"] = _deploy_dashboard()

    # Summary
    succeeded = sum(results.values())
    total = len(results)
    _log(f"Sync complete — {succeeded}/{total} steps succeeded")
    if succeeded < total:
        failed = [name for name, ok in results.items() if not ok]
        _log(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    run()
