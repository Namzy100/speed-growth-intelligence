"""Build a self-contained creative-performance dashboard HTML from live Sheets data.

Pulls the Channel Overview / Campaign Installs / Retention / Last Updated tabs
from the Google Sheet at build time, computes the highlights, asks Claude
(claude-sonnet-4-6) for plain-English insights, and bakes everything into a
single self-contained file at docs/creative_dashboard.html.

Why a build script rather than client-side fetching: gspread is server-side and
credentials.json is a service-account private key — embedding it in a browser
file would leak it. So data is fetched live here and inlined into the output.
Only Chart.js loads from cdnjs (per spec); all other CSS/JS and the data are
inline, so the file opens directly in a browser.

Run from repo root:  python pipelines/build_creative_dashboard.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from pipelines import sheets  # reuse _open/_retry auth + retry

_OUT = _ROOT / "docs" / "creative_dashboard.html"
_INSIGHTS_MODEL = "claude-sonnet-4-6"
_MIN_MEANINGFUL_INSTALLS = 100   # floor for "meaningful volume" in efficiency pick
_RETENTION_EXCLUDE_RECENT_DAYS = 2


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _num(x) -> float:
    """Tolerant numeric parse: strips commas/whitespace, returns 0.0 on failure."""
    if x is None:
        return 0.0
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _records(ss, tab: str) -> list[dict]:
    ws = sheets._retry(lambda: ss.worksheet(tab))
    return sheets._retry(ws.get_all_records)


# ------------------------------------------------------------------
# Section builders
# ------------------------------------------------------------------

def build_channels(rows: list[dict]) -> dict:
    channels = []
    for r in rows:
        channels.append({
            "channel": str(r.get("channel", "")).strip(),
            "installs": int(_num(r.get("installs"))),
            "impressions": int(_num(r.get("impressions"))),
            "clicks": int(_num(r.get("clicks"))),
            "ecpi": round(_num(r.get("ecpi")), 4),
        })
    channels = [c for c in channels if c["channel"]]
    channels.sort(key=lambda c: c["installs"], reverse=True)

    # Most efficient = lowest eCPI among PAID channels (eCPI > 0) with meaningful
    # volume. eCPI 0 means organic/no spend, which isn't "efficient", so excluded.
    paid = [c for c in channels if c["ecpi"] > 0 and c["installs"] >= _MIN_MEANINGFUL_INSTALLS]
    most_efficient = min(paid, key=lambda c: c["ecpi"]) if paid else None

    by_name = {c["channel"].lower(): c for c in channels}
    fb, apple = by_name.get("facebook"), by_name.get("apple")
    facebook_flag = None
    if fb and apple:
        facebook_flag = {
            "facebook_ecpi": fb["ecpi"],
            "apple_ecpi": apple["ecpi"],
            "flagged": fb["ecpi"] > apple["ecpi"],
        }

    return {
        "channels": channels,
        "most_efficient": most_efficient["channel"] if most_efficient else None,
        "most_efficient_detail": most_efficient,
        "facebook_flag": facebook_flag,
        "min_meaningful_installs": _MIN_MEANINGFUL_INSTALLS,
    }


def build_campaigns(rows: list[dict]) -> dict:
    camps = []
    excluded_organic = 0
    for r in rows:
        net = str(r.get("campaign_network", "")).strip()
        if not net or net.lower() == "unknown":
            # 'unknown' is the unattributed Organic bucket, not an ad campaign.
            excluded_organic += 1
            continue
        camps.append({
            "campaign": net,
            "channel": str(r.get("channel", "")).strip(),
            "installs": int(_num(r.get("installs"))),
            "cost": round(_num(r.get("cost")), 2),
        })
    camps.sort(key=lambda c: c["installs"], reverse=True)
    return {"campaigns": camps[:10], "excluded_organic_rows": excluded_organic}


def build_retention(rows: list[dict]) -> dict:
    # Sort cohorts by date and drop the most recent N (immature).
    rows = [r for r in rows if str(r.get("day", "")).strip()]
    rows.sort(key=lambda r: str(r["day"]))
    matured = rows[:-_RETENTION_EXCLUDE_RECENT_DAYS] if len(rows) > _RETENTION_EXCLUDE_RECENT_DAYS else []
    excluded = [str(r["day"]) for r in rows[-_RETENTION_EXCLUDE_RECENT_DAYS:]] if rows else []

    labels = [f"D{i}" for i in range(1, 8)]
    cols = [f"retention_rate_d{i}" for i in range(1, 8)]
    values = []
    for col in cols:
        # Average only cohorts with observed (non-zero) data for that day —
        # a 0 here means the cohort hasn't reached that day yet.
        observed = [_num(r.get(col)) for r in matured if _num(r.get(col)) > 0]
        values.append(round(mean(observed), 4) if observed else 0.0)

    return {
        "labels": labels,
        "values": values,
        "cohort_count": len(matured),
        "excluded_days": excluded,
    }


# ------------------------------------------------------------------
# Claude insights
# ------------------------------------------------------------------

def generate_insights(channels: dict, campaigns: dict, retention: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"insights": [], "source": "unavailable (ANTHROPIC_API_KEY not set)"}

    # Compact, factual summary of the real data for the model to reason over.
    summary = {
        "channels_top": channels["channels"][:8],
        "most_efficient_channel": channels["most_efficient_detail"],
        "facebook_vs_apple_ecpi": channels["facebook_flag"],
        "top_campaigns": campaigns["campaigns"],
        "retention_curve_d1_d7": dict(zip(retention["labels"], retention["values"])),
        "retention_cohorts_used": retention["cohort_count"],
    }

    prompt = (
        "You are a performance-marketing analyst for Speed Wallet, a Bitcoin "
        "Lightning payments app. Below is REAL data pulled live from the user "
        "acquisition dashboard (installs, eCPI, top campaigns, and the D1-D7 "
        "retention curve from matured cohorts).\n\n"
        "Write 3-5 concise, plain-English insight bullets: what's working, "
        "what isn't, and what to do next. Be specific and cite the actual "
        "numbers (channel names, eCPI values, install counts, retention %). "
        "No fluff, no generic advice. Each bullet one or two sentences.\n\n"
        "Return ONLY a JSON array of strings, e.g. [\"...\", \"...\"]. No prose.\n\n"
        f"DATA:\n{json.dumps(summary, indent=2)}"
    )

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_INSIGHTS_MODEL,
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        insights = _parse_json_array(text)
        if insights:
            return {"insights": insights, "source": _INSIGHTS_MODEL}
        return {"insights": [], "source": f"{_INSIGHTS_MODEL} (unparseable response)"}
    except Exception as e:
        return {"insights": [], "source": f"error: {type(e).__name__}: {e}"}


def _parse_json_array(text: str) -> list[str]:
    try:
        val = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            val = json.loads(m.group())
        except json.JSONDecodeError:
            return []
    if isinstance(val, list):
        return [str(x) for x in val if str(x).strip()]
    return []


# ------------------------------------------------------------------
# Render
# ------------------------------------------------------------------

def render_html(data: dict) -> str:
    return _TEMPLATE.replace("/*__DATA__*/", json.dumps(data))


def main() -> None:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        sys.exit("GOOGLE_SHEETS_ID must be set in .env")

    print("Opening Google Sheet (live)...")
    ss = sheets._open(sid)

    print("Reading tabs: Channel Overview, Campaign Installs, Retention, Last Updated...")
    channels = build_channels(_records(ss, "Channel Overview"))
    campaigns = build_campaigns(_records(ss, "Campaign Installs"))
    retention = build_retention(_records(ss, "Retention"))

    last_updated = ""
    lu = _records(ss, "Last Updated")
    if lu:
        last_updated = str(list(lu[0].values())[0])

    print(f"Generating insights with {_INSIGHTS_MODEL}...")
    insights = generate_insights(channels, campaigns, retention)
    print(f"  insights source: {insights['source']}  ({len(insights['insights'])} bullets)")

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "last_updated": last_updated,
        **channels,
        **campaigns,
        "retention": retention,
        "insights": insights["insights"],
        "insights_source": insights["source"],
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render_html(data), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  channels={len(data['channels'])} campaigns={len(data['campaigns'])} "
          f"retention_cohorts={retention['cohort_count']}")


# ------------------------------------------------------------------
# HTML template — data injected at /*__DATA__*/. Chart.js from cdnjs (per spec);
# everything else inline so the file opens directly in a browser.
# ------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Creative Performance Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1c2330; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --good:#3fb950;
    --warn:#d29922; --bad:#f85149; --teal:#2dd4bf;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.5; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1140px; margin:0 auto; padding:32px 20px 64px;}
  header{display:flex; flex-wrap:wrap; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:28px; border-bottom:1px solid var(--border); padding-bottom:20px;}
  h1{font-size:24px; margin:0 0 4px; letter-spacing:-0.02em;}
  .sub{color:var(--muted); font-size:13px;}
  .stamp{text-align:right; font-size:12px; color:var(--muted);}
  .stamp b{color:var(--text); font-weight:600;}
  section{margin:30px 0;}
  .sec-head{display:flex; align-items:baseline; gap:10px; margin-bottom:14px;}
  h2{font-size:15px; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin:0; font-weight:600;}
  .note{font-size:12px; color:var(--muted);}
  .panel{background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:18px;}
  .grid-2{display:grid; grid-template-columns:1fr 1fr; gap:20px;}
  @media(max-width:820px){.grid-2{grid-template-columns:1fr;}}
  table{width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums;}
  th,td{text-align:right; padding:9px 12px; border-bottom:1px solid var(--border); font-size:13.5px;}
  th{color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.05em;}
  th:first-child,td:first-child{text-align:left;}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr.eff{background:rgba(63,185,80,0.08);}
  td.ch{font-weight:600;}
  .badge{display:inline-block; font-size:10.5px; font-weight:700; padding:2px 7px; border-radius:20px; margin-left:8px; vertical-align:middle; letter-spacing:0.03em;}
  .badge.good{background:rgba(63,185,80,0.16); color:var(--good);}
  .badge.bad{background:rgba(248,81,73,0.16); color:var(--bad);}
  .num-good{color:var(--good); font-weight:600;}
  .num-bad{color:var(--bad); font-weight:600;}
  .muted{color:var(--muted);}
  .chart-box{position:relative; height:300px;}
  ul.insights{list-style:none; margin:0; padding:0;}
  ul.insights li{position:relative; padding:10px 0 10px 26px; border-bottom:1px solid var(--border); font-size:14px;}
  ul.insights li:last-child{border-bottom:none;}
  ul.insights li::before{content:"▸"; position:absolute; left:4px; color:var(--accent);}
  .empty{display:flex; flex-direction:column; align-items:center; justify-content:center; padding:42px 20px; text-align:center; border:1px dashed var(--border); border-radius:10px; background:var(--panel-2);}
  .empty .ico{font-size:30px; margin-bottom:10px; opacity:0.7;}
  .empty .msg{color:var(--muted); font-size:14px; max-width:440px;}
  .src{font-size:11px; color:var(--muted); margin-top:10px;}
  .fallback{color:var(--warn); font-size:13px; padding:20px; text-align:center;}
  footer{margin-top:40px; padding-top:16px; border-top:1px solid var(--border); font-size:11.5px; color:var(--muted);}
  code{background:var(--panel-2); padding:1px 5px; border-radius:4px; font-size:12px;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Creative Performance Dashboard</h1>
      <div class="sub">Speed Wallet · User acquisition &amp; retention</div>
    </div>
    <div class="stamp">
      Data last synced: <b id="lastUpdated">—</b><br>
      Dashboard built: <b id="builtAt">—</b>
    </div>
  </header>

  <!-- 1. Channel Performance -->
  <section>
    <div class="sec-head"><h2>Channel Performance</h2><span class="note" id="chNote"></span></div>
    <div class="panel"><table id="chTable">
      <thead><tr><th>Channel</th><th>Installs</th><th>eCPI</th><th>Impressions</th><th>Clicks</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </section>

  <div class="grid-2">
    <!-- 2. Campaign Breakdown -->
    <section>
      <div class="sec-head"><h2>Campaign Breakdown</h2><span class="note" id="cmpNote"></span></div>
      <div class="panel"><div class="chart-box"><canvas id="cmpChart"></canvas></div></div>
    </section>

    <!-- 3. Retention Curve -->
    <section>
      <div class="sec-head"><h2>Retention Curve</h2><span class="note" id="retNote"></span></div>
      <div class="panel"><div class="chart-box"><canvas id="retChart"></canvas></div></div>
    </section>
  </div>

  <!-- 4. Key Insights -->
  <section>
    <div class="sec-head"><h2>Key Insights</h2></div>
    <div class="panel">
      <ul class="insights" id="insights"></ul>
      <div class="src" id="insightsSrc"></div>
    </div>
  </section>

  <!-- 5. Meta Creative Analysis placeholder -->
  <section>
    <div class="sec-head"><h2>Meta Creative Analysis</h2></div>
    <div class="empty">
      <div class="ico">📊</div>
      <div class="msg">Pending Meta ad account access — will populate automatically once connected.</div>
    </div>
  </section>

  <footer>
    Data pulled live from Google Sheets at build time and inlined into this file.
    Charts via Chart.js (cdnjs). Insights generated by <code id="modelName">—</code>.
  </footer>
</div>

<script>
const DATA = /*__DATA__*/;

const intFmt = new Intl.NumberFormat("en-US");
const fmtInt = n => intFmt.format(Math.round(n||0));
const fmtEcpi = v => (v && v > 0) ? "$" + Number(v).toFixed(2) : "—";
const fmtCost = v => (v && v > 0) ? "$" + intFmt.format(Math.round(v)) : "—";
const fmtPct = v => (v*100).toFixed(1) + "%";

function renderChannels(){
  const tb = document.querySelector("#chTable tbody");
  const eff = DATA.most_efficient;
  const fb = DATA.facebook_flag;
  DATA.channels.forEach(c => {
    const tr = document.createElement("tr");
    if (c.channel === eff) tr.className = "eff";
    let badge = "";
    if (c.channel === eff) badge = '<span class="badge good">★ Most efficient</span>';
    if (fb && fb.flagged && c.channel.toLowerCase() === "facebook")
      badge += '<span class="badge bad">⚠ eCPI above Apple</span>';
    const ecpiCls = (c.channel === eff) ? "num-good" : ((fb && fb.flagged && c.channel.toLowerCase()==="facebook") ? "num-bad" : "");
    tr.innerHTML =
      `<td class="ch">${c.channel}${badge}</td>` +
      `<td>${fmtInt(c.installs)}</td>` +
      `<td class="${ecpiCls}">${fmtEcpi(c.ecpi)}</td>` +
      `<td class="muted">${fmtInt(c.impressions)}</td>` +
      `<td class="muted">${fmtInt(c.clicks)}</td>`;
    tb.appendChild(tr);
  });
  const parts = [];
  if (DATA.most_efficient_detail)
    parts.push(`Most efficient: ${eff} ($${DATA.most_efficient_detail.ecpi.toFixed(2)} eCPI, ${fmtInt(DATA.most_efficient_detail.installs)} installs, ≥${DATA.min_meaningful_installs} vol)`);
  if (fb && fb.flagged)
    parts.push(`Facebook eCPI $${fb.facebook_ecpi.toFixed(2)} > Apple $${fb.apple_ecpi.toFixed(2)}`);
  document.getElementById("chNote").textContent = parts.join("  ·  ");
}

function renderCampaigns(){
  const note = document.getElementById("cmpNote");
  note.textContent = `Top ${DATA.campaigns.length} by installs (excl. ${DATA.excluded_organic_rows} unattributed Organic)`;
  if (typeof Chart === "undefined"){ chartFallback("cmpChart"); return; }
  const labels = DATA.campaigns.map(c => c.campaign);
  const installs = DATA.campaigns.map(c => c.installs);
  new Chart(document.getElementById("cmpChart"), {
    type:"bar",
    data:{labels, datasets:[{
      label:"Installs", data:installs,
      backgroundColor:"#58a6ff", borderRadius:4, maxBarThickness:26,
    }]},
    options:{
      indexAxis:"y", responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{afterLabel:(ctx)=>{
          const c = DATA.campaigns[ctx.dataIndex];
          return `Channel: ${c.channel}\nCost: ${fmtCost(c.cost)}`;
        }}},
      },
      scales:{
        x:{ticks:{color:"#8b949e"}, grid:{color:"#21262d"}},
        y:{ticks:{color:"#e6edf3", font:{size:11}}, grid:{display:false}},
      },
    },
  });
}

function renderRetention(){
  const r = DATA.retention;
  document.getElementById("retNote").textContent =
    `Avg of ${r.cohort_count} matured cohorts (excl. ${r.excluded_days.length} most-recent days)`;
  if (typeof Chart === "undefined"){ chartFallback("retChart"); return; }
  new Chart(document.getElementById("retChart"), {
    type:"line",
    data:{labels:r.labels, datasets:[{
      label:"Retention", data:r.values.map(v=>+(v*100).toFixed(2)),
      borderColor:"#2dd4bf", backgroundColor:"rgba(45,212,191,0.12)",
      fill:true, tension:0.3, pointRadius:4, pointBackgroundColor:"#2dd4bf",
    }]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{label:(ctx)=>` ${ctx.parsed.y.toFixed(1)}% retained`}},
      },
      scales:{
        x:{ticks:{color:"#e6edf3"}, grid:{color:"#21262d"}},
        y:{ticks:{color:"#8b949e", callback:v=>v+"%"}, grid:{color:"#21262d"}, beginAtZero:true},
      },
    },
  });
}

function renderInsights(){
  const ul = document.getElementById("insights");
  if (!DATA.insights || !DATA.insights.length){
    ul.innerHTML = '<li class="muted">Insights unavailable for this build.</li>';
  } else {
    DATA.insights.forEach(t => {
      const li = document.createElement("li"); li.textContent = t; ul.appendChild(li);
    });
  }
  document.getElementById("insightsSrc").textContent = "Generated by " + DATA.insights_source;
  document.getElementById("modelName").textContent = DATA.insights_source;
}

function chartFallback(id){
  const cv = document.getElementById(id);
  const d = document.createElement("div");
  d.className = "fallback";
  d.textContent = "Chart.js failed to load (offline?). Data is present; reconnect to render charts.";
  cv.replaceWith(d);
}

function init(){
  try{
    document.getElementById("lastUpdated").textContent = DATA.last_updated || "—";
    document.getElementById("builtAt").textContent = DATA.generated_at || "—";
    renderChannels();
    renderCampaigns();
    renderRetention();
    renderInsights();
    console.log("Dashboard rendered OK:",
      DATA.channels.length, "channels,",
      DATA.campaigns.length, "campaigns,",
      DATA.retention.cohort_count, "retention cohorts");
  }catch(e){
    console.error("Dashboard render error:", e);
  }
}
document.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
