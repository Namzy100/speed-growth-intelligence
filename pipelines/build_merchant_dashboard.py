"""Build the Merchant Discovery dashboard (docs/merchant_dashboard.html).

Outbound discovery, NOT tracking: ranked public venues (forums, trade
publications, associations/events, B2B directories) where Speed could reach
decision-makers at potential merchant businesses. Reads the pipeline output at
data/processed/merchant_candidates.json (produced by merchants/discovery.py).

Self-contained HTML in the same visual language as the creator/trend dashboards
(dark theme, badges, score pills, filterable/sortable table). LinkedIn is shown
as an explicit manual-only exclusion, never as discovered data.

Run from repo root:  python pipelines/build_merchant_dashboard.py
"""

import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_IN = _ROOT / "data" / "processed" / "merchant_candidates.json"
_OUT = _ROOT / "docs" / "merchant_dashboard.html"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _discovered_at(data: dict) -> str:
    """Honest 'venues last discovered' date — the page rebuilds daily, but the
    underlying venue data only changes when merchants/discovery.py is run by hand.

    Order of truth: (1) an explicit `discovered_at` stamped into the JSON by a
    future discovery run; (2) the git commit date of merchant_candidates.json
    (CI-safe — reflects when the venue data actually last changed, unlike file
    mtime which is just the checkout time in a fresh CI clone); (3) file mtime as
    a last resort. Same 'real signal or an honest gap' standard as creator_country
    / audience_location labels elsewhere."""
    stamped = str(data.get("discovered_at", "")).strip()
    if stamped:
        return stamped[:10]
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d", "--", str(_IN)],
            cwd=_ROOT, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(_IN.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def build_rows(data: dict) -> list[dict]:
    rows = []
    for v in data.get("venues", []):
        rows.append({
            "venue": str(v.get("venue", "")),
            "url": str(v.get("url", "")),
            "url_verified": bool(v.get("url_verified", False)),
            "channel_type": v.get("channel_type") or "unclassified",
            "vertical": str(v.get("vertical", "")),
            "fit": float(v.get("fit_score", 0) or 0),
            "relevance": float(v.get("relevance", 0) or 0),
            "payments": float(v.get("payments_access", 0) or 0),
            "tier": str(v.get("audience_tier", "T3")),
            "outreach": v.get("outreach_mode", []) or [],
            "judge_source": str(v.get("judge_source", "")),
            "on_topic": bool(v.get("on_topic")),
            "cross_listing": int(v.get("cross_listing", 0) or 0),
            "reason": str(v.get("reason", "")),
            "source": str(v.get("source", "")),
        })
    rows.sort(key=lambda r: r["fit"], reverse=True)
    return rows


def main() -> None:
    if not _IN.exists():
        raise FileNotFoundError(f"{_IN} not found — run `python merchants/discovery.py` first.")
    data = json.loads(_IN.read_text(encoding="utf-8"))
    rows = build_rows(data)

    from collections import Counter
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "discovered_at": _discovered_at(data),
        "verticals": data.get("vertical_focus", []),
        "excluded": data.get("excluded_channels", {}),
        "venues": rows,
        "total": len(rows),
        "on_topic": sum(1 for r in rows if r["on_topic"]),
        "verified": sum(1 for r in rows if r["url_verified"]),
        "by_channel": dict(Counter(r["channel_type"] for r in rows)),
        "by_source": dict(Counter(r["judge_source"] for r in rows)),
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(_TEMPLATE.replace("/*__DATA__*/", json.dumps(payload)), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  total={payload['total']} on_topic={payload['on_topic']} "
          f"verified_url={payload['verified']} sources={payload['by_source']}")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Merchant Discovery</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1b2230;
    --hairline:rgba(255,255,255,0.09); --hairline-strong:rgba(255,255,255,0.16);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#2f5dfb; --accent-2:#6f9dff; --good:#3fb950; --warn:#e3b341; --bad:#f85149;
    --grad:linear-gradient(120deg,#2f5dfb,#6f9dff);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.7); --shadow-lift:0 18px 44px -16px rgba(0,0,0,0.8);
    --r-lg:16px; --r-md:12px; --r-sm:9px;
  }
  *{box-sizing:border-box}
  body{
    margin:0; color:var(--text); min-height:100vh; letter-spacing:-0.005em;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.5; -webkit-font-smoothing:antialiased;
    background:
      radial-gradient(1100px 600px at 50% -10%, rgba(47,93,251,0.20), transparent 58%),
      radial-gradient(820px 520px at 100% 0%, rgba(111,157,255,0.09), transparent 52%),
      radial-gradient(720px 480px at 0% 8%, rgba(63,185,80,0.045), transparent 50%),
      var(--bg);
    background-attachment:fixed;
  }
  body::before{ content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:0.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); }
  .wrap{position:relative; z-index:1; max-width:1180px; margin:0 auto; padding:0 24px 90px;}

  /* Brand bar */
  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand-left{display:flex; align-items:center; gap:16px; flex-wrap:wrap;}
  .hub-link{font-size:12.5px; font-weight:650; color:var(--accent-2); text-decoration:none;
    background:rgba(47,93,251,0.10); border:1px solid var(--hairline-strong); padding:5px 12px; border-radius:999px; transition:border-color .15s, transform .15s;}
  .hub-link:hover{border-color:var(--accent); transform:translateX(-2px);}
  .brand{font-weight:760; font-size:16px; letter-spacing:-0.02em; display:flex; align-items:center;}
  .brand .bolt{margin-right:8px; font-size:17px; background:linear-gradient(180deg,#f5c400,#f0a02a);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 6px rgba(240,160,42,0.45));}
  .brandbar .sync{font-size:12px; color:var(--muted);} .brandbar .sync b{color:var(--text); font-weight:600;}

  /* Title */
  .title-block{margin:34px 0 22px;}
  h1{font-size:28px; margin:0 0 5px; font-weight:780; letter-spacing:-0.03em;
    background:linear-gradient(180deg,#ffffff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .title-block .sub{color:var(--muted); font-size:13px; max-width:760px;}
  .freshness{margin-top:12px; font-size:12px; color:var(--faint); display:inline-flex; align-items:center; gap:8px;
    background:var(--panel-2); border:1px solid var(--hairline); border-radius:999px; padding:5px 13px;}
  .freshness .dot{width:7px; height:7px; border-radius:50%; background:var(--warn); box-shadow:0 0 7px rgba(227,179,65,.5);}
  .freshness b{color:var(--muted); font-weight:650;}

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
  .sec-note{font-size:12px; color:var(--faint);}

  /* Collapsible "how to read this" legend */
  details.crit{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:0 18px; box-shadow:var(--shadow);}
  details.crit summary{cursor:pointer; list-style:none; padding:15px 0; font-size:12.5px; font-weight:700; color:var(--text); display:flex; align-items:center; gap:9px;}
  details.crit summary::-webkit-details-marker{display:none;}
  details.crit summary .chev{margin-left:auto; color:var(--faint); transition:transform .2s ease;}
  details.crit[open] summary .chev{transform:rotate(90deg);}
  details.crit .body{padding:2px 0 18px; color:var(--muted); font-size:12.5px; line-height:1.75; border-top:1px solid var(--hairline); padding-top:14px;}
  details.crit .body p{margin:0 0 10px;} details.crit .body p:last-child{margin-bottom:0;}
  details.crit .body b{color:var(--text);}

  /* Filters */
  .filters{display:flex; flex-wrap:wrap; gap:16px; align-items:flex-end; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px 18px; box-shadow:var(--shadow); margin-bottom:16px;}
  .filters label{display:block; font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:var(--faint); margin-bottom:5px; font-weight:700;}
  select{background:#0e1117; color:var(--text); border:1px solid var(--hairline); border-radius:8px; padding:8px 11px; font-size:13px; min-width:150px; font-family:inherit;}
  select:focus,input:focus{outline:none; border-color:var(--accent);}
  input[type=range]{vertical-align:middle; width:150px; accent-color:var(--accent);}
  .rangeval{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums;}
  .toggle{display:flex; align-items:center; gap:7px; font-size:13px; color:var(--text); font-weight:500; cursor:pointer;}
  .toggle input{accent-color:var(--accent); width:15px; height:15px;}
  .count{margin-left:auto; color:var(--muted); font-size:13px; align-self:center;}

  /* Table */
  .table-wrap{overflow-x:auto; border:1px solid var(--hairline); border-radius:var(--r-md); background:var(--panel); box-shadow:var(--shadow);}
  table{width:100%; border-collapse:collapse; font-size:13px; min-width:900px;}
  th,td{text-align:left; padding:12px 14px; border-bottom:1px solid var(--hairline); vertical-align:top;}
  th{position:sticky; top:0; background:#10151d; color:var(--faint); font-size:10px; text-transform:uppercase; letter-spacing:0.06em; cursor:pointer; user-select:none; white-space:nowrap; z-index:1;}
  th.num,td.num{text-align:right; font-variant-numeric:tabular-nums;}
  tbody tr{transition:background .14s ease;} tbody tr:hover{background:rgba(255,255,255,0.03);} tbody tr:last-child td{border-bottom:none;}
  a.vname{color:var(--text); font-weight:650; text-decoration:none;} a.vname:hover{color:var(--accent-2);}
  .pending{color:var(--faint); font-size:11px;}
  .detail{color:var(--faint); font-size:11.5px; line-height:1.5; margin-top:4px; max-width:420px;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;}
  .badge{display:inline-flex; align-items:center; font-size:10px; font-weight:800; padding:2px 8px; border-radius:6px; letter-spacing:.02em; white-space:nowrap;}
  .ch-forum{background:#243b55; color:#cfe3ff;} .ch-publication{background:#2b2350; color:#d7ccff;}
  .ch-association_event{background:#12402a; color:#b6f0cf;} .ch-directory{background:#402a12; color:#f0d6b6;}
  .ch-unclassified{background:var(--panel-2); color:var(--faint);}
  .tier{background:var(--panel-2); color:var(--muted); border:1px solid var(--hairline-strong);}
  .tier-T1{color:var(--good); border-color:var(--good);} .tier-T2{color:var(--warn);}
  .src-manual-seed{background:var(--good); color:#08240f;} .src-llm{background:var(--accent); color:#fff;}
  .src-fallback{background:#6b7585; color:#e9edf3;}
  .pill{display:inline-block; min-width:34px; text-align:center; font-weight:800; padding:2px 9px; border-radius:20px; font-size:12px;}
  .pill.hi{background:rgba(63,185,80,0.15); color:var(--good);} .pill.mid{background:rgba(227,179,65,0.15); color:var(--warn);} .pill.lo{background:#3a4150; color:#c3ccd8;}
  .chip{display:inline-block; font-size:10px; background:var(--panel-2); border:1px solid var(--hairline);
    color:var(--muted); border-radius:5px; padding:1px 6px; margin:1px 2px 1px 0; white-space:nowrap;}
  tr.off{opacity:.5;}
  footer{margin-top:40px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint); line-height:1.7;}

  @keyframes rise{from{opacity:0; transform:translateY(12px);} to{opacity:1; transform:none;}}
  .kpi:nth-child(1){animation-delay:.04s}.kpi:nth-child(2){animation-delay:.09s}.kpi:nth-child(3){animation-delay:.14s}.kpi:nth-child(4){animation-delay:.19s}.kpi:nth-child(5){animation-delay:.24s}
  @media (prefers-reduced-motion: reduce){*{animation:none!important; transition:none!important;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandbar">
    <div class="brand-left">
      <a class="hub-link" href="index.html">← Dashboards</a>
      <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    </div>
    <div class="sync">Synced: <b id="sync">—</b></div>
  </div>

  <div class="title-block">
    <h1>Merchant Discovery</h1>
    <div class="sub">Outbound discovery of public venues to reach decision-makers at potential merchant businesses. Not tracking — marketing has no merchant activity data (that's the dev backend).</div>
    <div class="freshness" id="freshness"></div>
  </div>

  <div class="kpi-grid" id="stats"></div>

  <section>
    <div class="sec-head"><h2>How to read this</h2><span class="sec-note">Scoring, tiers &amp; exclusions</span></div>
    <details class="crit">
      <summary>Fit score, audience tier, sources &amp; the LinkedIn exclusion <span class="chev">›</span></summary>
      <div class="body">
        <p><b>Fit</b> = 0.5 vertical + decision-maker relevance + 0.3 payments/outreach access + 0.2 audience. A fit of 0 means the venue is gated as off-topic (player/consumer-facing or off-vertical).</p>
        <p><b>Audience tier</b> (T1/T2/T3) is a <b>reputation / cross-listing tier, NOT real traffic</b> — exact traffic would need a paid API this project doesn't use.</p>
        <p><b>Source</b>: <span class="badge src-manual-seed">manual-seed</span> human-verified investigation · <span class="badge src-llm">llm</span> Claude-judged · <span class="badge src-fallback">fallback</span> provisional keyword guess (upgraded to llm once Anthropic credits are back).</p>
        <p><b>LinkedIn communities are excluded</b> and shown here as manual-only — LinkedIn's ToS/API make community discovery non-automatable, so it is never scraped or faked.</p>
      </div>
    </details>
  </section>

  <section>
    <div class="sec-head"><h2>Discovered Venues</h2><span class="sec-note" id="count"></span></div>

    <div class="filters">
      <div><label>Vertical</label><select id="fVert"><option value="all">All</option></select></div>
      <div><label>Channel type</label><select id="fChan"><option value="all">All</option>
        <option>forum</option><option>publication</option><option>association_event</option><option>directory</option><option>unclassified</option></select></div>
      <div><label>Outreach mode</label><select id="fOut"><option value="all">All</option>
        <option>post content</option><option>advertise</option><option>get listed</option><option>sponsor</option></select></div>
      <div><label>Source</label><select id="fSrc"><option value="all">All</option>
        <option>manual-seed</option><option>llm</option><option>fallback</option></select></div>
      <div><label>Min fit: <span class="rangeval" id="minv">0</span></label><input type="range" id="fMin" min="0" max="10" step="0.5" value="0"></div>
      <label class="toggle"><input type="checkbox" id="fOn"> on-topic only</label>
    </div>

    <div class="table-wrap">
      <table id="tbl"><thead><tr>
        <th data-k="venue">Venue</th><th data-k="channel_type">Channel</th><th data-k="vertical">Vertical</th>
        <th class="num" data-k="fit">Fit</th><th data-k="tier">Audience</th><th data-k="outreach">Outreach</th>
        <th data-k="judge_source">Source</th>
      </tr></thead><tbody id="body"></tbody></table>
    </div>
  </section>

  <footer id="foot"></footer>
</div>

<script>
const DATA = /*__DATA__*/;
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById("sync").textContent = DATA.generated_at;
document.getElementById("freshness").innerHTML =
  '<span class="dot"></span>Page rebuilt daily · <b>venues last discovered ' + esc(DATA.discovered_at) +
  '</b> — the venue set only changes when discovery is re-run by hand';

// KPI strip
const st = [
  [DATA.total, "venues"], [DATA.on_topic, "on-topic"], [DATA.verified, "verified URL"],
  [DATA.by_source["manual-seed"]||0, "human-verified"], [DATA.by_source["llm"]||0, "llm-judged"],
];
document.getElementById("stats").innerHTML = st.map(([n,l])=>`<div class="kpi"><div class="val">${n}</div><div class="lab">${l}</div></div>`).join("");

// vertical filter options
const verts=[...new Set(DATA.venues.map(v=>v.vertical).filter(Boolean))].sort();
const fVert=document.getElementById("fVert");
verts.forEach(v=>{const o=document.createElement("option");o.textContent=v;fVert.appendChild(o);});

function fitCls(f){return f>=7?"hi":f>=4?"mid":"lo";}
function chanLabel(c){return c.replace("association_event","assoc/event");}

let sortKey="fit", sortDir=-1;
function rows(){
  const vert=fVert.value, chan=fChan.value, out=fOut.value, src=fSrc.value,
        min=+fMin.value, on=document.getElementById("fOn").checked;
  let r=DATA.venues.filter(v=>
    (vert==="all"||v.vertical===vert) && (chan==="all"||v.channel_type===chan) &&
    (out==="all"||(v.outreach||[]).includes(out)) && (src==="all"||v.judge_source===src) &&
    v.fit>=min && (!on||v.on_topic));
  r.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];
    if(sortKey==="outreach"){x=(x||[]).length;y=(y||[]).length;}
    if(typeof x==="string"){x=x.toLowerCase();y=y.toLowerCase();}
    return (x<y?-1:x>y?1:0)*sortDir;});
  return r;
}
function render(){
  const r=rows();
  document.getElementById("body").innerHTML = r.map(v=>{
    const name = v.url_verified && v.url
      ? `<a class="vname" href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.venue)}</a>`
      : `<span class="vname">${esc(v.venue)}</span> <span class="pending">(URL to verify)</span>`;
    const chips=(v.outreach||[]).map(o=>`<span class="chip">${esc(o)}</span>`).join("")||'<span class="pending">—</span>';
    return `<tr class="${v.on_topic?'':'off'}">
      <td>${name}<div class="detail" title="${esc(v.reason)}">${esc(v.reason)}</div></td>
      <td><span class="badge ch-${esc(v.channel_type)}">${esc(chanLabel(v.channel_type))}</span></td>
      <td>${esc(v.vertical)}</td>
      <td class="num"><span class="pill ${fitCls(v.fit)}">${v.fit.toFixed(1)}</span>
        <div class="detail">rel ${v.relevance.toFixed(0)} · pay ${v.payments.toFixed(0)}</div></td>
      <td><span class="badge tier tier-${esc(v.tier)}">${esc(v.tier)}</span></td>
      <td>${chips}</td>
      <td><span class="badge src-${esc(v.judge_source)}">${esc(v.judge_source)}</span>
        ${v.cross_listing?`<div class="detail">×${v.cross_listing} listed</div>`:""}</td>
    </tr>`;
  }).join("");
  document.getElementById("count").textContent = `${r.length} of ${DATA.total} shown`;
}
["fVert","fChan","fOut","fSrc"].forEach(id=>document.getElementById(id).addEventListener("change",render));
document.getElementById("fOn").addEventListener("change",render);
document.getElementById("fMin").addEventListener("input",e=>{document.getElementById("minv").textContent=e.target.value;render();});
document.querySelectorAll("#tbl th[data-k]").forEach(th=>th.addEventListener("click",()=>{
  const k=th.dataset.k; if(sortKey===k)sortDir*=-1; else{sortKey=k;sortDir=(k==="venue"||k==="channel_type"||k==="vertical")?1:-1;}
  render();
}));

const excl=Object.entries(DATA.excluded||{}).map(([k,v])=>`<b>${esc(k)}</b>: ${esc(v)}`).join("; ");
document.getElementById("foot").innerHTML =
  `Verticals: ${(DATA.verticals||[]).map(esc).join(", ")||"—"}. Channel mix: `+
  Object.entries(DATA.by_channel).map(([k,n])=>`${esc(k)} ${n}`).join(" · ")+
  `.<br>Excluded (manual only): ${excl}.<br>`+
  `Data: merchants/discovery.py (aggregator harvest + verified seed; web_search discovery + Claude relevance judging pending Anthropic credits).`;
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
