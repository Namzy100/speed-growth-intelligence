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
    --r-lg:16px; --r-md:12px; --r-sm:9px; --shadow:0 10px 30px -14px rgba(0,0,0,0.7);
  }
  *{box-sizing:border-box;} body{margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
  .wrap{max-width:1180px; margin:0 auto; padding:26px 20px 60px;}
  .brandbar{display:flex; justify-content:space-between; align-items:center; margin-bottom:18px;}
  .brand{font-weight:800; letter-spacing:.02em;} .brand .bolt{color:var(--warn); margin-right:6px;}
  .sync{font-size:12px; color:var(--faint);}
  h1{font-size:23px; margin:0 0 4px;} .sub{color:var(--muted); font-size:13px; margin-bottom:18px;}
  .stats{display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;}
  .stat{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:10px 14px; min-width:96px;}
  .stat b{display:block; font-size:20px;} .stat span{font-size:11px; color:var(--faint);}
  .note{background:var(--panel-2); border:1px solid var(--hairline); border-left:3px solid var(--warn);
    border-radius:var(--r-sm); padding:11px 13px; font-size:12px; color:var(--muted); line-height:1.7; margin-bottom:18px;}
  .note b{color:var(--text);}
  .filters{display:flex; gap:12px; flex-wrap:wrap; align-items:end; margin-bottom:14px;}
  .filters label{display:block; font-size:11px; color:var(--faint); margin-bottom:3px;}
  select,input[type=range]{background:var(--panel); border:1px solid var(--hairline-strong); color:var(--text);
    border-radius:8px; padding:7px 9px; font-size:13px;}
  .count{margin-left:auto; color:var(--muted); font-size:12px; align-self:center;}
  table{width:100%; border-collapse:collapse;} thead th{position:sticky; top:0; background:var(--bg);
    text-align:left; font-size:11px; color:var(--faint); text-transform:uppercase; letter-spacing:.04em;
    padding:9px 8px; border-bottom:1px solid var(--hairline-strong); cursor:pointer; white-space:nowrap;}
  tbody td{padding:10px 8px; border-bottom:1px solid var(--hairline); vertical-align:top;}
  tbody tr:hover{background:var(--panel);}
  .num{text-align:right; font-variant-numeric:tabular-nums;}
  a.vname{color:var(--text); font-weight:600; text-decoration:none;} a.vname:hover{color:var(--accent-2);}
  .pending{color:var(--faint); font-size:11px;}
  .badge{display:inline-flex; align-items:center; font-size:10px; font-weight:800; padding:2px 8px; border-radius:6px; letter-spacing:.02em;}
  .ch-forum{background:#243b55; color:#cfe3ff;} .ch-publication{background:#2b2350; color:#d7ccff;}
  .ch-association_event{background:#12402a; color:#b6f0cf;} .ch-directory{background:#402a12; color:#f0d6b6;}
  .ch-unclassified{background:var(--panel-2); color:var(--faint);}
  .tier{background:var(--panel-2); color:var(--muted); border:1px solid var(--hairline-strong);}
  .tier-T1{color:var(--good); border-color:var(--good);} .tier-T2{color:var(--warn);}
  .src-manual-seed{background:var(--good); color:#08240f;} .src-llm{background:var(--accent); color:#fff;}
  .src-fallback{background:#6b7585; color:#e9edf3;}
  .pill{display:inline-block; min-width:34px; text-align:center; font-weight:800; padding:2px 8px; border-radius:999px; font-size:12px;}
  .pill.hi{background:var(--good); color:#08240f;} .pill.mid{background:var(--warn); color:#3a2c05;} .pill.lo{background:#3a4150; color:#c3ccd8;}
  .chip{display:inline-block; font-size:10px; background:var(--panel-2); border:1px solid var(--hairline);
    color:var(--muted); border-radius:5px; padding:1px 6px; margin:1px 2px 1px 0;}
  .detail{color:var(--muted); font-size:12px;} .off{opacity:.5;}
  footer{margin-top:30px; padding-top:16px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint); line-height:1.7;}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandbar">
    <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    <div class="sync" id="sync"></div>
  </div>
  <h1>Merchant Discovery</h1>
  <div class="sub">Outbound discovery of public venues to reach decision-makers at potential merchant businesses. Not tracking — marketing has no merchant activity data (that's the dev backend).</div>

  <div class="stats" id="stats"></div>

  <div class="note">
    <b>Fit</b> = 0.5 vertical+decision-maker relevance + 0.3 payments/outreach access + 0.2 audience; a fit of 0 means gated as off-topic (player/consumer or off-vertical).
    <b>Audience tier</b> (T1/T2/T3) is a <b>reputation / cross-listing tier, NOT real traffic</b> — exact traffic needs a paid API this project doesn't use.
    <b>Source</b>: <span class="badge src-manual-seed">manual-seed</span> = human-verified investigation; <span class="badge src-llm">llm</span> = Claude-judged; <span class="badge src-fallback">fallback</span> = provisional keyword guess (upgraded to llm once Anthropic credits are back).
    <b>LinkedIn communities are excluded and shown here as manual-only</b> — LinkedIn's ToS/API make community discovery non-automatable, so it's never scraped or faked.
  </div>

  <div class="filters">
    <div><label>Vertical</label><select id="fVert"><option value="all">All</option></select></div>
    <div><label>Channel type</label><select id="fChan"><option value="all">All</option>
      <option>forum</option><option>publication</option><option>association_event</option><option>directory</option><option>unclassified</option></select></div>
    <div><label>Outreach mode</label><select id="fOut"><option value="all">All</option>
      <option>post content</option><option>advertise</option><option>get listed</option><option>sponsor</option></select></div>
    <div><label>Source</label><select id="fSrc"><option value="all">All</option>
      <option>manual-seed</option><option>llm</option><option>fallback</option></select></div>
    <div><label>Min fit: <span id="minv">0</span></label><input type="range" id="fMin" min="0" max="10" step="0.5" value="0"></div>
    <label style="font-size:12px;color:var(--muted)"><input type="checkbox" id="fOn"> on-topic only</label>
    <span class="count" id="count"></span>
  </div>

  <table id="tbl"><thead><tr>
    <th data-k="venue">Venue</th><th data-k="channel_type">Channel</th><th data-k="vertical">Vertical</th>
    <th class="num" data-k="fit">Fit</th><th data-k="tier">Audience</th><th data-k="outreach">Outreach</th>
    <th data-k="judge_source">Source</th>
  </tr></thead><tbody id="body"></tbody></table>

  <footer id="foot"></footer>
</div>

<script>
const DATA = /*__DATA__*/;
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById("sync").textContent = "Generated: " + DATA.generated_at;

// stats
const st = [
  [DATA.total, "venues"], [DATA.on_topic, "on-topic"], [DATA.verified, "verified URL"],
  [DATA.by_source["manual-seed"]||0, "human-verified"], [DATA.by_source["llm"]||0, "llm-judged"],
];
document.getElementById("stats").innerHTML = st.map(([n,l])=>`<div class="stat"><b>${n}</b><span>${l}</span></div>`).join("");

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
      <td>${name}<div class="detail">${esc(v.reason)}</div></td>
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
