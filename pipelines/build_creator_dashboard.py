"""Build a self-contained creator-intelligence dashboard from live Supabase data.

Reads every creator from Supabase at build time and bakes the data into a single
self-contained HTML file (docs/creator_dashboard.html) — same pattern as
build_creative_dashboard.py. The page filters client-side by segment, platform,
and minimum score, colour-codes composite scores, and flags any creator whose
niche_tags reference a competitor brand.

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
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(_TEMPLATE.replace("/*__DATA__*/", json.dumps(data)), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  total={data['total']} platforms={data['platforms']} flagged={data['flagged']}")


# ------------------------------------------------------------------
# Self-contained HTML template — data injected at /*__DATA__*/.
# ------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Creator Intelligence Dashboard</title>
<style>
  :root{
    --bg:#0a0c11; --surface:rgba(255,255,255,0.03); --hairline:rgba(255,255,255,0.10);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#a371f7; --good:#3fb950; --warn:#e3b341; --bad:#f85149;
    --r:12px;
  }
  *{box-sizing:border-box}
  body{margin:0; background:var(--bg); color:var(--text); line-height:1.5;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1280px; margin:0 auto; padding:28px 22px 80px;}
  h1{font-size:24px; margin:0 0 4px; font-weight:750; letter-spacing:-0.02em;}
  .sub{color:var(--muted); font-size:13px; margin-bottom:22px;}
  .panel{background:var(--surface); border:1px solid var(--hairline); border-radius:var(--r); padding:18px 20px; margin-bottom:20px;}
  .panel h2{font-size:12px; text-transform:uppercase; letter-spacing:0.09em; color:var(--muted); margin:0 0 12px; font-weight:700;}
  .segs{display:grid; grid-template-columns:repeat(3,1fr); gap:14px;}
  @media(max-width:760px){.segs{grid-template-columns:1fr;}}
  .seg{border:1px solid var(--hairline); border-radius:10px; padding:12px 14px;}
  .seg b{color:var(--accent);}
  .seg .hook{color:var(--muted); font-size:12.5px;}
  .crit{font-size:12.5px; color:var(--muted); margin-top:6px;}
  .crit code{background:rgba(255,255,255,0.06); padding:1px 6px; border-radius:5px; color:#cdbdf3;}

  .filters{display:flex; flex-wrap:wrap; gap:18px; align-items:flex-end;}
  .filters label{display:block; font-size:10.5px; text-transform:uppercase; letter-spacing:0.08em; color:var(--faint); margin-bottom:5px; font-weight:700;}
  select,input[type=text]{background:#11141b; color:var(--text); border:1px solid var(--hairline); border-radius:8px; padding:7px 10px; font-size:13px; min-width:150px;}
  input[type=range]{vertical-align:middle; width:160px; accent-color:var(--accent);}
  .rangeval{color:var(--text); font-weight:650; font-variant-numeric:tabular-nums;}
  .count{margin-left:auto; color:var(--muted); font-size:13px;}

  .table-wrap{overflow-x:auto; border:1px solid var(--hairline); border-radius:var(--r);}
  table{width:100%; border-collapse:collapse; font-size:13px; min-width:900px;}
  th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--hairline); white-space:nowrap;}
  th{position:sticky; top:0; background:#0e1117; color:var(--faint); font-size:10px; text-transform:uppercase; letter-spacing:0.06em; cursor:pointer; user-select:none;}
  th.num,td.num{text-align:right; font-variant-numeric:tabular-nums;}
  tbody tr:hover{background:rgba(255,255,255,0.03);}
  .pill{display:inline-block; font-size:11px; padding:2px 9px; border-radius:20px; background:rgba(255,255,255,0.07); color:var(--muted);}
  .score{font-weight:700; font-variant-numeric:tabular-nums;}
  .s-good{color:var(--good);} .s-warn{color:var(--warn);} .s-bad{color:var(--bad);}
  .tags{color:var(--muted); font-size:12px; white-space:normal; max-width:280px;}
  .tag{display:inline-block; background:rgba(255,255,255,0.05); border-radius:5px; padding:1px 6px; margin:1px 3px 1px 0; font-size:11px;}
  .flag{color:var(--bad); font-weight:700;}
  .flag-no{color:var(--faint);}
  .out{font-size:11.5px;}
  footer{margin-top:26px; color:var(--faint); font-size:11.5px;}
</style>
</head>
<body>
<div class="wrap">
  <h1>Creator Intelligence Dashboard</h1>
  <div class="sub">Speed Wallet partner scouting · <span id="total"></span> creators · synced <span id="gen"></span></div>

  <div class="panel">
    <h2>Target Segments</h2>
    <div class="segs">
      <div class="seg"><b>Remittance</b> — diaspora senders. <span class="hook">Hook: zero fees on cross-border sends.</span></div>
      <div class="seg"><b>iGaming</b> — online gambling/betting. <span class="hook">Hook: instant deposits & withdrawals.</span></div>
      <div class="seg"><b>Crypto-curious</b> — mainstream Bitcoin-curious. <span class="hook">Hook: simplicity & real-world utility.</span></div>
    </div>
    <div class="crit">Each creator is scored across five dimensions, each out of 20, for a composite out of 100:
      <code>audience fit</code> <code>engagement quality</code> <code>content alignment</code>
      <code>acquisition potential</code> <code>deposit relevance</code>.
      Composite colour: <span class="s-good">green &gt;50</span>, <span class="s-warn">yellow 30–50</span>, <span class="s-bad">red &lt;30</span>.
      A red brand flag means the creator's niche tags reference a competitor/brand.</div>
  </div>

  <div class="panel">
    <h2>Filters</h2>
    <div class="filters">
      <div><label>Segment</label>
        <select id="fSeg">
          <option value="all">All segments</option>
          <option value="remittance">remittance</option>
          <option value="iGaming">iGaming</option>
          <option value="crypto-curious">crypto-curious</option>
          <option value="general">general</option>
        </select></div>
      <div><label>Platform</label>
        <select id="fPlat">
          <option value="all">All platforms</option>
          <option value="YouTube">YouTube</option>
          <option value="TikTok">TikTok</option>
        </select></div>
      <div><label>Min composite score: <span class="rangeval" id="minVal">0</span></label>
        <input type="range" id="fMin" min="0" max="100" value="0" step="1"></div>
      <div><label>Search name</label>
        <input type="text" id="fSearch" placeholder="name contains…"></div>
      <div class="count" id="count"></div>
    </div>
  </div>

  <div class="table-wrap">
    <table id="tbl">
      <thead><tr>
        <th data-k="name">Creator</th>
        <th data-k="platform">Platform</th>
        <th class="num" data-k="followers">Followers</th>
        <th data-k="segment">Segment</th>
        <th class="num" data-k="score">Score /100</th>
        <th class="num" data-k="drs">Deposit Rel. /20</th>
        <th data-k="outreach">Outreach</th>
        <th data-k="brand_flag">Brand?</th>
        <th>Niche Tags</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <footer>Data baked from Supabase at build time. Rebuilt by pipelines/build_creator_dashboard.py.</footer>
</div>

<script>
const DATA = /*__DATA__*/;
const intFmt = new Intl.NumberFormat("en-US");
const esc = s => { const d=document.createElement("div"); d.textContent=(s==null?"":s); return d.innerHTML; };

document.getElementById("total").textContent = DATA.total;
document.getElementById("gen").textContent = DATA.generated_at;

let sortKey = "score", sortDir = -1;

function scoreClass(v){ return v > 50 ? "s-good" : (v >= 30 ? "s-warn" : "s-bad"); }

function currentRows(){
  const seg = document.getElementById("fSeg").value;
  const plat = document.getElementById("fPlat").value;
  const min = +document.getElementById("fMin").value;
  const q = document.getElementById("fSearch").value.trim().toLowerCase();
  let rows = DATA.creators.filter(c =>
    (seg === "all" || c.segment === seg) &&
    (plat === "all" || c.platform === plat) &&
    (c.score >= min) &&
    (!q || c.name.toLowerCase().includes(q))
  );
  rows.sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if (typeof x === "string"){ x=x.toLowerCase(); y=y.toLowerCase(); }
    return (x<y?-1:x>y?1:0) * sortDir;
  });
  return rows;
}

function render(){
  const rows = currentRows();
  const tb = document.querySelector("#tbl tbody");
  tb.innerHTML = rows.map(c => {
    const tags = c.tags.map(t => `<span class="tag">${esc(t)}</span>`).join(" ") || "<span class='flag-no'>—</span>";
    const flag = c.brand_flag ? "<span class='flag'>⚑ brand</span>" : "<span class='flag-no'>—</span>";
    return `<tr>
      <td>${esc(c.name)}</td>
      <td><span class="pill">${esc(c.platform)}</span></td>
      <td class="num">${intFmt.format(c.followers)}</td>
      <td>${esc(c.segment)}</td>
      <td class="num score ${scoreClass(c.score)}">${c.score.toFixed(1)}</td>
      <td class="num">${c.drs.toFixed(1)}</td>
      <td class="out">${esc(c.outreach)}</td>
      <td>${flag}</td>
      <td class="tags">${tags}</td>
    </tr>`;
  }).join("");
  document.getElementById("count").textContent =
    `${rows.length} of ${DATA.total} shown` + (DATA.flagged ? ` · ${DATA.flagged} brand-flagged` : "");
}

document.getElementById("fMin").addEventListener("input", e => {
  document.getElementById("minVal").textContent = e.target.value; render();
});
["fSeg","fPlat","fSearch"].forEach(id =>
  document.getElementById(id).addEventListener("input", render));
document.querySelectorAll("#tbl th[data-k]").forEach(th =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = (k==="name"||k==="segment"||k==="platform"||k==="outreach")?1:-1; }
    render();
  }));

render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
