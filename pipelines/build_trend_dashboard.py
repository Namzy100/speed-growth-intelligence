"""Build the Trend Intelligence dashboard (docs/trend_dashboard.html).

Self-contained dark-theme HTML for Speed's marketing team. At build time it calls
intelligence.trend_pipeline.collect_signals() for fresh YouTube + TikTok data,
asks Claude for content recommendations + ad-creative briefs, and bakes it all in.

Sections:
  A. What's trending now — top 5 YouTube + top 5 TikTok per segment
  B. Content recommendations — 3 ideas per segment (Claude)
  C. Ad creative briefs — top 3 formats to test as paid ads (Claude)
  D. What died this week — from the feedback-loop snapshot diff
  E. Platform signal — TikTok vs YouTube engagement per segment

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
_MODEL = "claude-sonnet-4-6"
_BENCHMARK_CPI = 3.17
_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]
_SEG_LABEL = {"remittance": "Remittance", "crypto-curious": "Crypto-Curious", "iGaming": "iGaming"}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_n(n) -> str:
    n = int(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)


# ------------------------------------------------------------------
# Claude: content recs (B) + ad briefs (C)
# ------------------------------------------------------------------

def _digest_for_claude(data: dict) -> str:
    parts = []
    for seg in _SEGMENTS:
        b = data["by_segment"][seg]
        parts.append(f"=== SEGMENT: {seg} ===")
        for label, items in (("YouTube", b["youtube"][:5]), ("TikTok", b["tiktok"][:5])):
            for v in items:
                parts.append(f"  [{label}] {v['views']:,} views, {v['er']:.1%} ER — {v['title'][:100]}")
        if not b["youtube"] and not b["tiktok"]:
            parts.append("  (no trending items this week)")
    return "\n".join(parts)


def generate_ai(data: dict) -> dict:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (
        "You are Speed Wallet's growth-creative strategist. Speed is a Bitcoin Lightning "
        "payments app. Segments: remittance (hook: zero-fee cross-border sends), "
        "crypto-curious (hook: dead-simple first Bitcoin use), iGaming (hook: instant "
        f"deposits/withdrawals). Best paid CPI benchmark: ${_BENCHMARK_CPI:.2f} (Meta).\n\n"
        "Below are THIS WEEK's trending YouTube + TikTok items per segment. Based ONLY on "
        "what's actually trending here, produce two things as a single JSON object:\n\n"
        "1. content_recs: for EACH of remittance, crypto-curious, iGaming, exactly 3 content "
        "ideas the team should make THIS WEEK. Each: hook (literal opening line), format "
        "(one of: 60s video / carousel / Story / 15s Short), platform (TikTok / YouTube / "
        "Instagram Reels), why (one line tying it to Speed's angle + the trend evidence).\n"
        "2. ad_briefs: the 3 strongest trending FORMATS Speed should test as paid ads. Each: "
        "format, hook (the line to steal), script (a tight 15-second outline), audience "
        f"(Meta/TikTok targeting), est_cpi (a range vs the ${_BENCHMARK_CPI:.2f} benchmark + one-line why).\n\n"
        'Return ONLY JSON: {"content_recs":{"remittance":[{"hook":"","format":"","platform":"","why":""}],'
        '"crypto-curious":[...],"iGaming":[...]},"ad_briefs":[{"format":"","hook":"","script":"","audience":"","est_cpi":""}]}\n\n'
        "--- TRENDING THIS WEEK ---\n" + _digest_for_claude(data)
    )
    for attempt in range(2):
        msg = prompt if attempt == 0 else prompt + "\n\nReturn ONLY valid JSON."
        resp = client.messages.create(model=_MODEL, max_tokens=2600,
                                      messages=[{"role": "user", "content": msg}])
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`"); text = text[text.find("{"):]
        try:
            return json.loads(text[text.find("{"): text.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError):
            continue
    return {"content_recs": {}, "ad_briefs": []}


# ------------------------------------------------------------------
# Render sections
# ------------------------------------------------------------------

def _card(v: dict) -> str:
    is_yt = v["platform"] == "YouTube"
    if is_yt and v.get("thumbnail"):
        media = f'<div class="thumb"><img src="{_e(v["thumbnail"])}" alt="" loading="lazy"><span class="pill yt">YouTube</span></div>'
    else:
        icon = "▶" if is_yt else "♪"
        cls = "yt" if is_yt else "tt"
        media = f'<div class="thumb noimg {cls}"><span class="glyph">{icon}</span><span class="pill {cls}">{_e(v["platform"])}</span></div>'
    return f"""
      <a class="tcard" href="{_e(v['url'])}" target="_blank" rel="noopener">
        {media}
        <div class="tbody">
          <div class="ttitle">{_e(v['title'][:110])}</div>
          <div class="tchan">{_e(v['channel'])}</div>
          <div class="tstats"><span>{_fmt_n(v['views'])} views</span><span class="er">{v['er']:.1%} ER</span><span class="date">{_e(v['publish_date'])}</span></div>
        </div>
      </a>"""


def render_trending(data: dict) -> str:
    blocks = []
    for seg in _SEGMENTS:
        b = data["by_segment"][seg]
        yt = "".join(_card(v) for v in b["youtube"][:5]) or '<div class="empty">No trending YouTube this week.</div>'
        tt = "".join(_card(v) for v in b["tiktok"][:5]) or '<div class="empty">No trending TikTok this week.</div>'
        blocks.append(f"""
      <div class="seg-block">
        <div class="seg-h"><span class="seg-chip {seg.replace('-','')}">{_e(_SEG_LABEL[seg])}</span></div>
        <div class="lane"><div class="lane-lab">YouTube</div><div class="cards">{yt}</div></div>
        <div class="lane"><div class="lane-lab">TikTok</div><div class="cards">{tt}</div></div>
      </div>""")
    return "\n".join(blocks)


def render_content(ai: dict) -> str:
    recs = ai.get("content_recs", {})
    cols = []
    for seg in _SEGMENTS:
        items = recs.get(seg, [])[:3]
        cards = "".join(f"""
        <div class="idea">
          <div class="idea-hook">“{_e(i.get('hook'))}”</div>
          <div class="idea-meta"><span class="fmt">{_e(i.get('format'))}</span><span class="plat">{_e(i.get('platform'))}</span></div>
          <div class="idea-why">{_e(i.get('why'))}</div>
        </div>""" for i in items) or '<div class="empty">—</div>'
        cols.append(f'<div class="rec-col"><div class="seg-chip {seg.replace("-","")}">{_e(_SEG_LABEL[seg])}</div>{cards}</div>')
    return f'<div class="rec-grid">{"".join(cols)}</div>'


def render_briefs(ai: dict) -> str:
    briefs = ai.get("ad_briefs", [])[:3]
    if not briefs:
        return '<div class="empty">No briefs generated.</div>'
    cards = []
    for i, b in enumerate(briefs, 1):
        cards.append(f"""
      <div class="brief">
        <div class="brief-n">{i}</div>
        <div class="brief-body">
          <div class="brief-fmt">{_e(b.get('format'))}</div>
          <div class="brief-row"><span class="k">Hook to steal</span><span class="v">“{_e(b.get('hook'))}”</span></div>
          <div class="brief-row"><span class="k">15-sec script</span><span class="v">{_e(b.get('script'))}</span></div>
          <div class="brief-row"><span class="k">Audience</span><span class="v">{_e(b.get('audience'))}</span></div>
          <div class="brief-row"><span class="k">Est. CPI</span><span class="v cpi">{_e(b.get('est_cpi'))}</span></div>
        </div>
      </div>""")
    return f'<div class="briefs">{"".join(cards)}</div>'


def render_died(data: dict) -> str:
    died = data.get("died", [])
    if not died:
        return ('<div class="died-empty">No week-over-week decline data yet — this is a '
                'baseline week. Next Monday\'s run will flag formats that lost traction.</div>')
    return '<ul class="died-list">' + "".join(f"<li>{_e(d)}</li>" for d in died[:8]) + "</ul>"


def render_signal(data: dict) -> str:
    sig = data.get("platform_signal", {})
    rows = []
    for seg in _SEGMENTS:
        s = sig.get(seg, {})
        yt, tt = s.get("youtube_er", 0), s.get("tiktok_er", 0)
        mx = max(yt, tt, 0.0001)
        stronger = "TikTok" if tt > yt else "YouTube" if yt > tt else "Even"
        rows.append(f"""
      <div class="sig-row">
        <div class="sig-seg">{_e(_SEG_LABEL[seg])}</div>
        <div class="sig-bars">
          <div class="sig-bar"><span class="sig-lab">YT</span><div class="sig-track"><div class="sig-fill yt" style="width:{yt/mx*100:.0f}%"></div></div><span class="sig-val">{yt:.1%}</span></div>
          <div class="sig-bar"><span class="sig-lab">TT</span><div class="sig-track"><div class="sig-fill tt" style="width:{tt/mx*100:.0f}%"></div></div><span class="sig-val">{tt:.1%}</span></div>
        </div>
        <div class="sig-win">{stronger} stronger</div>
      </div>""")
    return "\n".join(rows)


# ------------------------------------------------------------------
# Assemble
# ------------------------------------------------------------------

def render(data: dict, ai: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    repl = {
        "/*__SYNC__*/": now,
        "/*__TRENDING__*/": render_trending(data),
        "/*__CONTENT__*/": render_content(ai),
        "/*__BRIEFS__*/": render_briefs(ai),
        "/*__DIED__*/": render_died(data),
        "/*__SIGNAL__*/": render_signal(data),
        "/*__COUNTS__*/": f"{len(data['youtube'])} YouTube · {len(data['tiktok'])} TikTok videos this week",
    }
    out = _TEMPLATE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    print("Collecting fresh trend signals (YouTube + TikTok)...")
    data = trend_pipeline.collect_signals()
    print("Generating content recs + ad briefs (Claude)...")
    ai = generate_ai(data)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render(data, ai), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    recs = ai.get("content_recs", {})
    print(f"  YouTube={len(data['youtube'])} TikTok={len(data['tiktok'])} · "
          f"content ideas={sum(len(recs.get(s, [])) for s in _SEGMENTS)} · "
          f"ad briefs={len(ai.get('ad_briefs', []))}")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Trend Intelligence</title>
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
  .wrap{max-width:1200px; margin:0 auto; padding:0 24px 90px;}
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
  .seg-chip{display:inline-block; font-size:11px; font-weight:800; letter-spacing:0.03em; padding:4px 12px; border-radius:20px; color:#0d1117;}
  .seg-chip.remittance{background:var(--seg-remittance);} .seg-chip.cryptocurious{background:var(--seg-cryptocurious);} .seg-chip.iGaming{background:var(--seg-iGaming);}

  /* A: trending lanes */
  .seg-block{margin-bottom:30px;}
  .seg-h{margin-bottom:12px;}
  .lane{margin:10px 0;}
  .lane-lab{font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:var(--faint); font-weight:700; margin-bottom:8px;}
  .cards{display:flex; gap:14px; overflow-x:auto; padding-bottom:8px;}
  .tcard{flex:0 0 250px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); overflow:hidden; text-decoration:none; color:var(--text); transition:transform .2s ease, border-color .2s ease;}
  .tcard:hover{transform:translateY(-3px); border-color:var(--hairline-strong);}
  .thumb{position:relative; aspect-ratio:16/9; background:#000; overflow:hidden;}
  .thumb img{width:100%; height:100%; object-fit:cover;}
  .thumb.noimg{display:flex; align-items:center; justify-content:center;}
  .thumb.noimg.tt{background:linear-gradient(135deg,#111,#0c2a2a);} .thumb.noimg.yt{background:linear-gradient(135deg,#1a0000,#2a0c0c);}
  .thumb .glyph{font-size:44px; opacity:0.6;}
  .thumb.tt .glyph{color:var(--tt);} .thumb.yt .glyph{color:var(--yt);}
  .pill{position:absolute; top:8px; left:8px; font-size:9.5px; font-weight:800; padding:2px 7px; border-radius:5px; text-transform:uppercase; letter-spacing:0.04em;}
  .pill.yt{background:var(--yt); color:#fff;} .pill.tt{background:var(--tt); color:#04201f;}
  .tbody{padding:12px 13px;}
  .ttitle{font-size:13px; font-weight:650; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:35px;}
  .tchan{font-size:11.5px; color:var(--faint); margin:5px 0 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .tstats{display:flex; gap:9px; font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; flex-wrap:wrap;}
  .tstats .er{color:var(--good); font-weight:700;} .tstats .date{color:var(--faint); margin-left:auto;}
  .empty{color:var(--faint); font-size:13px; padding:10px 0;}

  /* B: content recs */
  .rec-grid{display:grid; grid-template-columns:repeat(3,1fr); gap:18px;}
  @media(max-width:900px){.rec-grid{grid-template-columns:1fr;}}
  .rec-col{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px;}
  .rec-col .seg-chip{margin-bottom:12px;}
  .idea{border-top:1px solid var(--hairline); padding:12px 0;}
  .idea:first-of-type{border-top:none;}
  .idea-hook{font-size:14px; font-weight:650; line-height:1.4;}
  .idea-meta{display:flex; gap:8px; margin:8px 0 6px;}
  .idea-meta span{font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:6px;}
  .idea-meta .fmt{background:rgba(163,113,245,0.15); color:var(--accent-2);}
  .idea-meta .plat{background:var(--panel-2); color:var(--muted);}
  .idea-why{font-size:12.5px; color:var(--muted); line-height:1.5;}

  /* C: ad briefs */
  .briefs{display:grid; grid-template-columns:repeat(3,1fr); gap:16px;}
  @media(max-width:900px){.briefs{grid-template-columns:1fr;}}
  .brief{display:flex; gap:14px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); padding:18px;}
  .brief-n{font-size:26px; font-weight:850; color:var(--accent-2); flex:0 0 auto;}
  .brief-fmt{font-size:15px; font-weight:740; margin-bottom:10px;}
  .brief-row{margin:9px 0;}
  .brief-row .k{display:block; font-size:9.5px; text-transform:uppercase; letter-spacing:0.07em; color:var(--faint); font-weight:700; margin-bottom:2px;}
  .brief-row .v{font-size:12.5px; color:var(--text); line-height:1.45;}
  .brief-row .v.cpi{color:var(--good); font-weight:700;}

  /* D: what died */
  .died-list{list-style:none; display:flex; flex-direction:column; gap:9px;}
  .died-list li{background:rgba(248,81,73,0.07); border:1px solid rgba(248,81,73,0.25); border-radius:var(--r-sm); padding:11px 15px; font-size:13px; color:var(--muted);}
  .died-list li::before{content:"↓ "; color:var(--bad); font-weight:800;}
  .died-empty{color:var(--faint); font-size:13px; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px;}

  /* E: platform signal */
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
    <h1>Trend Intelligence</h1>
    <div class="sub">What's trending in Speed's categories on YouTube &amp; TikTok — and exactly what to make about it. <span style="color:var(--faint)">/*__COUNTS__*/</span></div>
  </div>

  <section>
    <div class="sec-head"><h2>What's Trending Now</h2><span class="sec-note">Top 5 per platform, per segment · this week · ranked by views</span></div>
    /*__TRENDING__*/
  </section>

  <section>
    <div class="sec-head"><h2>Content Recommendations</h2><span class="sec-note">Make these this week</span></div>
    /*__CONTENT__*/
  </section>

  <section>
    <div class="sec-head"><h2>Ad Creative Briefs</h2><span class="sec-note">Top 3 trending formats to test as paid ads</span></div>
    /*__BRIEFS__*/
  </section>

  <section>
    <div class="sec-head"><h2>What Died This Week</h2><span class="sec-note">Losing traction vs last week — don't make these</span></div>
    /*__DIED__*/
  </section>

  <section>
    <div class="sec-head"><h2>Platform Signal</h2><span class="sec-note">Avg engagement rate · TikTok vs YouTube per segment</span></div>
    <div class="signal">/*__SIGNAL__*/</div>
  </section>

  <footer>
    Data: YouTube Data API (trending, last 7 days) + TikTok via Apify · recommendations &amp; briefs by Claude (claude-sonnet-4-6).<br>
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
