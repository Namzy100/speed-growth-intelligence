"""Agent evaluator — self-checks Speed's pipeline outputs before they reach the team.

Runs a battery of automated checks (freshness, dashboard integrity, KPI sanity,
creator-DB health, trend-dashboard language, code syntax, output completeness),
scores the CONTENT quality of the latest weekly brief + trend pipeline via Claude
(claude-haiku-4-5), tracks scores over time (feedback loop), prints a report, and
saves it to docs/evaluation_report_YYYY_MM_DD.txt.

Wired into run_sync.sh as the final step; emails an alert if the overall score
drops below 7 (unless --no-alert).

Run from repo root:  python intelligence/agent_evaluator.py [--no-alert]
"""

import ast
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

_DOCS = _ROOT / "docs"
_STATE = _ROOT / "data" / "processed" / "evaluator_state.json"
_HAIKU = "claude-haiku-4-5"
_ALERT_THRESHOLD = 7.0

_DASHBOARDS = {
    "creative": _DOCS / "creative_dashboard.html",
    "creator": _DOCS / "creator_dashboard.html",
    "trend": _DOCS / "trend_dashboard.html",
}


def _status(score: float) -> str:
    return "PASS" if score >= 8 else "WARN" if score >= 5 else "FAIL"


def _data_block(html: str) -> dict | None:
    m = re.search(r"const DATA\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return "BROKEN"  # sentinel: block present but unparseable


# ------------------------------------------------------------------
# A. Automated checks — each returns (score 0-10, detail str)
# ------------------------------------------------------------------

def check_freshness() -> tuple[float, str]:
    try:
        from pipelines import sheets
        ss = sheets._open(os.getenv("GOOGLE_SHEETS_ID"))
        vals = sheets._retry(lambda: ss.worksheet("Last Updated")).get_all_values()
        stamp = next((c for row in vals for c in row if re.search(r"\d{4}-\d{2}-\d{2}", str(c))), None)
        if not stamp:
            return 4.0, "Last Updated cell not found."
        ts = datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        hrs = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if hrs > 25:
            return 3.0, f"STALE — last sync {hrs:.1f}h ago ({stamp})."
        return 10.0, f"Fresh — last sync {hrs:.1f}h ago."
    except Exception as e:
        return 4.0, f"Could not read freshness: {e}"


def _score_dashboard(name: str, path: Path) -> tuple[float, str]:
    if not path.exists():
        return 0.0, f"{name}: file missing"
    html = path.read_text(encoding="utf-8")
    score, notes = 10.0, []
    leftover = len(re.findall(r"/\*__[A-Z_]+__\*/", html))
    if leftover:
        score -= 4
        notes.append(f"{leftover} leftover placeholder(s)")
    if name in ("creative", "creator"):
        d = _data_block(html)
        if d is None:
            score -= 4; notes.append("no DATA block")
        elif d == "BROKEN":
            score -= 6; notes.append("DATA block is broken JSON")
    else:  # trend dashboard: server-rendered sections
        need = ["Top Hooks", "Organic Content Calendar", "Paid Ad Creative Briefs",
                "What's Dying", "Platform Signal"]
        missing = [s for s in need if s not in html]
        if missing:
            score -= 2 * len(missing); notes.append(f"missing sections: {', '.join(missing)}")
    score = max(0.0, score)
    return score, f"{name}: {score:.0f}/10" + (f" ({'; '.join(notes)})" if notes else " (clean)")


def check_dashboards() -> tuple[float, str]:
    parts, scores = [], []
    for name, path in _DASHBOARDS.items():
        s, detail = _score_dashboard(name, path)
        scores.append(s); parts.append(detail)
    return (sum(scores) / len(scores)), " | ".join(parts)


def check_kpi_sanity(prev: dict) -> tuple[float, str, int]:
    html = _DASHBOARDS["creative"].read_text(encoding="utf-8") if _DASHBOARDS["creative"].exists() else ""
    d = _data_block(html)
    cur = None
    if isinstance(d, dict):
        cur = int(d.get("total_installs", 0) or 0)
    if not cur:
        return 4.0, "Could not read total_installs from dashboard.", 0
    prev_installs = (prev or {}).get("total_installs")
    if not prev_installs:
        return 10.0, f"Baseline — total_installs={cur:,} (no prior run to compare).", cur
    drop = (prev_installs - cur) / prev_installs
    if drop > 0.05:
        return 3.0, f"ANOMALY — installs fell {drop:.1%} ({prev_installs:,} → {cur:,}); likely data-pull error.", cur
    return 10.0, f"OK — total_installs={cur:,} ({-drop:+.1%} vs {prev_installs:,}).", cur


def check_creator_db() -> tuple[float, str, int]:
    try:
        from creators import database
        from creators.youtube_batch import EXCLUDED_BRANDS
        rows = database.get_all_creators()
    except Exception as e:
        return 4.0, f"Could not query Supabase: {e}", 0
    brands = [b.lower() for b in EXCLUDED_BRANDS]
    zero_score = sum(1 for r in rows if float(r.get("composite_score", 0) or 0) == 0)
    zero_foll = sum(1 for r in rows if (r.get("followers", 0) or 0) == 0
                    and "mimanshi_list" not in (r.get("niche_tags") or []))
    # A creator legitimately gets ONE ROW PER PLATFORM — save_creator dedups on
    # (name, platform), so e.g. MMCrypto on X (1.78M) and MMCrypto on YouTube
    # (620k) are two real cross-platform presences, NOT a duplicate. Count a true
    # duplicate only when (name, platform) repeats, matching the DB's real key;
    # keying on name alone wrongly flagged legitimate multi-platform creators.
    keys = [(str(r.get("name", "")).strip().lower(), r.get("platform")) for r in rows]
    dupes = sum(c - 1 for c in __import__("collections").Counter(keys).values() if c > 1)
    brand_hits = sum(1 for r in rows
                     if any(b in str(r.get("name", "")).lower() for b in brands))
    total = zero_score + zero_foll + dupes + brand_hits
    detail = (f"{total} issue(s): {zero_score} zero-score, {zero_foll} zero-followers(non-Mimanshi), "
              f"{dupes} duplicate (name+platform), {brand_hits} brand-name hits (of {len(rows)} creators).")
    score = 10.0 if total == 0 else 8.0 if total <= 3 else 6.0 if total <= 15 else 3.0
    return score, detail, total


def check_trend_language() -> tuple[float, str, int]:
    path = _DASHBOARDS["trend"]
    if not path.exists():
        return 0.0, "trend dashboard missing", 0
    from intelligence.trend_pipeline import _is_english
    html = path.read_text(encoding="utf-8")
    texts = re.findall(r'class="hook-text"[^>]*>(.*?)</a>', html, re.S)
    texts += re.findall(r'class="cal-hook">(.*?)</div>', html, re.S)
    import html as _h
    texts = [_h.unescape(re.sub(r"<[^>]+>", "", t)).strip().strip("“”\"") for t in texts]
    non_en = [t for t in texts if t and not _is_english(t)]
    if not texts:
        return 6.0, "No hook cards found to check.", 0
    score = 10.0 if not non_en else max(0.0, 10 - 2 * len(non_en))
    detail = (f"{len(texts)} cards checked, {len(non_en)} non-English."
              + ("" if not non_en else " → " + " | ".join(t[:40] for t in non_en[:3])))
    return score, detail, len(non_en)


def check_code_syntax() -> tuple[float, str, list]:
    errors = []
    for folder in ("intelligence", "pipelines"):
        for p in sorted((_ROOT / folder).glob("*.py")):
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                errors.append(f"{folder}/{p.name}: line {e.lineno}: {e.msg}")
    score = 10.0 if not errors else 0.0
    detail = "all .py files parse cleanly." if not errors else f"{len(errors)} syntax error(s): " + " | ".join(errors[:3])
    return score, detail, errors


def check_output_completeness() -> tuple[float, str, list]:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=7)
    expected = {
        "trend_pipeline": list(_DOCS.glob("trend_pipeline_*.txt")),
        "campaign_analysis": list(_DOCS.glob("campaign_analysis_*.txt")),
        "weekly_brief": list((_DOCS / "weekly_briefs").glob("brief_*.txt")),
        "eu_gtm_plan": list(_DOCS.glob("eu_gtm_plan_*.txt")),
    }
    missing = []
    for label, files in expected.items():
        recent = False
        for f in files:
            m = re.search(r"(\d{4})_(\d{2})_(\d{2})", f.name)
            if m:
                d = datetime(int(m[1]), int(m[2]), int(m[3])).date()
                if d >= cutoff:
                    recent = True; break
        if not recent:
            missing.append(label)
    score = 10.0 * (len(expected) - len(missing)) / len(expected)
    detail = "all weekly outputs present." if not missing else f"missing/stale: {', '.join(missing)}"
    return score, detail, missing


# ------------------------------------------------------------------
# B. Content quality via Claude (haiku)
# ------------------------------------------------------------------

def _latest(glob_dir: Path, pattern: str) -> str:
    files = sorted(glob_dir.glob(pattern))
    return files[-1].read_text(encoding="utf-8") if files else ""


def _score_content(kind: str, text: str, dims: list[str]) -> dict:
    if not text or not os.getenv("ANTHROPIC_API_KEY"):
        return {d: 0 for d in dims} | {"reason": "no content / no API key"}
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (
        f"Score this {kind} for Speed Wallet on each dimension 1-10, then give ONE short "
        f"reason line. Dimensions: {', '.join(dims)}.\n"
        'Return ONLY JSON: {' + ", ".join(f'"{d}":0' for d in dims) + ', "reason":""}.\n\n'
        + text[:6000]
    )
    try:
        resp = client.messages.create(model=_HAIKU, max_tokens=300,
                                      messages=[{"role": "user", "content": prompt}])
        t = resp.content[0].text.strip()
        if t.startswith("```"):
            t = t.strip("`"); t = t[t.find("{"):]
        return json.loads(t[t.find("{"): t.rfind("}") + 1])
    except Exception as e:
        return {d: 0 for d in dims} | {"reason": f"scoring failed: {e}"}


# ------------------------------------------------------------------
# Report + feedback loop
# ------------------------------------------------------------------

def _load_state() -> dict:
    if _STATE.exists():
        try:
            return json.loads(_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _avg(d: dict, dims: list[str]) -> float:
    vals = [float(d.get(x, 0) or 0) for x in dims]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def run(alert: bool = True) -> float:
    prev = _load_state()
    prev_scores = prev.get("scores", {})

    fresh = check_freshness()
    dash = check_dashboards()
    kpi_s, kpi_d, cur_installs = check_kpi_sanity(prev)
    cdb_s, cdb_d, cdb_issues = check_creator_db()
    lang_s, lang_d, lang_bad = check_trend_language()
    syn_s, syn_d, syn_errs = check_code_syntax()
    out_s, out_d, out_missing = check_output_completeness()

    brief_dims = ["specificity", "actionability", "accuracy"]
    trend_dims = ["relevance", "usability", "english_quality"]
    brief_q = _score_content("weekly marketing brief", _latest(_DOCS / "weekly_briefs", "brief_*.txt"), brief_dims)
    trend_q = _score_content("trend/creative pipeline output", _latest(_DOCS, "trend_pipeline_*.txt"), trend_dims)
    brief_avg, trend_avg = _avg(brief_q, brief_dims), _avg(trend_q, trend_dims)

    checks = [
        ("Data freshness", fresh[0], fresh[1]),
        ("Dashboard integrity", dash[0], dash[1]),
        ("KPI sanity", kpi_s, kpi_d),
        ("Creator DB health", cdb_s, cdb_d),
        ("Language filter", lang_s, lang_d),
        ("Code syntax", syn_s, syn_d),
        ("Output completeness", out_s, out_d),
    ]
    scores = {name: round(s, 1) for name, s, _ in checks}
    scores["Brief quality"] = brief_avg
    scores["Trend quality"] = trend_avg
    overall = round(sum(scores.values()) / len(scores), 1)

    # Feedback loop: flag drops > 2 points vs last run.
    drops = []
    for name, s in scores.items():
        old = prev_scores.get(name)
        if old is not None and (old - s) > 2:
            drops.append(f"{name}: {old} → {s} (down {old - s:.1f})")
    prev_overall = prev.get("overall")
    if prev_overall is None:
        trend_arrow = "baseline"
    elif overall > prev_overall + 0.2:
        trend_arrow = "up"
    elif overall < prev_overall - 0.2:
        trend_arrow = "down"
    else:
        trend_arrow = "stable"

    # Collect concrete issues.
    issues = []
    if fresh[0] < 8: issues.append(f"Freshness: {fresh[1]}")
    if dash[0] < 8: issues.append(f"Dashboards: {dash[1]}")
    if kpi_s < 8: issues.append(f"KPI: {kpi_d}")
    if cdb_issues: issues.append(f"Creator DB: {cdb_d}")
    if lang_bad: issues.append(f"Language: {lang_d}")
    if syn_errs: issues.append(f"Syntax: {syn_d}")
    if out_missing: issues.append(f"Outputs: {out_d}")
    for d in drops: issues.append(f"SCORE DROP → {d}")

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"SPEED INTELLIGENCE — EVALUATION REPORT  {date}",
        "=" * 56,
        f"Overall Score: {overall}/10   ("
        + (f"{trend_arrow} vs last run {prev_overall}" if prev_overall is not None else "baseline run") + ")",
        "",
    ]
    for name, s, detail in checks:
        lines.append(f"[{_status(s):<4}] {name}: {detail}")
    lines += [
        "",
        "CONTENT QUALITY",
        f"Brief score: {brief_avg}/10 — {brief_q.get('reason', '')}",
        f"Trend score: {trend_avg}/10 — {trend_q.get('reason', '')}",
        "",
        "ISSUES TO FIX:",
    ]
    lines += [f"  - {i}" for i in issues] if issues else ["  None — all systems healthy"]
    lines += ["", f"SCORE TREND: {trend_arrow}"
              + (f"  ({prev_overall} → {overall})" if prev_overall is not None else "  (baseline run)")]
    report = "\n".join(lines)

    print(report)
    out_path = _DOCS / f"evaluation_report_{datetime.now(timezone.utc):%Y_%m_%d}.txt"
    out_path.write_text(report + "\n", encoding="utf-8")

    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": overall, "scores": scores,
        "total_installs": cur_installs or prev.get("total_installs"),
        "issues": issues,
    }, indent=2), encoding="utf-8")

    if overall < _ALERT_THRESHOLD and alert:
        _send_alert(overall, report)
    elif overall < _ALERT_THRESHOLD:
        print(f"\n[alert suppressed (--no-alert); overall {overall} < {_ALERT_THRESHOLD}]")

    return overall


def _send_alert(overall: float, report: str) -> None:
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_EMAIL", "namanbehl1@gmail.com")
    if not user or not pw:
        print("[alert not sent — GMAIL_USER/GMAIL_APP_PASSWORD not set]")
        return
    try:
        msg = EmailMessage()
        msg["From"], msg["To"] = user, to
        msg["Subject"] = f"⚠ Speed eval score {overall}/10 — below {_ALERT_THRESHOLD}"
        msg.set_content("Automated evaluator alert.\n\n" + report)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"[alert emailed to {to}]")
    except Exception as e:
        print(f"[alert send failed: {e}]")


if __name__ == "__main__":
    run(alert="--no-alert" not in sys.argv)
