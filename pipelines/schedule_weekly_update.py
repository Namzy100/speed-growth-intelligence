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
    # Each figure is labeled with its REAL query window. Installs, eCPI and Meta
    # spend come from Adjust/Meta tabs pulled at days=30 (NOT weekly). D1 is the
    # average of the last 7 matured daily cohorts (a genuine recent-week figure).
    # The model is instructed to keep these windows in the copy.
    return (
        f"Total installs (last 30 days): {kpis['total_installs']:,}\n"
        f"Best campaign eCPI (last 30 days): ${kpis['best_ecpi']:.2f}\n"
        f"D1 retention (avg of last 7 matured daily cohorts): {kpis['d1_retention']:.1%}\n"
        f"Meta ad spend (last 30 days): ${kpis['meta_spend']:,.2f}"
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
        "(1) a one-line opener that naturally works in a short, genuine apology for "
        "this update being late because you were traveling. One sentence, keep it light, "
        "do not over-explain or make it its own section;\n"
        "(2) 'What I shipped' — the top 3 most meaningful items from the shipped list, "
        "in plain language (what it does for the team, not the raw commit text);\n"
        "(3) a tight KPI snapshot using the EXACT figures given;\n"
        "(4) 'Content loop' — 3-4 lines using the EXACT numbers from the TREND CONTENT "
        "LOOP data: what's posted, any results-in with their actual numbers, and state "
        "plainly that paid results now pull in AUTOMATICALLY from Meta/Adjust while "
        "organic is still logged manually, plus how many items are sitting in "
        "suggested/briefed that need her decision this week. If nothing is posted yet, "
        "say so honestly (the loop just went live) — do not invent posted items or results;\n"
        "(5) 'Next week' — your actual plan, not a list. Say which of the suggested "
        "content items you intend to move to briefed and by when, and for the key paid "
        "findings give the specific action, but framed as what you'll PROPOSE or flag for "
        "sign-off (e.g. the exact budget shift you'll recommend, and what you'll check "
        "afterward to know it worked). Each line should read as a concrete next step with a "
        "number or a date where you can, not a restated finding.\n"
        "AUTHORITY: you are an intern. You do not execute ad-spend or budget decisions "
        "yourself. Content items you can move through the pipeline (briefing, drafting). But "
        "for anything involving budget, pausing/scaling campaigns, or spend, write it as a "
        "recommendation you'll put up for approval ('I'll propose pausing X', 'I'd recommend "
        "shifting ~$Y, pending sign-off'), never as an action you're taking ('I'm pausing X', "
        "'I'm moving $Y').\n"
        "Throughout, when you raise something, add a few words on what you're going to DO "
        "about it, not just what it is. Under 270 words total: if that runs long, tighten the "
        "shipped and content-loop sections rather than dropping the next-step detail. Open "
        "with 'Hey Niyati,'. Do NOT add a sign-off, your name, or any links; those are "
        "appended automatically."
    ),
    "sumit": (
        "You are Naman, a sharp junior on the growth team, writing your weekly note to "
        "Sumit, a senior leader at the company. Write it as a real email between colleagues, "
        "not a status report.\n\n"
        "WHO SUMIT IS: he understands organic-vs-paid dynamics, CPI and eCPI, retention "
        "math, and funnel mechanics better than you do. Give him the specific numbers and "
        "the one or two calls that need his attention, and trust him to draw the "
        "implications himself. This is the most important rule:\n"
        "- Do NOT explain what a finding means or why it matters when he already knows. "
        "State the number and the call, then stop. Kill textbook-style explanatory clauses. "
        "For example, write 'Apple Search Ads is at $2.40 eCPI vs Google Brand at $4.51, I'd "
        "recommend shifting budget over this week, worth a quick sign-off', NOT 'that gap is "
        "too wide to leave alone' or 'organic "
        "is a structural advantage worth protecting before we scale paid on top of it'. "
        "He knows why a gap matters and what organic means.\n"
        "- Only add a sentence of interpretation when it is a genuinely new or non-obvious "
        "angle he probably has not already considered. When in doubt, cut it.\n\n"
        "FORM: write 3 to 5 short, flowing paragraphs with natural transitions, the way a "
        "person actually writes an email. Do NOT use section headers or all-caps labels "
        "(no 'KEY FINDINGS', 'RECOMMENDATION', 'THE SYSTEM', 'THE ACCURACY SHIFT'). Keep it "
        "scannable by leading each short paragraph with the point. Fold recommendations into "
        "the same sentence as the finding, not into a separate labeled block. At most one "
        "small cluster of two or three hyphen bullets is allowed, and only if it genuinely "
        "reads better than prose for parallel numbers; otherwise use prose.\n\n"
        "WHAT TO COVER, in a natural order:\n"
        "- 'Hey Sumit,' then one short, genuine sentence that this is a bit late because you "
        "were traveling, then the single number that matters most this week.\n"
        "- The two or three findings that actually deserve his attention, each as a plain "
        "statement of the number(s) with your recommended call folded in. For each call, "
        "add the concrete next step, briefly: roughly how much, what you'll check afterward "
        "to know it worked, and by when, all framed as a proposal for sign-off (e.g. 'I'd "
        "recommend shifting ~$X to Apple this week, worth a quick sign-off, and I'll check "
        "blended eCPI by Friday'). Give him the plan, not just the recommendation.\n"
        "AUTHORITY: you are a junior on the team, not a decision-maker on spend. You do NOT "
        "move, pause, or reallocate budget on your own. Every budget or campaign call is "
        "something you RECOMMEND and flag for his (or the paid team's) approval, then action "
        "once signed off. Write 'I'd recommend pausing X', 'worth pausing, can action once "
        "approved', 'flagging this for the paid team to action this week', never 'I'm pausing "
        "X' or 'I'm moving $Y'. Keep the specifics (the dollar figure, the check-in point, "
        "what you'll look at and when); only fix who is doing the action.\n"
        "- Any data-quality gap that blocks a real decision, stated plainly.\n"
        "- Only if there is something real to say: one sentence that paid content is now "
        "measured predicted-vs-actual automatically, so we will see whether the calls were "
        "right rather than just making them. Do not claim results that do not exist yet, and "
        "do not describe how it is built.\n"
        "HARD RULES: under 210 words (if the next-step detail runs long, cut interpretation "
        "and connective phrases, never the numbers or the next steps). Use the EXACT figures "
        "from the data, each with its real time window. No 'what I built' "
        "or activity recap. Do NOT name any software, vendor, or tool (no GitHub, Supabase, "
        "Apify, Claude, or dashboards by name). Do NOT add a sign-off, your name, or links; "
        "those are appended automatically."
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
    "specific, and conversational, the way a sharp teammate actually writes.\n\n"
    "ACCURACY (do not get this wrong): every KPI you are given is labeled with its real "
    "time window, e.g. '(last 30 days)' or '(avg of last 7 matured daily cohorts)'. Always "
    "describe each number with its actual window. NEVER call a 30-day figure 'this week' or "
    "imply it is weekly. If you cite installs, eCPI, or spend, say 'over the last 30 days' "
    "(or similar); only D1 retention is a recent-week figure. If you have no true weekly "
    "number for something, use the real window rather than inventing a weekly frame.\n\n"
    "DO NOT INCLUDE: do not surface Germany (or its organic install count) as a new demand "
    "signal, EU entry signal, or market-entry item. This has already been discussed and is "
    "not fresh each week, so raising it again reads as forgetful or credit-taking. Leave "
    "Germany and EU-market-entry framing out of the email entirely, even if it appears in "
    "the brief or trend data below."
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
    return f"{body.rstrip()}\n\nDashboards:\n{_DASHBOARDS}\n\nRegards,\nNaman\n"


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


def run(dry_run: bool = False, only: str | None = None, to: str | None = None) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise EnvironmentError("GOOGLE_SHEETS_ID must be set in .env")

    refresh_brief()
    print("Building shared context (KPIs, commits, brief)...")
    ctx = _shared_context(spreadsheet_id)

    for key, email, compose_fn in _PERSONAS:
        if only and key != only:
            continue
        # --to overrides the real recipient (for test sends); the persona/subject
        # are unchanged so the test copy reads exactly as the real one will.
        recipient = to or email
        print(f"\nComposing {key} version (would go to {email}"
              + (f", sending TEST to {recipient}" if to else "") + ")...")
        subject, body = compose_fn(ctx)
        if dry_run:
            print(f"--- DRY RUN (not sent) ---\nTo: {recipient}\nSubject: {subject}\n")
            print(body)
        else:
            if to:  # test send: show exactly what went out
                print(f"--- TEST SEND -> {recipient} (real recipient would be {email}) ---")
                print(f"Subject: {subject}\n{body}")
            send(recipient, subject, body)
            print(f"Sent to {recipient}"
                  + (f" (TEST override; real recipient is {email})" if to else "") + ".")


if __name__ == "__main__":
    only, to = None, None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1].strip().lower()
        elif arg.startswith("--to="):
            to = arg.split("=", 1)[1].strip()
    try:
        run(dry_run="--dry-run" in sys.argv, only=only, to=to)
    except (EnvironmentError, FileNotFoundError) as e:
        print(f"Config error: {e}")
        sys.exit(1)
