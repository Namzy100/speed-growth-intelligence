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

import json
import os
import smtplib
import ssl
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from intelligence import weekly_brief  # reuse the sheet readers

_MODEL = "claude-sonnet-4-6"
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

    # D1: average of matured cohorts, last 7. Adjust retention lags ~2 days, so the
    # 2 most-recent cohort days are always immature (under-counted) and excluded —
    # matches weekly_brief._fmt_retention so the email's KPI agrees with the brief.
    ret = data.get("retention", pd.DataFrame())
    d1 = 0.0
    if {"day", "retention_rate_d1"} <= set(ret.columns):
        today = datetime.now(timezone.utc).date()

        def _cd(day):
            try:
                return datetime.strptime(str(day), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None

        dated = [d for d in (_cd(r["day"]) for _, r in ret.iterrows()) if d]
        immature = set(sorted(set(dated), reverse=True)[:2])
        vals = []
        for _, r in ret.sort_values("day", ascending=False).iterrows():
            cohort = _cd(r["day"])
            if cohort is None or cohort in immature or cohort + timedelta(days=1) >= today:
                continue  # immature — excluded
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
# Compose — two differentiated versions
# ------------------------------------------------------------------

def _kpi_lines(kpis: dict) -> str:
    return (
        f"Total installs: {kpis['total_installs']:,}\n"
        f"Best eCPI: ${kpis['best_ecpi']:.2f}\n"
        f"D1 retention: {kpis['d1_retention']:.1%}\n"
        f"Meta spend: ${kpis['meta_spend']:,.2f}"
    )


def _trend_summary() -> dict:
    """Read REAL trend-loop numbers from docs/dashboard_state.json (no placeholders)."""
    path = _ROOT / "docs" / "dashboard_state.json"
    if not path.exists():
        return {}
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    items = list((st.get("items") or {}).values())
    from collections import Counter
    sc = Counter(v.get("status") for v in items)
    def pick(pred):
        return [v for v in items if pred(v)]
    return {
        "total": len(items),
        "status_counts": dict(sc),
        "posted": pick(lambda v: v.get("status") == "posted"),
        "results_in": pick(lambda v: v.get("status") == "results_in"),
        "auto_paid": pick(lambda v: v.get("results_source") == "auto" and v.get("type") == "paid"),
        "awaiting": pick(lambda v: v.get("status") in ("suggested", "briefed")),
    }


def _trend_lines(t: dict) -> str:
    """Compact, tool-neutral rendering of the trend loop for the email data blob."""
    if not t:
        return "(no trend tracker data on file)"
    sc = t.get("status_counts", {})
    L = [f"Tracked content items: {t.get('total', 0)} — suggested {sc.get('suggested', 0)}, "
         f"briefed {sc.get('briefed', 0)}, in_production {sc.get('in_production', 0)}, "
         f"posted {sc.get('posted', 0)}, results_in {sc.get('results_in', 0)}"]
    posted = t.get("posted", [])
    L.append("Posted (awaiting results): "
             + ("; ".join(p["hook"][:60] for p in posted) if posted else "none yet"))
    ri = t.get("results_in", [])
    if ri:
        for r in ri:
            res = r.get("results", {})
            L.append(f"Results in — \"{r['hook'][:50]}\": "
                     f"views={res.get('views')}, cpi={res.get('cpi')}, installs={res.get('installs')}")
    else:
        L.append("Results in (measured actuals): none yet")
    auto = t.get("auto_paid", [])
    if auto:
        L.append("Paid results auto-imported this week: "
                 + "; ".join(f"\"{a['hook'][:38]}\" CPI ${a.get('results', {}).get('cpi')}" for a in auto))
    else:
        L.append("Paid auto-import: LIVE and verified against real ad-platform data "
                 "(no paid brief has reached 'posted' yet, so nothing auto-filled this week)")
    L.append("Organic results: manual entry only — no automated organic tracking exists yet")
    aw = t.get("awaiting", [])
    L.append(f"Awaiting a decision (suggested/briefed): {len(aw)}"
             + (" — " + "; ".join(a["hook"][:44] for a in aw[:5]) if aw else ""))
    return "\n".join(L)


def _shared_context(spreadsheet_id: str) -> dict:
    return {
        "n": week_number(),
        "kpis": current_kpis(spreadsheet_id),
        "built": commits_last_7_days(),
        "brief": latest_brief(),
        "trend": _trend_summary(),
    }


# Persona prompts. Niyati = operational (what was built); Sumit = strategic
# (what it means). Both get identical data, framed differently.
_PERSONA_PROMPTS = {
    "niyati": (
        "You are Naman, a marketing & analytics intern at Speed Wallet, writing your "
        "weekly update to Niyati, your day-to-day manager. Voice: direct, confident, "
        "human — a quick sync from a teammate, NOT a formal status report. Structure:\n"
        "(1) a one-line opener;\n"
        "(2) 'What I shipped' — the top 3 most meaningful items from the shipped list, "
        "in plain language (what it does for the team, not the raw commit text);\n"
        "(3) a tight KPI snapshot using the EXACT figures given;\n"
        "(4) 'Content loop' — 3-4 lines using the EXACT numbers from the TREND CONTENT "
        "LOOP data: what's posted, any results-in with their actual numbers, and state "
        "plainly that paid results now pull in AUTOMATICALLY from Meta/Adjust while "
        "organic is still logged manually, plus how many items are sitting in "
        "suggested/briefed that need her decision this week. If nothing is posted yet, "
        "say so honestly (the loop just went live) — do not invent posted items or results;\n"
        "(5) 'Next week' — 2-3 concrete things you'll do, inferred from the brief.\n"
        "Under 270 words. Open with 'Hey Niyati,'. Do NOT add a sign-off, your name, "
        "or any links — those are appended automatically."
    ),
    "sumit": (
        "You are Naman, writing a weekly growth-intelligence note to Sumit, a SENIOR "
        "LEADER. He wants business outcomes, strategic recommendations, and a high-level "
        "view of what the intelligence is surfacing — NOT activity or what was built. "
        "Write in exactly this order, plain prose with short labels:\n"
        "1. OPENER — one line stating the single most important business signal this week: "
        "the number that matters most.\n"
        "2. KEY FINDINGS — 3-4 findings framed as BUSINESS INSIGHTS (what the data MEANS, "
        "not what was built). Each cites the specific numbers and, where relevant, the "
        "implied action. Example of the style: 'Re-engagement is spending $1,575 at 0.09% "
        "conversion — recommend pausing and reallocating to Payday Broad+ at $3.15 CPI.' "
        "Draw these strictly from the brief's data.\n"
        "3. RECOMMENDATION — one clear, specific, actionable strategic recommendation for "
        "the week.\n"
        "4. THE SYSTEM — 2-3 lines describing the growth-intelligence system as BUSINESS "
        "INFRASTRUCTURE: what it monitors (paid performance, the creator partner pipeline, "
        "market & competitor signals, content trends), that it refreshes automatically "
        "every day/week, and what decisions it informs. Frame it as infrastructure, not "
        "technology.\n"
        "5. THE ACCURACY SHIFT — 2-3 lines: the content engine has moved from a "
        "disposable weekly report to a system that TRACKS ITS OWN ACCURACY — predicted "
        "vs actual performance is now automatically measured for paid content, so we can "
        "see whether what we predicted actually happened. Frame this as the "
        "outcome-over-infrastructure move leadership asked for (measuring results, not "
        "just producing more reports). Describe what it ENABLES, not how it's built. "
        "Do not claim measured results that don't exist in the data.\n"
        "6. EU OPPORTUNITY — if the brief shows meaningful organic install demand in "
        "Germany / UK / Portugal, surface it in 1-2 lines as a market-entry opportunity "
        "with the numbers. Skip entirely if not meaningful.\n\n"
        "HARD RULES: Maximum 260 words. Use the EXACT figures from the data. Do NOT "
        "include any 'what I built' or activity section. Do NOT name ANY software, vendor, "
        "or tool (no GitHub, Supabase, Apify, Claude, dashboards-by-name, etc.). "
        "Open with 'Hey Sumit,'. Do NOT add a sign-off, your name, or links — those are "
        "appended automatically."
    ),
}


# These go out as plain-text email, so markdown would render as literal noise.
_PLAINTEXT_RULE = (
    "\n\nIMPORTANT (FORMAT): write PLAIN TEXT only. No markdown, no asterisks for bold, no "
    "'#' headers, no pipe tables. Use short line breaks, ALL-CAPS or a trailing colon "
    "for section labels, and simple hyphen bullets. It must read cleanly in a plain "
    "email client.\n\n"
    "STYLE (write like a real person, not an AI). These are hard rules:\n"
    "1. Do NOT use em dashes or en dashes (the characters '—' or '–') ANYWHERE in the "
    "output. Whenever you would reach for one, either split it into two separate sentences, "
    "or use a comma, a period, or parentheses. (Plain hyphens '-' for bullet points are fine.)\n"
    "2. Do NOT use 'not just X, but Y', 'not only X but also Y', or any symmetric, balanced, "
    "or mirror-image parallel construction. Make the point plainly in one direction.\n"
    "3. Avoid other stiff AI tells: skip phrases like 'it's worth noting' and 'that said', "
    "skip forced rule-of-three lists, and skip grandiose wrap-up sentences. Keep it direct, "
    "specific, and conversational, the way a sharp teammate actually writes."
)


def _ai_body(persona: str, ctx: dict) -> str:
    """Claude-composed persona narrative (no sign-off/links — appended later)."""
    built = "\n".join(f"- {s}" for s in ctx["built"][:6]) or "- (nothing notable)"
    data = (
        f"Week number: {ctx['n']}\n\n"
        f"KPIs (use these exact figures):\n{_kpi_lines(ctx['kpis'])}\n\n"
        f"Most meaningful things shipped this week:\n{built}\n\n"
        f"TREND CONTENT LOOP (real numbers from the content tracker — use these exactly):\n"
        f"{_trend_lines(ctx.get('trend', {}))}\n\n"
        f"This week's growth brief (findings + recommendation):\n{ctx['brief']}"
    )
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=_MODEL, max_tokens=700,
        messages=[{"role": "user",
                   "content": _PERSONA_PROMPTS[persona] + _PLAINTEXT_RULE + "\n\n--- DATA ---\n" + data}],
    )
    return resp.content[0].text.strip()


def _assemble(body: str) -> str:
    return f"{body.rstrip()}\n\nDashboards:\n{_DASHBOARDS}\n\n— Naman\n"


def compose_niyati(ctx: dict) -> tuple[str, str]:
    return f"Week {ctx['n']} update — Naman", _assemble(_ai_body("niyati", ctx))


def compose_sumit(ctx: dict) -> tuple[str, str]:
    # "week of" = the Monday of the current week, e.g. "Jun 30, 2026".
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    subject = f"Growth intelligence — week of {monday:%b} {monday.day}, {monday.year} | Naman"
    return subject, _assemble(_ai_body("sumit", ctx))


# Recipient → (label, compose fn). One tailored email each.
_PERSONAS = [
    ("niyati", "niyati@tryspeed.com", compose_niyati),
    ("sumit", "sumit@tryspeed.com", compose_sumit),
]


# ------------------------------------------------------------------
# Send
# ------------------------------------------------------------------

def send(to: str, subject: str, body: str) -> None:
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise EnvironmentError(
            "GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env "
            "(use a Gmail App Password, not the account password)."
        )
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.send_message(msg)


def refresh_brief() -> None:
    """Regenerate today's weekly brief so the emails carry current data.

    Calls weekly_brief.run() directly (it exposes one). Best-effort: if it fails,
    the emails fall back to the most recent brief already on disk.
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


def run(dry_run: bool = False, only: str | None = None) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    refresh_brief()
    print("Building shared context (KPIs, commits, brief)...")
    ctx = _shared_context(spreadsheet_id)

    for key, email, compose_fn in _PERSONAS:
        if only and key != only:
            continue
        print(f"\nComposing {key} version ({email})...")
        subject, body = compose_fn(ctx)
        if dry_run:
            print(f"--- DRY RUN (not sent) ---\nTo: {email}\nSubject: {subject}\n")
            print(body)
        else:
            send(email, subject, body)
            print(f"Sent to {email}.")


if __name__ == "__main__":
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1].strip().lower()
    try:
        run(dry_run="--dry-run" in sys.argv, only=only)
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
