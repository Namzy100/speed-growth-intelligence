"""Weekly progress email for the Speed marketing leads.

Runs every Friday at 12:00 (via cron). Auto-composes a plain-text update from:
  - the latest weekly brief in docs/weekly_briefs/
  - current KPIs from the Google Sheet (installs, best eCPI, D1 retention, Meta spend)
  - the last 7 days of git commits (what was built)
  - the creator outreach pipeline summary (Supabase)

...and emails it to the marketing leads via Gmail SMTP.

Secrets (never hardcode — read from .env):
  GMAIL_USER            the sending Gmail address
  GMAIL_APP_PASSWORD    a Gmail App Password (not the account password)

Usage:
  python pipelines/schedule_weekly_update.py            # compose + send
  python pipelines/schedule_weekly_update.py --dry-run  # compose + print, no send
"""

import os
import smtplib
import ssl
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from intelligence import weekly_brief  # reuse the sheet readers

_RECIPIENTS = ["niyati@tryspeed.com", "sumit@tryspeed.com"]
_BRIEFS_DIR = _ROOT / "docs" / "weekly_briefs"

# Internship week 1 began the week of this Monday; used to number the update.
# Override with WEEKLY_UPDATE_START=YYYY-MM-DD in .env if the start date shifts.
_DEFAULT_START = date(2026, 6, 9)

_DASHBOARDS = (
    "Creative:  https://namzy100.github.io/speed-growth-intelligence\n"
    "Creator:   https://namzy100.github.io/speed-growth-intelligence/creators\n"
    "Strategy:  https://namzy100.github.io/speed-growth-intelligence/strategy"
)


# ------------------------------------------------------------------
# Content builders
# ------------------------------------------------------------------

def week_number(today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    raw = os.getenv("WEEKLY_UPDATE_START")
    start = date.fromisoformat(raw) if raw else _DEFAULT_START
    return max(1, (today - start).days // 7 + 1)


def latest_brief() -> str:
    files = sorted(_BRIEFS_DIR.glob("brief_*.txt"))
    if not files:
        return "(no weekly brief found)"
    return files[-1].read_text(encoding="utf-8").strip()


def _num(series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def current_kpis(spreadsheet_id: str) -> dict:
    """Total installs, best eCPI, recent D1 retention, and Meta spend."""
    data = weekly_brief.read_sheets_data(spreadsheet_id)

    co = data.get("channel_overview", pd.DataFrame())
    total_installs = int(_num(co["installs"]).sum()) if "installs" in co else 0

    # Best eCPI is the lowest CAMPAIGN-level cost-per-install (Campaign Installs
    # tab: cost / installs), not the blended channel-level figure — the campaign
    # number (e.g. Apple US Brand Exact) is more accurate and more impressive.
    best_ecpi = 0.0
    ci = data.get("installs_by_campaign", pd.DataFrame())
    if {"installs", "cost"} <= set(ci.columns):
        c = ci.copy()
        c["_inst"] = _num(c["installs"])
        c["_cost"] = _num(c["cost"])
        c = c[(c["_inst"] > 0) & (c["_cost"] > 0)]
        if not c.empty:
            best_ecpi = float((c["_cost"] / c["_inst"]).min())

    meta = data.get("meta_campaigns", pd.DataFrame())
    meta_spend = float(_num(meta["spend"]).sum()) if "spend" in meta else 0.0

    # D1: average of matured cohorts only (cohort_day + 1 < today), last 7.
    ret = data.get("retention", pd.DataFrame())
    d1 = 0.0
    if {"day", "retention_rate_d1"} <= set(ret.columns):
        today = datetime.now(timezone.utc).date()
        vals = []
        for _, r in ret.sort_values("day", ascending=False).iterrows():
            try:
                cohort = datetime.strptime(str(r["day"]), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if cohort + timedelta(days=1) >= today:
                continue  # immature
            v = float(pd.to_numeric(r["retention_rate_d1"], errors="coerce") or 0)
            if v > 0:
                vals.append(v)
            if len(vals) >= 7:
                break
        if vals:
            d1 = sum(vals) / len(vals)

    return {
        "total_installs": total_installs,
        "best_ecpi": best_ecpi,
        "d1_retention": d1,
        "meta_spend": meta_spend,
    }


# Commit-message prefixes that signal routine/noise rather than real work.
_NOISE_PREFIXES = ("chore:", "refresh", "auto-deploy", "nudge", "redeploy", "update")


def commits_last_7_days() -> list[str]:
    """Top 10 meaningful commit subjects from the last 7 days (most recent first).

    Filters out routine/noise commits (chore:, Refresh, auto-deploy, Nudge,
    Redeploy, Update) and caps at 10; shows all if fewer than 10 remain.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--since=7 days ago", "--pretty=format:%s"],
            cwd=_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return []
    seen, subjects = set(), []
    for line in out.splitlines():  # git log is newest-first
        line = line.strip()
        if not line or line.lower().startswith(_NOISE_PREFIXES):
            continue
        if line not in seen:
            seen.add(line)
            subjects.append(line)
    return subjects[:10]


def outreach_summary() -> list[tuple[str, int]]:
    """Counts per outreach stage in funnel order, plus total."""
    from creators import database
    order = ["not_contacted", "contacted", "responded",
             "in_negotiation", "confirmed", "declined"]
    rows = database.get_all_creators()
    counts = {s: 0 for s in order}
    for r in rows:
        s = r.get("outreach_status", "not_contacted")
        counts[s] = counts.get(s, 0) + 1
    result = [(s, counts[s]) for s in order]
    result.append(("total", len(rows)))
    return result


# ------------------------------------------------------------------
# Compose
# ------------------------------------------------------------------

def compose(spreadsheet_id: str) -> tuple[str, str]:
    n = week_number()
    subject = f"Speed Growth Intelligence — Week {n} Update | Naman"

    kpis = current_kpis(spreadsheet_id)
    built = commits_last_7_days()
    outreach = outreach_summary()
    brief = latest_brief()

    built_block = "\n".join(f"  - {s}" for s in built) or "  (no commits in the last 7 days)"
    outreach_block = "\n".join(f"  {stage:<16}{count:>5}" for stage, count in outreach)

    body = f"""Hi Niyati and Sumit,

Here's the Week {n} Speed growth-intelligence update.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KPIs — last 30 days
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Total installs      {kpis['total_installs']:>12,}
  Best eCPI           {'$' + format(kpis['best_ecpi'], '.2f'):>12}
  D1 retention        {format(kpis['d1_retention'], '.1%'):>12}
  Meta spend          {'$' + format(kpis['meta_spend'], ',.2f'):>12}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
What was built this week
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{built_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Creator outreach pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{outreach_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This week's brief
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{brief}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Live dashboards
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_DASHBOARDS}

— Naman
"""
    return subject, body


# ------------------------------------------------------------------
# Send
# ------------------------------------------------------------------

def send(subject: str, body: str) -> None:
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise EnvironmentError(
            "GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env "
            "(use a Gmail App Password, not the account password)."
        )
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(_RECIPIENTS)
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.send_message(msg)


def refresh_brief() -> None:
    """Regenerate today's weekly brief so the email carries current data.

    Calls weekly_brief.run() directly (it exposes one). Best-effort: if it fails,
    the email falls back to the most recent brief already on disk.
    """
    print("Regenerating weekly brief for fresh data...")
    try:
        if hasattr(weekly_brief, "run"):
            weekly_brief.run()
        else:  # fallback if the entrypoint is ever renamed
            subprocess.run(
                [sys.executable, str(_ROOT / "intelligence" / "weekly_brief.py")],
                cwd=_ROOT, check=True, timeout=120,
            )
    except Exception as e:
        print(f"  brief refresh failed ({e}); using the latest brief on disk.")


def run(dry_run: bool = False) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    refresh_brief()

    print("Composing weekly update...")
    subject, body = compose(spreadsheet_id)

    if dry_run:
        print("\n--- DRY RUN (not sent) ---")
        print(f"To: {', '.join(_RECIPIENTS)}")
        print(f"Subject: {subject}\n")
        print(body)
        return

    print(f"Sending to {', '.join(_RECIPIENTS)}...")
    send(subject, body)
    print("Sent.")


if __name__ == "__main__":
    try:
        run(dry_run="--dry-run" in sys.argv)
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
