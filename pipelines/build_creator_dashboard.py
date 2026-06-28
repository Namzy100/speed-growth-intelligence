"""Build a self-contained creator-intelligence dashboard from live Supabase data.

Reads every creator from Supabase at build time and bakes the data into a single
self-contained HTML file (docs/creator_dashboard.html) — same pattern + aesthetic
as build_creative_dashboard.py (dark theme, purple accent, Speed Wallet brand).
A top-20 card grid headlines, with a searchable/filterable table for the full set
and a collapsible scoring-criteria explainer.

Run from repo root:  python pipelines/build_creator_dashboard.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from creators import database
from creators.youtube_batch import EXCLUDED_BRANDS

_OUT = _ROOT / "docs" / "creator_dashboard.html"
_COMPETITOR_TOKENS = [b.lower() for b in EXCLUDED_BRANDS]


def _brand_flag(tags: list) -> bool:
    """True if any niche tag references a competitor/brand token."""
    for t in tags:
        tl = str(t).lower()
        if any(b in tl for b in _COMPETITOR_TOKENS):
            return True
    return False


def build_rows() -> list[dict]:
    rows = database.get_all_creators()  # ordered by composite_score desc
    out = []
    for r in rows:
        tags = r.get("niche_tags") or []
        out.append({
            "name": str(r.get("name", "")),
            "platform": str(r.get("platform", "")),
            "followers": int(r.get("followers", 0) or 0),
            "segment": str(r.get("segment_tag", "general")),
            "score": round(float(r.get("composite_score", 0) or 0), 1),
            "drs": round(float(r.get("deposit_relevance_score", 0) or 0), 1),
            "infl": round(float(r.get("influencer_score", 0) or 0), 1),
            "is_influencer": bool(r.get("is_influencer", False)),
            "outreach": str(r.get("outreach_status", "not_contacted")),
            "tags": [str(t) for t in tags[:6]],
            "brand_flag": _brand_flag(tags),
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def main() -> None:
    print("Reading creators from Supabase...")
    creators = build_rows()
    print(f"  {len(creators)} creators")

    from collections import Counter
    seg_counts = Counter(c["segment"] for c in creators)
    plat_counts = Counter(c["platform"] for c in creators)

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "creators": creators,
        "total": len(creators),
        "segments": dict(seg_counts),
        "platforms": dict(plat_counts),
        "flagged": sum(1 for c in creators if c["brand_flag"]),
        "avg_score": round(sum(c["score"] for c in creators) / len(creators), 1) if creators else 0,
        "influencers": sum(1 for c in creators if c["is_influencer"]),
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(_TEMPLATE.replace("/*__DATA__*/", json.dumps(data)), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  total={data['total']} platforms={data['platforms']} flagged={data['flagged']}")


# ------------------------------------------------------------------
# Self-contained HTML template — data injected at /*__DATA__*/.
# Matches the creative dashboard: dark #0d1117, panels #161b22, accent #6e40c9.
# ------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Creator Intelligence</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1b2230;
    --hairline:rgba(255,255,255,0.09); --hairline-strong:rgba(255,255,255,0.16);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#6e40c9; --accent-2:#a371f7;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149;
    --grad:linear-gradient(120deg,#6e40c9,#a371f7);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.7);
    --shadow-lift:0 18px 44px -16px rgba(0,0,0,0.8);
    --r-lg:16px; --r-md:12px; --r-sm:9px;
    --seg-remittance:#3fb950; --seg-iGaming:#e3b341;
    --seg-crypto-curious:#a371f7; --seg-general:#6b7585;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{
    margin:0; color:var(--text); min-height:100vh; letter-spacing:-0.005em;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.5; -webkit-font-smoothing:antialiased;
    background:
      radial-gradient(1100px 600px at 50% -10%, rgba(110,64,201,0.20), transparent 58%),
      radial-gradient(820px 520px at 100% 0%, rgba(163,113,245,0.09), transparent 52%),
      radial-gradient(720px 480px at 0% 8%, rgba(63,185,80,0.045), transparent 50%),
      var(--bg);
    background-attachment:fixed;
  }
  body::before{ content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:0.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); }
  .wrap{position:relative; z-index:1; max-width:1240px; margin:0 auto; padding:0 24px 90px;}

  /* Brand bar */
  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand{font-weight:760; font-size:16px; letter-spacing:-0.02em; display:flex; align-items:center;}
  .brand .bolt{margin-right:8px; font-size:17px; background:linear-gradient(180deg,#ffd66e,#f0a02a);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 6px rgba(240,160,42,0.45));}
  .brandbar .sync{font-size:12px; color:var(--muted);} .brandbar .sync b{color:var(--text); font-weight:600;}

  /* Title */
  .title-block{margin:34px 0 22px;}
  h1{font-size:28px; margin:0 0 5px; font-weight:780; letter-spacing:-0.03em;
    background:linear-gradient(180deg,#ffffff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .title-block .sub{color:var(--muted); font-size:13px;}

  /* KPI strip */
  .kpi-grid{display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:8px;}
  @media(max-width:880px){.kpi-grid{grid-template-columns:repeat(2,1fr);}}
  .kpi{padding:16px 18px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.6));
    border:1px solid var(--hairline); border-radius:var(--r-md); box-shadow:var(--shadow);
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both;}
  .kpi .val{font-size:24px; font-weight:760; letter-spacing:-0.02em; font-variant-numeric:tabular-nums;
    background:linear-gradient(180deg,#fff,#cfd6e4); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .kpi .lab{font-size:10px; text-transform:uppercase; letter-spacing:0.09em; color:var(--faint); margin-top:7px; font-weight:700;}

  section{margin:40px 0; animation:rise .55s cubic-bezier(.2,.7,.2,1) both;}
  .sec-head{display:flex; align-items:center; gap:11px; margin-bottom:18px; flex-wrap:wrap;}
  h2{font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted); margin:0; font-weight:700; display:flex; align-items:center; gap:10px;}
  h2::before{content:""; width:3px; height:13px; border-radius:2px; background:var(--grad);}
  .note{font-size:12px; color:var(--faint);}

  /* Card grid */
  .cards{display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px;}
  .card{position:relative; padding:18px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55));
    border:1px solid var(--hairline); border-radius:var(--r-lg); box-shadow:var(--shadow);
    transition:transform .22s ease, border-color .22s ease, box-shadow .22s ease;
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both;}
  .card:hover{transform:translateY(-3px); border-color:var(--hairline-strong); box-shadow:var(--shadow-lift);}
  .card.flagged{border-color:rgba(248,81,73,0.4);}
  .card-top{display:flex; gap:13px; align-items:center;}
  .avatar{flex:0 0 auto; width:46px; height:46px; border-radius:50%; display:flex; align-items:center; justify-content:center;
    font-weight:760; font-size:17px; color:#fff; letter-spacing:-0.02em; box-shadow:inset 0 0 0 1px rgba(255,255,255,0.12);}
  .card-id{min-width:0; flex:1;}
  .card-name{font-weight:680; font-size:15px; letter-spacing:-0.01em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .card-meta{display:flex; gap:6px; align-items:center; margin-top:5px; flex-wrap:wrap;}
  .ring{flex:0 0 auto; width:60px; height:60px; position:relative;}
  .ring svg{transform:rotate(-90deg);}
  .ring .rv{position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-weight:760; font-size:15px; font-variant-numeric:tabular-nums;}
  .card-foot{display:flex; justify-content:space-between; align-items:center; margin-top:15px; padding-top:13px; border-top:1px solid var(--hairline); font-size:12px;}
  .card-foot .k{color:var(--faint); font-size:10px; text-transform:uppercase; letter-spacing:0.06em;}
  .card-foot .v{font-weight:650; font-variant-numeric:tabular-nums;}

  .badge{display:inline-flex; align-items:center; font-size:10.5px; font-weight:700; padding:2px 9px; border-radius:20px; letter-spacing:0.02em; line-height:1.6;}
  .badge.plat{background:var(--panel-2); color:var(--muted); border:1px solid var(--hairline);}
  .badge.seg{color:#0d1117;}
  .seg-remittance{background:var(--seg-remittance);} .seg-iGaming{background:var(--seg-iGaming);}
  .seg-crypto-curious{background:var(--seg-crypto-curious);} .seg-general{background:var(--seg-general); color:#e9edf3;}
  .out-pill{font-size:11px; color:var(--muted);}
  .flagchip{font-size:10px; font-weight:700; color:var(--bad); background:rgba(248,81,73,0.14); padding:1px 7px; border-radius:5px;}

  /* Collapsible scoring */
  details.crit{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:0 18px; box-shadow:var(--shadow);}
  details.crit summary{cursor:pointer; list-style:none; padding:15px 0; font-size:12.5px; font-weight:700; color:var(--text); display:flex; align-items:center; gap:9px;}
  details.crit summary::-webkit-details-marker{display:none;}
  details.crit summary .chev{margin-left:auto; color:var(--faint); transition:transform .2s ease;}
  details.crit[open] summary .chev{transform:rotate(90deg);}
  details.crit .body{padding:0 0 18px; color:var(--muted); font-size:13px; border-top:1px solid var(--hairline); padding-top:14px;}
  .dims{display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-top:12px;}
  @media(max-width:760px){.dims{grid-template-columns:1fr 1fr;}}
  .dim{background:var(--panel-2); border:1px solid var(--hairline); border-radius:var(--r-sm); padding:10px 12px;}
  .dim b{color:var(--accent-2); font-size:12.5px;} .dim span{display:block; color:var(--faint); font-size:11px; margin-top:3px;}
  .segline{margin-top:12px;} .segline .badge{margin-right:7px;}

  /* Filters + table */
  .filters{display:flex; flex-wrap:wrap; gap:16px; align-items:flex-end; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px 18px; box-shadow:var(--shadow);}
  .filters label{display:block; font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:var(--faint); margin-bottom:5px; font-weight:700;}
  select,input[type=text]{background:#0e1117; color:var(--text); border:1px solid var(--hairline); border-radius:8px; padding:8px 11px; font-size:13px; min-width:150px; font-family:inherit;}
  .toggle{display:flex; align-items:center; gap:7px; font-size:13px; color:var(--text); text-transform:none; letter-spacing:0; font-weight:500; cursor:pointer;}
  .toggle input{accent-color:var(--accent); width:15px; height:15px;}
  .infl-dot{color:var(--accent-2); font-size:10px;}
  select:focus,input:focus{outline:none; border-color:var(--accent);}
  input[type=range]{vertical-align:middle; width:150px; accent-color:var(--accent);}
  .rangeval{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums;}
  .count{margin-left:auto; color:var(--muted); font-size:13px;}

  .table-wrap{overflow-x:auto; border:1px solid var(--hairline); border-radius:var(--r-md); margin-top:16px; background:var(--panel);}
  table{width:100%; border-collapse:collapse; font-size:13px; min-width:880px;}
  th,td{text-align:left; padding:10px 13px; border-bottom:1px solid var(--hairline); white-space:nowrap;}
  th{position:sticky; top:0; background:#10151d; color:var(--faint); font-size:10px; text-transform:uppercase; letter-spacing:0.06em; cursor:pointer; user-select:none; z-index:1;}
  th.num,td.num{text-align:right; font-variant-numeric:tabular-nums;}
  tbody tr{transition:background .14s ease;} tbody tr:hover{background:rgba(255,255,255,0.03);} tbody tr:last-child td{border-bottom:none;}
  .score-pill{font-weight:700; font-variant-numeric:tabular-nums; padding:2px 9px; border-radius:20px;}
  .s-good{color:var(--good); background:rgba(63,185,80,0.13);} .s-warn{color:var(--warn); background:rgba(227,179,65,0.13);} .s-bad{color:var(--bad); background:rgba(248,81,73,0.13);}
  td.tags{white-space:normal; max-width:260px;} .tag{display:inline-block; background:var(--panel-2); border-radius:5px; padding:1px 6px; margin:1px 3px 1px 0; font-size:11px; color:var(--muted);}
  .muted{color:var(--muted);} .flag-no{color:var(--faint);}
  footer{margin-top:46px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint);}

  @keyframes rise{from{opacity:0; transform:translateY(12px);} to{opacity:1; transform:none;}}
  .kpi:nth-child(1){animation-delay:.04s}.kpi:nth-child(2){animation-delay:.09s}.kpi:nth-child(3){animation-delay:.14s}.kpi:nth-child(4){animation-delay:.19s}.kpi:nth-child(5){animation-delay:.24s}
  @media (prefers-reduced-motion: reduce){*{animation:none!important; transition:none!important;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandbar">
    <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    <div class="sync">Synced: <b id="syncTime">—</b></div>
  </div>

  <div class="title-block">
    <h1>Creator Intelligence</h1>
    <div class="sub">Partner scouting across YouTube &amp; TikTok · scored for Speed's three segments</div>
  </div>

  <div class="kpi-grid" id="kpis"></div>

  <section>
    <div class="sec-head"><h2>Top 20 Partner Candidates</h2><span class="note">Ranked by composite score</span></div>
    <div class="cards" id="cards"></div>
  </section>

  <section>
    <div class="sec-head"><h2>Scoring Method</h2></div>
    <details class="crit">
      <summary>How creators are scored <span class="chev">›</span></summary>
      <div class="body">
        Each creator is scored across five dimensions, each out of 20, for a composite out of 100.
        Composite colour: <span class="score-pill s-good">green &gt;50</span>
        <span class="score-pill s-warn">yellow 30–50</span>
        <span class="score-pill s-bad">red &lt;30</span>.
        A red brand flag means the creator's niche tags reference a competitor.
        <div class="dims">
          <div class="dim"><b>Audience fit</b><span>match to Speed's segments</span></div>
          <div class="dim"><b>Engagement quality</b><span>real vs inflated reach</span></div>
          <div class="dim"><b>Content alignment</b><span>crypto / fintech focus</span></div>
          <div class="dim"><b>Acquisition potential</b><span>install-driving reach</span></div>
          <div class="dim"><b>Deposit relevance</b><span>likelihood of USD deposits</span></div>
        </div>
        <div class="segline">
          Segments:
          <span class="badge seg seg-remittance">remittance</span>
          <span class="badge seg seg-iGaming">iGaming</span>
          <span class="badge seg seg-crypto-curious">crypto-curious</span>
          <span class="badge seg seg-general">general</span>
        </div>
      </div>
    </details>
  </section>

  <section>
    <div class="sec-head"><h2>All Creators</h2><span class="note" id="tableNote"></span></div>
    <div class="filters">
      <div><label>Segment</label>
        <select id="fSeg"><option value="all">All segments</option><option>remittance</option><option>iGaming</option><option>crypto-curious</option><option>general</option></select></div>
      <div><label>Platform</label>
        <select id="fPlat"><option value="all">All platforms</option><option>YouTube</option><option>TikTok</option></select></div>
      <div><label>Min score: <span class="rangeval" id="minVal">0</span></label>
        <input type="range" id="fMin" min="0" max="100" value="0" step="1"></div>
      <div><label>Search</label><input type="text" id="fSearch" placeholder="name contains…"></div>
      <div><label>&nbsp;</label><label class="toggle"><input type="checkbox" id="fInfl"> Influencers only</label></div>
      <div class="count" id="count"></div>
    </div>
    <div class="table-wrap"><table id="tbl">
      <thead><tr>
        <th data-k="name">Creator</th><th data-k="platform">Platform</th><th data-k="segment">Segment</th>
        <th class="num" data-k="followers">Followers</th><th class="num" data-k="score">Score</th>
        <th class="num" data-k="infl">Influencer</th>
        <th class="num" data-k="drs">Deposit Rel.</th><th data-k="outreach">Outreach</th>
        <th data-k="brand_flag">Brand</th><th>Niche Tags</th>
      </tr></thead><tbody></tbody>
    </table></div>
  </section>

  <footer>Data baked live from Supabase at build time · rebuilt daily by pipelines/build_creator_dashboard.py</footer>
</div>

<script>
const DATA = /*__DATA__*/;
const intFmt = new Intl.NumberFormat("en-US");
const esc = s => { const d=document.createElement("div"); d.textContent=(s==null?"":s); return d.innerHTML; };
const segClass = s => "seg-" + String(s).replace(/[^a-zA-Z-]/g,"");
const scoreCls = v => v > 50 ? "s-good" : (v >= 30 ? "s-warn" : "s-bad");
const scoreColor = v => v > 50 ? "#3fb950" : (v >= 30 ? "#e3b341" : "#f85149");
const segColor = s => getComputedStyle(document.documentElement).getPropertyValue("--seg-"+String(s).replace(/[^a-zA-Z-]/g,"")).trim() || "#6b7585";
const fmtFollow = n => n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(n>=1e4?0:1)+"k" : String(n);
const initials = n => (n.trim().split(/\s+/).map(w=>w[0]).join("").slice(0,2) || "?").toUpperCase();

document.getElementById("syncTime").textContent = DATA.generated_at || "—";

function renderKPIs(){
  const s = DATA.segments || {};
  const k = [
    [DATA.total, "Creators"],
    [(DATA.platforms.YouTube||0)+" / "+(DATA.platforms.TikTok||0), "YouTube / TikTok"],
    [DATA.influencers, "Influencers"],
    [(s["remittance"]||0)+" · "+(s["iGaming"]||0)+" · "+(s["crypto-curious"]||0), "Rem · iGam · Crypto"],
    [DATA.flagged, "Brand-flagged"],
  ];
  document.getElementById("kpis").innerHTML = k.map(([v,l]) =>
    `<div class="kpi"><div class="val">${esc(v)}</div><div class="lab">${esc(l)}</div></div>`).join("");
}

function ring(score){
  const r=24, c=2*Math.PI*r, off=c*(1-Math.min(score,100)/100), col=scoreColor(score);
  return `<div class="ring"><svg width="60" height="60" viewBox="0 0 60 60">
    <circle cx="30" cy="30" r="${r}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="5"/>
    <circle cx="30" cy="30" r="${r}" fill="none" stroke="${col}" stroke-width="5" stroke-linecap="round"
      stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/>
  </svg><div class="rv" style="color:${col}">${score.toFixed(0)}</div></div>`;
}

function renderCards(){
  const top = DATA.creators.slice(0, 20);
  document.getElementById("cards").innerHTML = top.map((c,i) => `
    <div class="card ${c.brand_flag?'flagged':''}" style="animation-delay:${(0.03+i*0.02).toFixed(2)}s">
      <div class="card-top">
        <div class="avatar" style="background:linear-gradient(135deg, ${segColor(c.segment)}, #2a2140)">${esc(initials(c.name))}</div>
        <div class="card-id">
          <div class="card-name" title="${esc(c.name)}">${esc(c.name)}</div>
          <div class="card-meta">
            <span class="badge plat">${esc(c.platform)}</span>
            <span class="badge seg ${segClass(c.segment)}">${esc(c.segment)}</span>
            ${c.brand_flag?'<span class="flagchip">⚑</span>':''}
          </div>
        </div>
        ${ring(c.score)}
      </div>
      <div class="card-foot">
        <div><span class="k">Followers</span> <span class="v">${fmtFollow(c.followers)}</span></div>
        <div><span class="k">Deposit rel.</span> <span class="v">${c.drs.toFixed(1)}</span></div>
        <div><span class="out-pill">${esc(c.outreach)}</span></div>
      </div>
    </div>`).join("");
}

let sortKey="score", sortDir=-1;
function rows(){
  const seg=fSeg.value, plat=fPlat.value, min=+fMin.value, q=fSearch.value.trim().toLowerCase();
  const inflOnly = document.getElementById("fInfl").checked;
  let r = DATA.creators.filter(c =>
    (seg==="all"||c.segment===seg) && (plat==="all"||c.platform===plat) && c.score>=min &&
    (!inflOnly||c.is_influencer) && (!q||c.name.toLowerCase().includes(q)));
  r.sort((a,b)=>{let x=a[sortKey],y=b[sortKey]; if(typeof x==="string"){x=x.toLowerCase();y=y.toLowerCase();} return (x<y?-1:x>y?1:0)*sortDir;});
  return r;
}
function renderTable(){
  const r = rows(), tb=document.querySelector("#tbl tbody");
  tb.innerHTML = r.map(c => {
    const tags = c.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join(" ") || "<span class='flag-no'>—</span>";
    const flag = c.brand_flag ? "<span class='flagchip'>⚑ brand</span>" : "<span class='flag-no'>—</span>";
    return `<tr>
      <td>${esc(c.name)}</td>
      <td><span class="badge plat">${esc(c.platform)}</span></td>
      <td><span class="badge seg ${segClass(c.segment)}">${esc(c.segment)}</span></td>
      <td class="num">${intFmt.format(c.followers)}</td>
      <td class="num"><span class="score-pill ${scoreCls(c.score)}">${c.score.toFixed(1)}</span></td>
      <td class="num">${c.infl.toFixed(1)}${c.is_influencer?' <span class="infl-dot" title="influencer">●</span>':''}</td>
      <td class="num">${c.drs.toFixed(1)}</td>
      <td class="muted">${esc(c.outreach)}</td>
      <td>${flag}</td>
      <td class="tags">${tags}</td>
    </tr>`;
  }).join("");
  document.getElementById("count").textContent = `${r.length} of ${DATA.total} shown`;
}
["fSeg","fPlat","fSearch"].forEach(id=>document.getElementById(id).addEventListener("input",renderTable));
document.getElementById("fInfl").addEventListener("change",renderTable);
fMin.addEventListener("input",e=>{document.getElementById("minVal").textContent=e.target.value; renderTable();});
document.querySelectorAll("#tbl th[data-k]").forEach(th=>th.addEventListener("click",()=>{
  const k=th.dataset.k;
  if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=(k==="name"||k==="segment"||k==="platform"||k==="outreach")?1:-1;}
  renderTable();
}));

document.getElementById("tableNote").textContent = `${DATA.total} total`;
renderKPIs(); renderCards(); renderTable();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
