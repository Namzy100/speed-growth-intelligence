"""Speed morning brief — a real, prioritized status check across the project's own
infrastructure. NOT a generic calendar/inbox summary: every line is pulled from a
real Speed source and is honest about staleness or unknowns (no invented urgency).

What it checks each run:
  1. GitHub Actions — did the daily-sync workflow fire + succeed on its last run?
  2. Evaluator health — the latest evaluation_report_*.txt (the one that emails on
     schedule): overall score, anything WARN/FAIL, open issues, and how stale it is.
  3. Creator pipeline — total creators, YouTube sponsorship coverage (X of Y),
     backfill remaining.
  4. Open PRs — anything unmerged and waiting (real actionable work).
  5. Blockers — anything explicitly flagged in data/processed/blockers.md.

Output is prioritized: anything broken or blocked leads, then real numbers, then
routine status. Written to data/processed/morning_brief.md (kept OUT of docs/ so it
never lands on the public Pages site) and printed to stdout.

This is deliberately a STANDALONE manual script — it is NOT wired into the daily-sync
workflow's schedule. Run it by hand:

  python pipelines/morning_brief.py
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

_DOCS = _ROOT / "docs"
_OUT = _ROOT / "data" / "processed" / "morning_brief.md"
_BLOCKERS = _ROOT / "data" / "processed" / "blockers.md"

# Severity buckets — controls ordering + emoji in the rendered brief.
ALERT, INFO, ROUTINE = "alert", "info", "routine"
_EMOJI = {ALERT: "🔴", INFO: "📊", ROUTINE: "✅"}

# A scheduled daily job is "late" if the newest run is older than this.
_STALE_RUN_HOURS = 28
# The evaluator report is flagged stale past this many days.
_STALE_EVAL_DAYS = 2


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(dt: datetime) -> str:
    """Human 'Nh ago' / 'Nd ago' from a tz-aware datetime."""
    secs = (_now() - dt).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.1f}d ago"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _github_repo() -> str | None:
    """owner/repo derived from origin, so the brief follows the repo across the
    migration (no hardcoded name to go stale)."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=_ROOT,
                             capture_output=True, text=True).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _gh_token() -> str | None:
    """Read the GitHub token from the local credential store. Never printed."""
    try:
        out = subprocess.run(["git", "credential", "fill"],
                             input="protocol=https\nhost=github.com\n\n",
                             cwd=_ROOT, capture_output=True, text=True).stdout
        return next((l[9:] for l in out.splitlines() if l.startswith("password=")), None)
    except Exception:
        return None


def _finding(severity: str, title: str, detail: str = "") -> dict:
    return {"severity": severity, "title": title, "detail": detail}


# ------------------------------------------------------------------
# 1. GitHub Actions — daily-sync last run
# ------------------------------------------------------------------

def check_actions(repo: str | None, headers: dict | None) -> dict:
    if not repo or not headers:
        return _finding(ROUTINE, "GitHub Actions: not checked",
                        "no origin remote or GitHub token available")
    try:
        api = f"https://api.github.com/repos/{repo}/actions/workflows/daily-sync.yml/runs"
        runs = requests.get(api, headers=headers, params={"per_page": 10},
                            timeout=30).json().get("workflow_runs", [])
    except Exception as e:
        return _finding(ROUTINE, "GitHub Actions: check failed", str(e)[:80])
    if not runs:
        return _finding(ALERT, "daily-sync has NEVER run on this repo",
                        "no workflow runs found — expected a daily 04:00 UTC run")

    latest = runs[0]
    when = _parse_iso(latest["created_at"])
    concl = latest.get("conclusion") or latest.get("status")
    ev = latest.get("event")
    line = f"last run {_age(when)} ({ev}) — {concl}"
    # Newest run failed → top alert.
    if concl not in ("success", "in_progress", "queued"):
        return _finding(ALERT, f"daily-sync last run FAILED ({concl})",
                        f"{line}\n  {latest.get('html_url', '')}")
    # Newest run is stale (didn't fire) → alert.
    if (_now() - when).total_seconds() > _STALE_RUN_HOURS * 3600:
        return _finding(ALERT, f"daily-sync hasn't fired in {_age(when)}",
                        f"expected daily (04:00 UTC); {line}")
    if concl in ("in_progress", "queued"):
        return _finding(ROUTINE, f"daily-sync currently {concl}", line)
    return _finding(ROUTINE, "daily-sync healthy", line)


# ------------------------------------------------------------------
# 2. Evaluator health report
# ------------------------------------------------------------------

def _latest_eval_report() -> Path | None:
    reports = list(_DOCS.glob("evaluation_report_*.txt"))
    if not reports:
        return None
    # Sort by the DATE in the filename, not mtime.
    def key(p: Path):
        m = re.search(r"(\d{4})_(\d{2})_(\d{2})", p.name)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return sorted(reports, key=key)[-1]


def check_evaluator() -> list[dict]:
    report = _latest_eval_report()
    if not report:
        return [_finding(ROUTINE, "Evaluator: no report found",
                         "no docs/evaluation_report_*.txt yet")]
    text = report.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})", report.name)
    rdate = datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc)
    age_days = (_now() - rdate).days
    overall = re.search(r"Overall Score:\s*([\d.]+)", text)
    overall_s = overall.group(1) if overall else "?"

    findings = []
    # Any WARN/FAIL category lines are real signal.
    bad = [ln.strip() for ln in text.splitlines()
           if ln.strip().startswith(("[FAIL]", "[WARN]"))]
    fails = [ln for ln in bad if ln.startswith("[FAIL]")]
    if fails:
        findings.append(_finding(ALERT, f"Evaluator: {len(fails)} FAIL",
                                 "\n  ".join(fails)))
    elif bad:
        findings.append(_finding(INFO, f"Evaluator: {len(bad)} WARN",
                                 "\n  ".join(bad)))

    # Explicit issues section.
    issues = re.search(r"ISSUES TO FIX:\s*\n(.*?)(?:\n\s*\n|\nSCORE TREND|\Z)",
                       text, re.S)
    if issues:
        body = [ln.strip() for ln in issues.group(1).splitlines() if ln.strip()]
        if body and not any("None" in ln for ln in body):
            findings.append(_finding(ALERT, "Evaluator: open issues",
                                     "\n  ".join(body)))

    # Staleness is itself a real signal (the eval emails on schedule; if the newest
    # report is days old the scheduled evaluator likely hasn't run).
    stale = f"  ⚠ report is {age_days}d old" if age_days > _STALE_EVAL_DAYS else ""
    sev = INFO if not findings else findings[0]["severity"]
    findings.insert(0, _finding(sev, f"Evaluator overall {overall_s}/10 ({report.name})",
                                f"as of {rdate:%Y-%m-%d} ({age_days}d ago){stale}"))
    return findings


# ------------------------------------------------------------------
# 3. Creator pipeline snapshot
# ------------------------------------------------------------------

def check_creators() -> dict:
    try:
        from creators import database
        rows = database.get_all_creators()
    except Exception as e:
        return _finding(ROUTINE, "Creator pipeline: unavailable", str(e)[:80])
    total = len(rows)
    yt = [r for r in rows if r.get("platform") == "YouTube"]
    spons = sum(1 for r in yt if r.get("sponsorship_data_available"))
    remaining = len(yt) - spons
    pct = round(100 * spons / len(yt)) if yt else 0
    detail = (f"total {total} · YouTube sponsorship {spons}/{len(yt)} ({pct}%)"
              f" · {remaining} backfill remaining")
    # Backfill remaining is informational, not an alert (the last 10 are manual).
    return _finding(INFO, "Creator pipeline", detail)


# ------------------------------------------------------------------
# 4. Open PRs
# ------------------------------------------------------------------

def check_prs(repo: str | None, headers: dict | None) -> dict:
    if not repo or not headers:
        return _finding(ROUTINE, "Open PRs: not checked", "no GitHub token")
    try:
        prs = requests.get(f"https://api.github.com/repos/{repo}/pulls",
                           headers=headers, params={"state": "open", "per_page": 20},
                           timeout=30).json()
    except Exception as e:
        return _finding(ROUTINE, "Open PRs: check failed", str(e)[:80])
    if not isinstance(prs, list) or not prs:
        return _finding(ROUTINE, "Open PRs: none", "nothing waiting on review")
    lines = []
    for p in prs:
        draft = " [draft]" if p.get("draft") else ""
        age = _age(_parse_iso(p["created_at"]))
        lines.append(f"#{p['number']} {p['title'][:60]}{draft} — opened {age}")
    sev = INFO if len(prs) else ROUTINE
    return _finding(sev, f"Open PRs: {len(prs)} waiting", "\n  ".join(lines))


# ------------------------------------------------------------------
# 5. Blockers (data/processed/blockers.md)
# ------------------------------------------------------------------

def check_blockers() -> list[dict]:
    if not _BLOCKERS.exists():
        return [_finding(ROUTINE, "Blockers: none flagged",
                         f"(create {_BLOCKERS.relative_to(_ROOT)} to track them)")]
    out = []
    for ln in _BLOCKERS.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if s.startswith("- [BLOCKED]"):
            out.append(_finding(ALERT, "BLOCKED", s[11:].strip()))
        elif s.startswith("- [WATCH]"):
            out.append(_finding(ROUTINE, "Watch", s[9:].strip()))
    if not out:
        out.append(_finding(ROUTINE, "Blockers: none active",
                            "blockers.md present, no [BLOCKED]/[WATCH] lines"))
    return out


# ------------------------------------------------------------------
# Render
# ------------------------------------------------------------------

def _render(findings: list[dict]) -> str:
    now = _now().strftime("%Y-%m-%d %H:%M UTC")
    alerts = [f for f in findings if f["severity"] == ALERT]
    infos = [f for f in findings if f["severity"] == INFO]
    routine = [f for f in findings if f["severity"] == ROUTINE]

    lines = [f"# Speed morning brief — {now}", ""]
    if alerts:
        lines.append(f"## {_EMOJI[ALERT]} Needs attention ({len(alerts)})")
    else:
        lines.append("## ✅ Nothing broken or blocked")
    for f in alerts:
        lines.append(f"- **{f['title']}**")
        if f["detail"]:
            lines += [f"  {d}" for d in f["detail"].splitlines()]
    lines.append("")

    lines.append(f"## {_EMOJI[INFO]} Numbers")
    for f in infos:
        lines.append(f"- **{f['title']}** — {f['detail']}" if f["detail"]
                     else f"- **{f['title']}**")
    lines.append("")

    lines.append(f"## {_EMOJI[ROUTINE]} Routine")
    for f in routine:
        lines.append(f"- {f['title']}" + (f" — {f['detail'].splitlines()[0]}"
                                          if f["detail"] else ""))
    lines.append("")
    return "\n".join(lines)


def run() -> str:
    repo = _github_repo()
    token = _gh_token()
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"} if token else None

    findings: list[dict] = []
    findings.append(check_actions(repo, headers))
    findings += check_blockers()
    findings += check_evaluator()
    findings.append(check_creators())
    findings.append(check_prs(repo, headers))

    brief = _render(findings)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(brief, encoding="utf-8")
    print(brief)
    print(f"\n[written to {_OUT.relative_to(_ROOT)}]")
    return brief


if __name__ == "__main__":
    run()
