"""Build the weekly Sumit update as an HTML dashboard (docs/weekly_update.html).

This is a SEPARATE deliverable from pipelines/schedule_weekly_update.py (the plain-
text email). It does NOT touch that path. It reuses the VISUAL design system of the
fortnightly leadership deck (docs/leadership_deck.html) — colour palette, .kpi cards,
.eyebrow section headers with date-range subtitles, .box cards, .ws status table —
but the CONTENT STRUCTURE is Sumit's weekly template, one-to-one:

  1. TL;DR (compact callout, text)
  2. Shipped This Week vs. Plan (status table with colored badges)
  3. Ahead of Plan / Pulled Forward (cards)
  4. Key Numbers (.kpi row — the one genuinely metric section)
  5. Insights & Findings (highlighted prose block)
  6. Blockers & Asks (spotlight/warning styling, owner + deadline)
  7. Next Week Preview (plain list)
  8. Decisions Needed From You / Niyati (plain list, or "None")

Metrics are pulled LIVE (creator DB, Adjust, Meta, dashboard_state.json). Where a
number does not exist yet (e.g. install-to-deposit), the section SAYS SO rather than
inventing one. The editorial rows (shipped deliverables, findings, blockers, asks)
are curated here — they reflect real, verifiable work — and are meant to be edited
each week.

Run:  python pipelines/build_weekly_update.py
"""

import json
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from html import escape
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

_OUT = _ROOT / "docs" / "weekly_update.html"
_STATE = _ROOT / "docs" / "dashboard_state.json"


# ------------------------------------------------------------------
# Live data
# ------------------------------------------------------------------
def _content_status() -> dict:
    if not _STATE.exists():
        return {}
    st = json.loads(_STATE.read_text(encoding="utf-8"))
    items = list((st.get("items") or {}).values())
    sc = Counter(v.get("status") for v in items)
    return {
        "total": len(items),
        "paid": sum(1 for v in items if v.get("type") == "paid"),
        "organic": sum(1 for v in items if v.get("type") == "organic"),
        "posted": sc.get("posted", 0) + sc.get("results_in", 0),
        "suggested": sc.get("suggested", 0),
        "briefed": sc.get("briefed", 0),
        "updated_at": st.get("updated_at"),
    }


def _creators() -> dict:
    try:
        from creators import database
        rows = database.get_all_creators()
        segs = Counter(r.get("segment_tag") for r in rows)
        return {"total": len(rows), "segments": dict(segs)}
    except Exception as e:
        return {"total": None, "error": str(e)[:120]}


def _synced() -> dict:
    out = {"channels": None, "meta_campaigns": None, "meta_ads": None,
           "apple_ecpi": None, "google_ecpi": None, "organic_share": None,
           "total_installs": None}
    try:
        from pipelines.adjust import AdjustPipeline
        camp = AdjustPipeline().get_installs_by_campaign(days=30)
        out["channels"] = int(camp["channel"].nunique())
        g = camp.groupby("channel").agg(installs=("installs", "sum"), cost=("cost", "sum"))
        tot = float(camp["installs"].sum())
        out["total_installs"] = int(tot)
        if "Organic" in g.index and tot:
            out["organic_share"] = g.loc["Organic", "installs"] / tot
        for ch, key in (("Apple", "apple_ecpi"), ("Google Ads", "google_ecpi")):
            if ch in g.index and g.loc[ch, "installs"]:
                out[key] = g.loc[ch, "cost"] / g.loc[ch, "installs"]
    except Exception as e:
        out["adjust_error"] = str(e)[:120]
    try:
        from pipelines.meta import MetaPipeline
        meta = MetaPipeline().get_creative_performance(days=30)
        out["meta_campaigns"] = int(meta["campaign_name"].nunique())
        out["meta_ads"] = int(meta["ad_name"].nunique())
    except Exception as e:
        out["meta_error"] = str(e)[:120]
    return out


def _week_range() -> tuple[date, date]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


# ------------------------------------------------------------------
# Editorial content (real, verifiable work — edit weekly)
# ------------------------------------------------------------------
# Shipped vs Plan. NOTE: no project-plan file exists in the repo, so the
# "Planned Deliverable" text below reflects THIS WEEK'S ACTUAL WORK (verifiable in
# git history). Swap in the verbatim wording from your real project plan when you
# have it. status: shipped | progress | slipped
_SHIPPED = [
    ("Stateful trend dashboard (predict → ship → measure)", "shipped",
     "Status tracking, results logging, week-over-week versioning. Live."),
    ("Auto-import paid results from Meta + Adjust", "shipped",
     "Paid cards fill from real ad-platform data; no manual entry."),
    ("Weekly update pipeline — accuracy + persona rework", "shipped",
     "Fixed KPI time-window mislabel; separate Niyati / Sumit voices."),
    ("Fix scheduled automation (cron Full Disk Access)", "shipped",
     "Daily sync + weekly + trend now fire on schedule; cron invokes Python directly."),
    ("Strategy dashboard — daily rebuild", "shipped",
     "Rebuilds every day from latest source docs via Claude."),
    ("Map the 3 paid briefs to live Meta campaigns (ad_ref)", "progress",
     "Blocked: briefs unconfirmed with team; none posted yet. See Blockers."),
]

_AHEAD = [
    ("Weekly update as an HTML dashboard", "Not on this week's plan — built the reviewable "
     "dashboard version of this update ahead of schedule for evaluation."),
    ("Instagram Reels ingestion", "Added profile-based Reels scraping (no login) so trend "
     "monitoring does not go dark when API access is flaky."),
]

_NEXT_WEEK = [
    "Move any team-confirmed paid briefs to briefed / in_production and set their ad_ref.",
    "Land the first posted paid campaign so the predicted-vs-actual card populates with real numbers.",
    "Extend paid auto-import coverage beyond the campaigns it maps today.",
    "Scope the organic-attribution gap so organic results stop being manual-only.",
]


def _findings(s: dict) -> list[str]:
    out = []
    if s.get("apple_ecpi") and s.get("google_ecpi"):
        cheaper = (1 - s["apple_ecpi"] / s["google_ecpi"]) * 100
        out.append(
            f"Apple Search Ads is our most efficient paid channel at ${s['apple_ecpi']:.2f} eCPI "
            f"(last 30 days) versus Google at ${s['google_ecpi']:.2f}, roughly {cheaper:.0f}% cheaper. "
            "A measured budget shift toward Apple is worth testing before we scale spend elsewhere."
        )
    if s.get("organic_share") and s.get("total_installs"):
        out.append(
            f"Organic is carrying {s['organic_share']:.0%} of the {s['total_installs']:,} installs "
            "(last 30 days) at zero media cost. Paid should amplify that demand, not replace it. The "
            "gap: organic results are still manual-entry only, so this share is not yet auto-verified."
        )
    if not out:
        out.append("Live channel data was unavailable at build time; findings will populate on the next successful sync.")
    return out


def _blockers(content: dict) -> list[dict]:
    b = [{
        "title": "The 3 paid briefs are not confirmed with the team",
        "body": ("Remittance, beginner-Bitcoin, and iGaming briefs are all still at "
                 f"'suggested' ({content.get('suggested', '?')} items, 0 posted). Until confirmed, "
                 "they can't be briefed, launched, or attributed, which blocks the whole predict-vs-actual loop."),
        "owner": "Sumit / marketing team", "due": "Before the Wed review",
    }, {
        "title": "Install-to-deposit is not tracked yet",
        "body": ("No deposit or revenue event exists in the Adjust/Meta pipelines, so we can only "
                 "measure to the install, not to the deposit. This caps how far the funnel analysis can go."),
        "owner": "Naman + data/eng", "due": "Scope next week",
    }]
    return b


def _decisions() -> list[str]:
    return [
        "Confirm whether the 3 paid briefs (remittance / beginner-Bitcoin / iGaming) are the real plan, or replace them.",
        "Decide whether this HTML update replaces or supplements the plain-text weekly email.",
    ]


def _tldr(content: dict, syn: dict) -> list[str]:
    return [
        "Infrastructure week: scheduled automation is fixed, all dashboards rebuild clean, and the "
        "Claude credit block is cleared. The system is green for the review.",
        f"The content loop is live but empty: {content.get('total', 0)} items tracked, 0 posted yet. "
        "Paid auto-import is wired up and waiting on the first launched campaign.",
        "One decision is blocking progress: the 3 paid briefs need team sign-off before anything can ship.",
    ]


# ------------------------------------------------------------------
# Render
# ------------------------------------------------------------------
_STYLE = """
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1b2230;
    --hairline:rgba(255,255,255,0.09); --hairline-strong:rgba(255,255,255,0.16);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#6e40c9; --accent-2:#a371f7;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149; --gold:#ffd66e;
    --grad:linear-gradient(120deg,#6e40c9,#a371f7);
    --r-lg:18px; --r-md:13px; --r-sm:9px;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  body{
    color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    letter-spacing:-0.01em; -webkit-font-smoothing:antialiased; line-height:1.5;
    background:
      radial-gradient(1200px 700px at 50% -12%, rgba(110,64,201,0.22), transparent 58%),
      radial-gradient(900px 560px at 100% 0%, rgba(163,113,245,0.10), transparent 52%),
      radial-gradient(760px 520px at 0% 10%, rgba(63,185,80,0.05), transparent 50%),
      var(--bg);
    min-height:100vh; padding:6vh 6vw 12vh;
  }
  .wrap{max-width:1080px; margin:0 auto;}

  /* masthead */
  .brand{font-size:14px; font-weight:740; display:flex; align-items:center; gap:9px; color:var(--muted); margin-bottom:26px;}
  .bolt{background:linear-gradient(180deg,#ffd66e,#f0a02a); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 10px rgba(240,160,42,0.45)); font-size:19px;}
  h1{font-size:clamp(30px,4.4vw,50px); font-weight:800; line-height:1.05; letter-spacing:-0.035em;
    background:linear-gradient(180deg,#ffffff,#c5bdec); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .byline{margin-top:14px; font-size:15px; color:var(--faint);}
  .byline b{color:var(--text); font-weight:650;}

  /* section header (the deck's .eyebrow + date-range subtitle) */
  section{margin-top:52px;}
  .eyebrow{font-size:12.5px; text-transform:uppercase; letter-spacing:0.16em; color:var(--accent-2); font-weight:750; display:flex; align-items:center; gap:10px;}
  .eyebrow::before{content:""; width:26px; height:2px; background:var(--grad); border-radius:2px;}
  .sec-sub{font-size:13px; color:var(--faint); margin-top:6px; letter-spacing:0.01em;}
  .sec-title{font-size:clamp(21px,2.4vw,28px); font-weight:770; letter-spacing:-0.025em; margin-top:12px;}

  /* TL;DR callout */
  .tldr{margin-top:18px; background:linear-gradient(180deg,rgba(110,64,201,0.14),rgba(22,27,34,0.4));
    border:1px solid var(--hairline-strong); border-left:3px solid var(--accent-2); border-radius:var(--r-md); padding:22px 26px;}
  .tldr ul{list-style:none; display:flex; flex-direction:column; gap:12px;}
  .tldr li{display:flex; gap:12px; font-size:15.5px; color:var(--text);}
  .tldr li::before{content:"▸"; color:var(--accent-2); font-weight:800; flex:0 0 auto;}

  /* status table */
  .ws{margin-top:18px; border:1px solid var(--hairline); border-radius:var(--r-md); overflow:hidden;}
  .ws table{width:100%; border-collapse:collapse; font-size:15px;}
  .ws th,.ws td{padding:15px 18px; border-bottom:1px solid var(--hairline); text-align:left; vertical-align:top;}
  .ws th{background:#10151d; color:var(--faint); font-size:11.5px; text-transform:uppercase; letter-spacing:0.07em; font-weight:750;}
  .ws td:first-child{font-weight:680; max-width:34ch;}
  .ws tbody tr:last-child td{border-bottom:none;}
  .ws .note{color:var(--muted); font-size:14px;}
  .badge{display:inline-flex; align-items:center; gap:7px; font-size:12px; font-weight:800; letter-spacing:0.03em;
    padding:5px 11px; border-radius:999px; white-space:nowrap; text-transform:uppercase;}
  .badge::before{content:""; width:8px; height:8px; border-radius:50%;}
  .badge.shipped{color:var(--good); background:rgba(63,185,80,0.12); border:1px solid rgba(63,185,80,0.35);}
  .badge.shipped::before{background:var(--good);}
  .badge.progress{color:var(--gold); background:rgba(255,214,110,0.10); border:1px solid rgba(255,214,110,0.32);}
  .badge.progress::before{background:var(--gold);}
  .badge.slipped{color:var(--bad); background:rgba(248,81,73,0.12); border:1px solid rgba(248,81,73,0.35);}
  .badge.slipped::before{background:var(--bad);}
  .plan-note{margin-top:12px; font-size:12.5px; color:var(--faint); font-style:italic;}

  /* cards (ahead of plan) */
  .grid-2{display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:18px;}
  @media(max-width:820px){.grid-2{grid-template-columns:1fr;}}
  .box{background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.5)); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:22px 24px; position:relative; overflow:hidden;}
  .box::before{content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--grad);}
  .box .b-title{font-size:17px; font-weight:730; letter-spacing:-0.02em; margin-bottom:7px;}
  .box .b-sub{font-size:14px; color:var(--muted); line-height:1.5;}

  /* KPI row */
  .kpi-row{display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-top:20px;}
  @media(max-width:820px){.kpi-row{grid-template-columns:1fr 1fr;}}
  .kpi{background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.5)); border:1px solid var(--hairline);
    border-radius:var(--r-md); padding:22px 18px; text-align:center;}
  .kpi .v{font-size:clamp(26px,3.4vw,40px); font-weight:800; letter-spacing:-0.03em; font-variant-numeric:tabular-nums;
    background:linear-gradient(180deg,#fff,#cfd6e4); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .kpi.muted .v{background:none; -webkit-text-fill-color:initial; color:var(--faint); font-size:clamp(15px,1.7vw,18px); font-weight:700;}
  .kpi .l{font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--faint); margin-top:10px; font-weight:700;}
  .kpi .sub{font-size:11.5px; color:var(--muted); margin-top:6px;}

  /* findings prose block */
  .findings{margin-top:18px; background:linear-gradient(180deg,rgba(63,185,80,0.06),rgba(22,27,34,0.4));
    border:1px solid var(--hairline); border-left:3px solid var(--good); border-radius:var(--r-md); padding:22px 26px;}
  .findings p{font-size:15.5px; color:var(--text); line-height:1.6;}
  .findings p + p{margin-top:16px;}

  /* blockers — spotlight */
  .spotlight{margin-top:18px; background:linear-gradient(180deg,rgba(248,81,73,0.10),rgba(22,27,34,0.4));
    border:1px solid rgba(248,81,73,0.4); border-radius:var(--r-md); padding:8px 4px; box-shadow:0 0 0 1px rgba(248,81,73,0.12), 0 18px 50px -22px rgba(248,81,73,0.35);}
  .blocker{padding:16px 22px; border-bottom:1px solid rgba(248,81,73,0.18);}
  .blocker:last-child{border-bottom:none;}
  .blocker .bt{font-size:16px; font-weight:760; color:#ffb3ae; display:flex; align-items:center; gap:9px;}
  .blocker .bt::before{content:"⚠"; color:var(--bad);}
  .blocker .bb{font-size:14px; color:var(--muted); margin:7px 0 10px; line-height:1.5;}
  .blocker .meta{display:flex; gap:10px; flex-wrap:wrap;}
  .tag{font-size:11.5px; font-weight:700; padding:4px 10px; border-radius:var(--r-sm); border:1px solid var(--hairline-strong); color:var(--text);}
  .tag b{color:var(--faint); font-weight:700; text-transform:uppercase; letter-spacing:0.05em; font-size:10.5px; margin-right:5px;}

  /* plain lists */
  .plain{list-style:none; margin-top:18px; display:flex; flex-direction:column; gap:12px;}
  .plain li{display:flex; gap:12px; font-size:15.5px; color:var(--text); padding-left:2px;}
  .plain li::before{content:"—"; color:var(--accent-2); font-weight:800; flex:0 0 auto;}
  .none{margin-top:18px; font-size:15.5px; color:var(--good); font-weight:700; display:inline-flex; align-items:center; gap:9px;
    background:rgba(63,185,80,0.1); border:1px solid rgba(63,185,80,0.3); padding:12px 20px; border-radius:12px;}

  .foot{margin-top:60px; padding-top:20px; border-top:1px solid var(--hairline); font-size:12.5px; color:var(--faint);}
</style>
"""


def _badge(status: str) -> str:
    label = {"shipped": "Shipped", "progress": "In Progress", "slipped": "Slipped"}[status]
    return f'<span class="badge {status}">{label}</span>'


def _num(v, fallback="n/a"):
    return f"{v:,}" if isinstance(v, int) else fallback


def build() -> Path:
    content = _content_status()
    creators = _creators()
    syn = _synced()
    mon, sun = _week_range()
    rng = f"{mon:%b %-d} – {sun:%b %-d, %Y}"

    # --- KPI cards (the one genuinely-metric section) ---
    seg = creators.get("segments", {})
    seg_sub = " · ".join(f"{v} {k}" for k, v in
                         sorted(seg.items(), key=lambda x: -x[1])[:3]) if seg else ""
    kpis = [
        (_num(creators.get("total")), "Creators scored", "cumulative" + (f" · {seg_sub}" if seg_sub else ""), False),
        (_num(syn.get("channels")), "Channels synced", "Adjust · daily", False),
        (f"{_num(syn.get('meta_campaigns'))} / {_num(syn.get('meta_ads'))}", "Meta campaigns / ads", "synced daily", False),
        ("Not tracked yet", "Install → deposit", "no deposit event in pipeline", True),
    ]
    kpi_html = "".join(
        f'<div class="kpi{" muted" if muted else ""}"><div class="v">{escape(str(v))}</div>'
        f'<div class="l">{escape(l)}</div>{f"<div class=\'sub\'>{escape(sub)}</div>" if sub else ""}</div>'
        for v, l, sub, muted in kpis
    )

    # --- TL;DR ---
    tldr = "".join(f"<li>{escape(x)}</li>" for x in _tldr(content, syn))

    # --- shipped table ---
    rows = "".join(
        f"<tr><td>{escape(d)}</td><td>{_badge(s)}</td><td class='note'>{escape(n)}</td></tr>"
        for d, s, n in _SHIPPED
    )

    # --- ahead cards ---
    ahead = "".join(
        f'<div class="box"><div class="b-title">{escape(t)}</div><div class="b-sub">{escape(b)}</div></div>'
        for t, b in _AHEAD
    )

    # --- findings ---
    findings = "".join(f"<p>{escape(x)}</p>" for x in _findings(syn))

    # --- blockers ---
    blockers = "".join(
        f'<div class="blocker"><div class="bt">{escape(b["title"])}</div>'
        f'<div class="bb">{escape(b["body"])}</div>'
        f'<div class="meta"><span class="tag"><b>Owner</b>{escape(b["owner"])}</span>'
        f'<span class="tag"><b>Due</b>{escape(b["due"])}</span></div></div>'
        for b in _blockers(content)
    )

    # --- next week + decisions ---
    nextw = "".join(f"<li>{escape(x)}</li>" for x in _NEXT_WEEK)
    decisions_list = _decisions()
    decisions = ("".join(f"<li>{escape(x)}</li>" for x in decisions_list)
                 if decisions_list else "")

    body = f"""
<div class="wrap">
  <div class="brand"><span class="bolt">⚡</span>Speed Wallet · Growth Intelligence</div>
  <h1>Weekly Update</h1>
  <div class="byline"><b>Naman Behl</b> → Sumit · Week of {escape(rng)}</div>

  <section>
    <div class="eyebrow">TL;DR</div>
    <div class="tldr"><ul>{tldr}</ul></div>
  </section>

  <section>
    <div class="eyebrow">Shipped this week vs. plan</div>
    <div class="sec-sub">{escape(rng)}</div>
    <div class="ws"><table>
      <thead><tr><th>Planned deliverable</th><th>Status</th><th>Notes / link</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
    <div class="plan-note">Planned-deliverable text currently reflects this week's actual work items (verifiable in git history); no project-plan file exists in the repo to quote verbatim. Swap in the exact plan wording when available.</div>
  </section>

  <section>
    <div class="eyebrow">Ahead of plan / pulled forward</div>
    <div class="grid-2">{ahead}</div>
  </section>

  <section>
    <div class="eyebrow">Key numbers</div>
    <div class="sec-sub">last 30 days · pulled live from Adjust, Meta &amp; the creator pipeline</div>
    <div class="kpi-row">{kpi_html}</div>
  </section>

  <section>
    <div class="eyebrow">Insights &amp; findings</div>
    <div class="findings">{findings}</div>
  </section>

  <section>
    <div class="eyebrow">Blockers &amp; asks</div>
    <div class="spotlight">{blockers}</div>
  </section>

  <section>
    <div class="eyebrow">Next week preview</div>
    <ul class="plain">{nextw}</ul>
  </section>

  <section>
    <div class="eyebrow">Decisions needed from you / Niyati</div>
    {'<ul class="plain">' + decisions + '</ul>' if decisions else '<div class="none">✓ None this week</div>'}
  </section>

  <div class="foot">Generated {date.today():%B %-d, %Y} · content status from dashboard_state.json (updated {escape(str(content.get('updated_at')))}) · metrics live from Adjust / Meta / creator pipeline. Design system reused from the fortnightly leadership deck.</div>
</div>
"""

    html = ("<!doctype html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Speed Wallet — Weekly Update</title>\n"
            + _STYLE + "</head>\n<body>\n" + body + "\n</body>\n</html>\n")
    _OUT.write_text(html, encoding="utf-8")
    return _OUT


def main() -> None:
    out = build()
    print(f"Wrote {out.relative_to(_ROOT)} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
