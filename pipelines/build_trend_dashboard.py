"""Build the Trend Intelligence dashboard (docs/trend_dashboard.html) — US market.

Self-contained dark-theme HTML for Speed's marketing team. Calls
intelligence.trend_pipeline.collect_signals() for fresh US YouTube + TikTok data,
asks Claude to enrich the top hooks and generate an organic content calendar +
paid ad briefs, and bakes it in.

Sections:
  1. This week's top hooks (US only) — 10 highest-ER hook patterns
  2. Organic content calendar — 5 pieces to post this week
  3. Paid ad creative briefs — 3 concepts to brief
  4. What's dying — formats losing traction
  5. Platform signal — TikTok vs YouTube per segment

Run from repo root:  python pipelines/build_trend_dashboard.py
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from intelligence import trend_pipeline

_OUT = _ROOT / "docs" / "trend_dashboard.html"
_CACHE = _ROOT / "data" / "processed" / "trend_raw_cache.json"
_CACHE_MAX_AGE_H = 3  # reuse a very-recent scrape when iterating on rendering/Claude
_MODEL = "claude-sonnet-4-6"
_BENCHMARK_CPI = 3.17
_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]
_SEG_LABEL = {"remittance": "Remittance", "crypto-curious": "Crypto-Curious", "iGaming": "iGaming"}
_CANDIDATES = 24   # pool Claude selects the final relevant top-10 from
_TOP_HOOKS = 10


def _load_cache() -> dict | None:
    """Reuse a very-recent raw scrape (avoids re-hitting Apify/YouTube when only
    the rendering or Claude layer changed). Fresh weekly runs are always >3h apart,
    so the cron never reuses stale data."""
    if "--fresh" in sys.argv or not _CACHE.exists():
        return None
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data.get("generated_at", ""))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if age_h <= _CACHE_MAX_AGE_H:
            print(f"Using cached raw scrape ({age_h:.1f}h old; pass --fresh to refetch).")
            return data
    except Exception:
        return None
    return None


def _save_cache(data: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(data), encoding="utf-8")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_n(n) -> str:
    n = int(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)


# ------------------------------------------------------------------
# Claude: enrich top hooks + organic calendar + paid briefs
# ------------------------------------------------------------------

def generate_ai(data: dict, candidates: list[dict]) -> dict:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    hook_lines = []
    for i, v in enumerate(candidates):
        hook_lines.append(
            f"{i}. [{v['segment']}/{v['track']}] {v['platform']} · {v['views']:,} views · "
            f"{v['er']:.1%} ER · save {v.get('save_rate',0):.1%} · "
            f"{'replicable' if v.get('replicable') else 'produced'} — HOOK: {v['hook']}"
        )
    seg_digest = []
    for seg in _SEGMENTS:
        b = data["by_segment"][seg]
        seg_digest.append(f"{seg}: {len(b['organic'])} organic-track, {len(b['paid'])} paid-track items")

    prompt = (
        "You are Speed Wallet's growth-creative strategist. Speed is a Bitcoin Lightning "
        "payments app. Segments: remittance (zero-fee cross-border sends), crypto-curious "
        "(dead-simple first Bitcoin use), iGaming (instant deposits/withdrawals). Best paid "
        f"CPI benchmark: ${_BENCHMARK_CPI:.2f}.\n\n"
        "Below are candidate US trending hooks (YouTube + TikTok), ranked by engagement. "
        "Keyword matching lets some IRRELEVANT viral novelty through (ASMR, memes, monkey "
        "clips, unrelated 'money' trends). Produce a single JSON object:\n\n"
        "1. hooks: SELECT the up-to-10 candidates a fintech/crypto/remittance/iGaming brand "
        "could actually learn from, and DROP the irrelevant novelty even if its engagement is "
        "high. Return them ranked best-first, each enriched. Fields: index (int, the candidate's "
        "number), format_type (one of: talking-head / text-on-screen / screen-record / reaction "
        "/ animation / ugc), replication_score (int 1-10, how easily Speed could copy it with a "
        "phone + basic edit), production_cost (one of: '$0 (phone video)' / '$50-200 (basic "
        "edit)' / '$200+ (produced)'), why_it_works (one line naming the psychological driver: "
        "fear of loss / social proof / curiosity gap / identity signal / etc).\n"
        "2. organic_calendar: exactly 5 pieces the Speed team should post THIS WEEK (phone-filmable). "
        "Each: hook (literal line), outline (3-4 beats, <=60s), platform (TikTok/Instagram Reels), "
        "segment, est_reach (based on the ER benchmarks shown), production_notes (e.g. 'film on "
        "iPhone, no editing needed').\n"
        "3. paid_briefs: exactly 3 ad concepts. Each: hook (first 3s script), problem (secs 4-8), "
        f"solution (secs 9-15, Speed's angle), cta (final 3s), audience (Meta/TikTok targeting), "
        f"est_cpi (range vs ${_BENCHMARK_CPI:.2f} + one-line why).\n\n"
        'Return ONLY JSON: {"hooks":[{"index":0,"format_type":"","replication_score":0,'
        '"production_cost":"","why_it_works":""}],"organic_calendar":[{"hook":"","outline":"",'
        '"platform":"","segment":"","est_reach":"","production_notes":""}],"paid_briefs":'
        '[{"hook":"","problem":"","solution":"","cta":"","audience":"","est_cpi":""}]}\n\n'
        "--- TOP HOOKS ---\n" + "\n".join(hook_lines) + "\n\n--- TRACK COUNTS ---\n" + "\n".join(seg_digest)
    )
    for attempt in range(2):
        msg = prompt if attempt == 0 else prompt + "\n\nReturn ONLY valid JSON."
        resp = client.messages.create(model=_MODEL, max_tokens=4000,
                                      messages=[{"role": "user", "content": msg}])
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`"); text = text[text.find("{"):]
        try:
            return json.loads(text[text.find("{"): text.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError):
            continue
    return {"hooks": [], "organic_calendar": [], "paid_briefs": []}


# ------------------------------------------------------------------
# Render
# ------------------------------------------------------------------

def _repl_dots(score) -> str:
    try:
        n = max(0, min(10, int(score)))
    except (TypeError, ValueError):
        n = 0
    return f'<span class="repl"><span class="repl-fill" style="width:{n*10}%"></span></span><span class="repl-n">{n}/10</span>'


def render_hooks(candidates: list[dict], enrich: dict) -> str:
    selected = [h for h in enrich.get("hooks", [])
                if isinstance(h.get("index"), int) and 0 <= h["index"] < len(candidates)]
    if not selected:  # fallback: raw top candidates if Claude selection failed
        selected = [{"index": i} for i in range(min(_CANDIDATES, len(candidates)))]
    rows, rank = [], 0
    for e in selected:
        v = candidates[e["index"]]
        # Deterministic English guard — drop anything with foreign-language text
        # that slipped past classification/Claude (e.g. Arabic hashtags).
        if not trend_pipeline._is_english(v["hook"]):
            continue
        rank += 1
        if rank > _TOP_HOOKS:
            break
        plat_cls = "yt" if v["platform"] == "YouTube" else "tt"
        rows.append(f"""
      <div class="hook-card">
        <div class="hook-rank">{rank}</div>
        <div class="hook-main">
          <a class="hook-text" href="{_e(v['url'])}" target="_blank" rel="noopener">“{_e(v['hook'])}”</a>
          <div class="hook-meta">
            <span class="badge {plat_cls}">{_e(v['platform'])}</span>
            <span class="badge seg {v['segment'].replace('-','')}">{_e(_SEG_LABEL.get(v['segment'], v['segment']))}</span>
            <span class="m">{_fmt_n(v['views'])} views</span>
            <span class="m er">{v['er']:.1%} ER</span>
            <span class="m fmt">{_e(e.get('format_type','—'))}</span>
            <span class="m cost">{_e(e.get('production_cost',''))}</span>
          </div>
          <div class="hook-why">{_e(e.get('why_it_works',''))}</div>
        </div>
        <div class="hook-repl"><div class="repl-lab">Replication</div>{_repl_dots(e.get('replication_score'))}</div>
      </div>""")
    return "".join(rows) or '<div class="empty">No qualifying hooks this week.</div>'


def render_calendar(enrich: dict) -> str:
    items = enrich.get("organic_calendar", [])[:5]
    cards = []
    for i, c in enumerate(items, 1):
        cards.append(f"""
      <div class="cal-card">
        <div class="cal-day">Post {i}</div>
        <div class="cal-hook">“{_e(c.get('hook'))}”</div>
        <div class="cal-outline">{_e(c.get('outline'))}</div>
        <div class="cal-foot">
          <span class="badge seg {str(c.get('segment','')).replace('-','')}">{_e(c.get('segment'))}</span>
          <span class="badge plat">{_e(c.get('platform'))}</span>
          <span class="cal-reach">~{_e(c.get('est_reach'))}</span>
        </div>
        <div class="cal-notes">🎬 {_e(c.get('production_notes'))}</div>
      </div>""")
    return "".join(cards) or '<div class="empty">—</div>'


def render_paid(enrich: dict) -> str:
    briefs = enrich.get("paid_briefs", [])[:3]
    cards = []
    for i, b in enumerate(briefs, 1):
        cards.append(f"""
      <div class="paid-card">
        <div class="paid-n">{i}</div>
        <div class="paid-body">
          <div class="paid-row"><span class="k">Hook · 0-3s</span><span class="v">“{_e(b.get('hook'))}”</span></div>
          <div class="paid-row"><span class="k">Problem · 4-8s</span><span class="v">{_e(b.get('problem'))}</span></div>
          <div class="paid-row"><span class="k">Speed · 9-15s</span><span class="v">{_e(b.get('solution'))}</span></div>
          <div class="paid-row"><span class="k">CTA · final 3s</span><span class="v">{_e(b.get('cta'))}</span></div>
          <div class="paid-row"><span class="k">Audience</span><span class="v">{_e(b.get('audience'))}</span></div>
          <div class="paid-row"><span class="k">Est. CPI</span><span class="v cpi">{_e(b.get('est_cpi'))}</span></div>
        </div>
      </div>""")
    return "".join(cards) or '<div class="empty">—</div>'


def render_died(data: dict) -> str:
    died = data.get("died", [])
    if not died:
        return ('<div class="died-empty">No week-over-week decline data yet — baseline week. '
                "Next Monday's run flags formats losing traction.</div>")
    return '<ul class="died-list">' + "".join(f"<li>{_e(d)}</li>" for d in died[:8]) + "</ul>"


def render_signal(data: dict) -> str:
    sig = data.get("platform_signal", {})
    rows = []
    for seg in _SEGMENTS:
        s = sig.get(seg, {})
        yt, tt = s.get("youtube_er", 0), s.get("tiktok_er", 0)
        mx = max(yt, tt, 0.0001)
        winner = "TikTok" if tt > yt else "YouTube" if yt > tt else "Even"
        rows.append(f"""
      <div class="sig-row">
        <div class="sig-seg">{_e(_SEG_LABEL[seg])}</div>
        <div class="sig-bars">
          <div class="sig-bar"><span class="sig-lab">YT</span><div class="sig-track"><div class="sig-fill yt" style="width:{yt/mx*100:.0f}%"></div></div><span class="sig-val">{yt:.1%}</span></div>
          <div class="sig-bar"><span class="sig-lab">TT</span><div class="sig-track"><div class="sig-fill tt" style="width:{tt/mx*100:.0f}%"></div></div><span class="sig-val">{tt:.1%}</span></div>
        </div>
        <div class="sig-win">{winner} stronger</div>
      </div>""")
    return "".join(rows)


def render(data: dict, enrich: dict, candidates: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fs = data.get("filter_stats", {})
    counts = (f"{len(data['youtube'])} YouTube · {len(data['tiktok'])} TikTok (US + English) · "
              f"filtered {fs.get('non_us',0)+fs.get('youtube_filtered',0)} non-US/EN YT, "
              f"{fs.get('tiktok_filtered',0)} non-EN TikTok")
    repl = {
        "/*__SYNC__*/": now, "/*__COUNTS__*/": counts,
        "/*__HOOKS__*/": render_hooks(candidates, enrich),
        "/*__CALENDAR__*/": render_calendar(enrich),
        "/*__PAID__*/": render_paid(enrich),
        "/*__DIED__*/": render_died(data),
        "/*__SIGNAL__*/": render_signal(data),
    }
    out = _TEMPLATE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    data = _load_cache()
    if data is None:
        print("Collecting fresh US trend signals (YouTube + TikTok)...")
        data = trend_pipeline.collect_signals()
        _save_cache(data)
    candidates = data["top_hooks"][:_CANDIDATES]
    print(f"Selecting + enriching top hooks from {len(candidates)} candidates (Claude)...")
    enrich = generate_ai(data, candidates)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render(data, enrich, candidates), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  hooks selected={len(enrich.get('hooks', []))} · "
          f"calendar={len(enrich.get('organic_calendar', []))} · "
          f"paid briefs={len(enrich.get('paid_briefs', []))}")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Trend Intelligence (US)</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1b2230;
    --hairline:rgba(255,255,255,0.09); --hairline-strong:rgba(255,255,255,0.16);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#6e40c9; --accent-2:#a371f7;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149; --gold:#ffd66e;
    --yt:#ff4d4d; --tt:#25f4ee;
    --grad:linear-gradient(120deg,#6e40c9,#a371f7);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.7);
    --r-lg:16px; --r-md:12px; --r-sm:9px;
    --seg-remittance:#3fb950; --seg-cryptocurious:#a371f7; --seg-iGaming:#e3b341;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  body{color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    letter-spacing:-0.005em; line-height:1.5; -webkit-font-smoothing:antialiased; min-height:100vh;
    background:radial-gradient(1100px 600px at 50% -10%, rgba(110,64,201,0.20), transparent 58%),
      radial-gradient(820px 520px at 100% 0%, rgba(163,113,245,0.09), transparent 52%), var(--bg);
    background-attachment:fixed;}
  .wrap{max-width:1180px; margin:0 auto; padding:0 24px 90px;}
  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand{font-weight:760; font-size:16px; display:flex; align-items:center; gap:8px;}
  .brand .bolt{background:linear-gradient(180deg,#ffd66e,#f0a02a); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .sync{font-size:12px; color:var(--muted);} .sync b{color:var(--text);}
  .title-block{margin:34px 0 24px;}
  h1{font-size:30px; font-weight:790; letter-spacing:-0.03em; background:linear-gradient(180deg,#fff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .title-block .sub{color:var(--muted); font-size:13.5px; margin-top:5px;}
  section{margin:40px 0;}
  .sec-head{display:flex; align-items:baseline; gap:11px; margin-bottom:18px; flex-wrap:wrap;}
  h2{font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted); font-weight:700; display:flex; align-items:center; gap:10px;}
  h2::before{content:""; width:3px; height:13px; border-radius:2px; background:var(--grad);}
  .sec-note{font-size:12px; color:var(--faint);}
  .badge{display:inline-flex; align-items:center; font-size:10px; font-weight:800; padding:2px 8px; border-radius:6px; letter-spacing:0.03em;}
  .badge.yt{background:var(--yt); color:#fff;} .badge.tt{background:var(--tt); color:#04201f;}
  .badge.plat{background:var(--panel-2); color:var(--muted);}
  .badge.seg{color:#0d1117;} .badge.seg.remittance{background:var(--seg-remittance);} .badge.seg.cryptocurious{background:var(--seg-cryptocurious);} .badge.seg.iGaming{background:var(--seg-iGaming);}
  .empty{color:var(--faint); font-size:13px; padding:10px 0;}

  /* 1: top hooks */
  .hook-card{display:flex; gap:16px; align-items:flex-start; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px 18px; margin-bottom:11px;}
  .hook-rank{font-size:22px; font-weight:850; color:var(--accent-2); min-width:26px; font-variant-numeric:tabular-nums;}
  .hook-main{flex:1; min-width:0;}
  .hook-text{display:block; font-size:15.5px; font-weight:680; color:var(--text); text-decoration:none; line-height:1.35;}
  .hook-text:hover{color:var(--accent-2);}
  .hook-meta{display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin:9px 0 7px;}
  .hook-meta .m{font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums;}
  .hook-meta .er{color:var(--good); font-weight:700;} .hook-meta .fmt{color:var(--accent-2); font-weight:600;} .hook-meta .cost{color:var(--faint);}
  .hook-why{font-size:12.5px; color:var(--muted); font-style:italic;}
  .hook-repl{flex:0 0 auto; text-align:right; min-width:96px;}
  .repl-lab{font-size:9px; text-transform:uppercase; letter-spacing:0.07em; color:var(--faint); font-weight:700; margin-bottom:5px;}
  .repl{display:inline-block; width:70px; height:7px; background:rgba(255,255,255,0.08); border-radius:5px; overflow:hidden; vertical-align:middle;}
  .repl-fill{display:block; height:100%; background:linear-gradient(90deg,#2c8c3c,var(--good)); border-radius:5px;}
  .repl-n{font-size:11px; font-weight:700; color:var(--muted); margin-left:7px;}

  /* 2: calendar */
  .cal-grid{display:grid; grid-template-columns:repeat(5,1fr); gap:14px;}
  @media(max-width:1000px){.cal-grid{grid-template-columns:1fr 1fr;}}
  @media(max-width:640px){.cal-grid{grid-template-columns:1fr;}}
  .cal-card{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:15px; display:flex; flex-direction:column; gap:9px;}
  .cal-day{font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:var(--accent-2); font-weight:800;}
  .cal-hook{font-size:14px; font-weight:700; line-height:1.35;}
  .cal-outline{font-size:12px; color:var(--muted); line-height:1.5; flex:1;}
  .cal-foot{display:flex; flex-wrap:wrap; gap:6px; align-items:center;}
  .cal-reach{font-size:11px; color:var(--gold); font-weight:700; margin-left:auto;}
  .cal-notes{font-size:11px; color:var(--faint); border-top:1px solid var(--hairline); padding-top:8px;}

  /* 3: paid briefs */
  .paid-grid{display:grid; grid-template-columns:repeat(3,1fr); gap:16px;}
  @media(max-width:900px){.paid-grid{grid-template-columns:1fr;}}
  .paid-card{display:flex; gap:13px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); padding:17px;}
  .paid-n{font-size:24px; font-weight:850; color:var(--accent-2);}
  .paid-row{margin:8px 0;}
  .paid-row .k{display:block; font-size:9px; text-transform:uppercase; letter-spacing:0.06em; color:var(--faint); font-weight:700; margin-bottom:2px;}
  .paid-row .v{font-size:12.5px; color:var(--text); line-height:1.4;} .paid-row .v.cpi{color:var(--good); font-weight:700;}

  /* 4: dying */
  .died-list{list-style:none; display:flex; flex-direction:column; gap:9px;}
  .died-list li{background:rgba(248,81,73,0.07); border:1px solid rgba(248,81,73,0.25); border-radius:var(--r-sm); padding:11px 15px; font-size:13px; color:var(--muted);}
  .died-list li::before{content:"↓ "; color:var(--bad); font-weight:800;}
  .died-empty{color:var(--faint); font-size:13px; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px;}

  /* 5: signal */
  .signal{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:18px 22px;}
  .sig-row{display:grid; grid-template-columns:130px 1fr 130px; align-items:center; gap:16px; padding:12px 0; border-top:1px solid var(--hairline);}
  .sig-row:first-child{border-top:none;}
  .sig-seg{font-weight:700; font-size:14px;}
  .sig-bar{display:flex; align-items:center; gap:8px; margin:4px 0;}
  .sig-lab{font-size:10px; color:var(--faint); font-weight:700; width:20px;}
  .sig-track{flex:1; height:8px; background:rgba(255,255,255,0.06); border-radius:5px; overflow:hidden;}
  .sig-fill{height:100%; border-radius:5px;} .sig-fill.yt{background:var(--yt);} .sig-fill.tt{background:var(--tt);}
  .sig-val{font-size:11px; font-variant-numeric:tabular-nums; color:var(--muted); width:44px; text-align:right;}
  .sig-win{font-size:12px; font-weight:700; color:var(--accent-2); text-align:right;}

  footer{margin-top:50px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint); line-height:1.7;}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandbar">
    <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    <div class="sync">Synced: <b>/*__SYNC__*/</b></div>
  </div>
  <div class="title-block">
    <h1>Trend Intelligence — US</h1>
    <div class="sub">US-only trending hooks on YouTube &amp; TikTok, turned into content you can post and ads you can brief. <span style="color:var(--faint)">/*__COUNTS__*/</span></div>
  </div>

  <section>
    <div class="sec-head"><h2>This Week's Top Hooks</h2><span class="sec-note">US only · ranked by engagement rate</span></div>
    /*__HOOKS__*/
  </section>

  <section>
    <div class="sec-head"><h2>Organic Content Calendar</h2><span class="sec-note">5 pieces to post this week · phone-filmable</span></div>
    <div class="cal-grid">/*__CALENDAR__*/</div>
  </section>

  <section>
    <div class="sec-head"><h2>Paid Ad Creative Briefs</h2><span class="sec-note">3 concepts ready to brief · 15s structure</span></div>
    <div class="paid-grid">/*__PAID__*/</div>
  </section>

  <section>
    <div class="sec-head"><h2>What's Dying</h2><span class="sec-note">Losing traction — don't make these</span></div>
    /*__DIED__*/
  </section>

  <section>
    <div class="sec-head"><h2>Platform Signal</h2><span class="sec-note">Avg engagement · TikTok vs YouTube per segment</span></div>
    <div class="signal">/*__SIGNAL__*/</div>
  </section>

  <footer>
    Data: YouTube Data API (US, trending last 7d) + TikTok via Apify (English-only) · enrichment, calendar &amp; briefs by Claude (claude-sonnet-4-6).<br>
    Rebuilt weekly by pipelines/build_trend_dashboard.py.
  </footer>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        main()
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
