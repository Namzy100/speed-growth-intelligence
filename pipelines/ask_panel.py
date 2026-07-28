"""Shared "Ask this dashboard" panel — CSS, HTML and a configurable JS engine.

Every dashboard already bakes its own data into the page. This module turns that
data into a question box that answers from it, computed entirely in the browser.

WHY IT COMPUTES LOCALLY INSTEAD OF CALLING A MODEL. GitHub Pages is static-only and
Namzy100/speed-growth-intelligence is a PUBLIC repo, so there is nowhere to put an
API key: a runtime model call from these pages would either ship a secret in client
JS or need a backend that does not exist. GitHub-Actions-as-compute does not rescue
it either — a static page cannot trigger a workflow without an authenticated token,
which is the same secret problem. So answers are derived from the rows the page
already carries: the numbers are real and checkable against the tables beside them,
nothing leaves the browser, and it ships with zero infrastructure. `answerQuestion`
is the single seam — when a backend exists, swap that one function.

WHY ONE SHARED ENGINE. The five dashboards hold genuinely different data (channel
performance, venue prospecting, a content kanban, a strategy doc, creator scores),
so the QUESTION SETS differ per dashboard — but the machinery underneath is the
same: pick a collection, filter it, then count / rank / average / break down / look
up one row. Five inline copies would drift. Each dashboard supplies a config
describing its own collections, facets, metrics and scalars; the engine is identical.

CONFIG SHAPE (per dashboard, passed to `js()`):

    {
      "noun": "channels",                  # what a bare row is called
      "collections": [
        {"name": "channels",               # id used in answers
         "words": ["channel", "network"],  # picks this collection from the question
         "rows": <list[dict]>,             # the actual data
         "label": "name",                  # field holding the human name
         "facets":  [{"key": "...", "words": [...], "values": [...]}],
         "metrics": [{"key": "...", "words": [...], "label": "...",
                      "fmt": "int|money|pct|float", "lower_is_better": bool}],
         "detail": ["field", ...]},        # fields shown in a single-row lookup
      ],
      "scalars": [{"words": [...], "label": "...", "value": 123, "fmt": "int"}],
      "series":  [{"words": [...], "label": "...", "labels": [...], "values": [...],
                   "fmt": "pct"}],
      "examples": ["...", "..."],          # the clickable chips
    }

Anything the engine cannot answer from the data gets an explicit refusal — a
dashboard that invents a number is worse than one that declines.
"""

import json

from pipelines import list_collapse
from pipelines.json_embed import dumps_for_script

__all__ = ["css", "html_section", "js", "inject"]


def inject(html: str, config: dict, heading: str = "Ask this dashboard",
           note: str | None = None) -> str:
    """Splice the panel into a finished dashboard at three uniform anchors.

    Done by post-processing the rendered HTML rather than by editing each
    dashboard's own placeholder scheme: the five builders use different marker
    conventions (`/*__DATA__*/`, `/*__STATE_JSON__*/`, `/*__TACTICS__*/`, …) and
    only these three anchors are common to all of them. The engine is appended as
    its own <script> just before </body> — strategy has no script block at all, and
    a self-contained block avoids assuming anything about the existing one's order.
    """
    if "askQ" in html:                       # already injected — idempotent
        return html
    for anchor in ("</style>", "<footer", "</body>"):
        if anchor not in html:
            raise ValueError(f"ask_panel.inject: anchor {anchor!r} not found")
    # The long-list collapse ships with the panel deliberately: it exists so the
    # panel is reachable without scrolling a multi-thousand-pixel table, and both
    # want the same single injection point rather than five more builder edits.
    html = html.replace("</style>", css() + list_collapse.css() + "</style>", 1)
    html = html.replace("<footer", html_section(heading, note) + "\n  <footer", 1)
    return html.replace("</body>",
                        "<script>\n" + js(config) + "\n</script>\n"
                        "<script>\n" + list_collapse.js() + "\n</script>\n</body>", 1)


def css() -> str:
    """Panel styles. Uses each dashboard's existing custom properties."""
    return """
  /* Ask panel (shared — pipelines/ask_panel.py) */
  .ask-wrap{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md,10px); padding:16px 18px; box-shadow:var(--shadow,none);}
  .ask-row{display:flex; gap:10px; align-items:center;}
  .ask-row input[type=text]{flex:1; min-width:0; padding:10px 13px; font-size:14px; background:#0e1117;
    color:var(--text); border:1px solid var(--hairline); border-radius:8px; font-family:inherit;}
  .ask-row input[type=text]:focus{outline:none; border-color:var(--accent);}
  .ask-row button{background:var(--accent); color:#fff; border:1px solid var(--accent); border-radius:8px;
    padding:10px 20px; font-size:13.5px; font-weight:650; cursor:pointer; font-family:inherit; transition:filter .15s;}
  .ask-row button:hover{filter:brightness(1.12);}
  .ask-eg{margin-top:11px; font-size:11.5px; color:var(--faint); display:flex; flex-wrap:wrap; gap:7px; align-items:center;}
  .ask-chip{background:rgba(47,93,251,0.10); border:1px solid var(--hairline-strong,var(--hairline)); color:var(--accent-2,var(--accent));
    padding:3px 10px; border-radius:999px; cursor:pointer; transition:border-color .15s;}
  .ask-chip:hover{border-color:var(--accent);}
  .ask-answer{margin-top:14px; padding-top:14px; border-top:1px solid var(--hairline); font-size:13.5px; line-height:1.55;}
  .ask-answer.ask-empty{color:var(--faint);}
  .ask-answer b{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums;}
  .ask-answer .ask-head{font-size:15px; font-weight:650; color:var(--text); margin-bottom:8px;}
  .ask-answer .ask-miss{color:var(--warn,#e3b341);}
  .ask-list{margin:9px 0 0; padding-left:0; list-style:none;}
  .ask-list li{display:flex; justify-content:space-between; gap:14px; padding:5px 0; border-bottom:1px solid var(--hairline);}
  .ask-list li:last-child{border-bottom:none;}
  .ask-list .ask-meta{color:var(--muted,var(--faint)); font-size:12.5px; font-variant-numeric:tabular-nums; white-space:nowrap;}
  .ask-prov{margin-top:10px; font-size:11px; color:var(--faint);}
"""


def html_section(heading: str = "Ask this dashboard", note: str | None = None) -> str:
    """The panel markup. Examples/chips are injected by the JS from the config."""
    note = note or "answers computed from the data on this page — nothing leaves your browser"
    return f"""  <section>
    <div class="sec-head"><h2>{heading}</h2><span class="note">{note}</span></div>
    <div class="ask-wrap">
      <div class="ask-row">
        <input type="text" id="askQ" autocomplete="off" placeholder="ask a question about this data…">
        <button id="askGo">Ask</button>
      </div>
      <div class="ask-eg" id="askEg">Try:</div>
      <div id="askA" class="ask-answer ask-empty"></div>
    </div>
  </section>
"""


def js(config: dict) -> str:
    """The engine plus this dashboard's config, as one <script>-safe JS blob."""
    return _ENGINE.replace("/*__ASK_CONFIG__*/", dumps_for_script(config))


_ENGINE = r"""
/* ==========================================================================
   "Ask this dashboard" — shared engine (pipelines/ask_panel.py).
   Deliberately NOT a runtime model call: these pages are static and public, so
   there is nowhere to hold an API key. Every answer is computed from the rows
   already embedded above, which is why the numbers are checkable against the
   tables on this page. `answerQuestion` is the seam to swap for a real backend.
   ========================================================================== */
const ASK = /*__ASK_CONFIG__*/;
const askInt = new Intl.NumberFormat("en-US");
const askEsc = s => { const d = document.createElement("div"); d.textContent = (s == null ? "" : s); return d.innerHTML; };

function askNum(v){
  if (typeof v === "number") return v;
  if (v == null) return null;
  const n = parseFloat(String(v).replace(/[$,%\s,]/g, ""));
  return isNaN(n) ? null : n;
}
function askFmt(v, fmt){
  if (v == null) return "—";
  if (fmt === "money") return "$" + (Math.abs(v) >= 100 ? askInt.format(Math.round(v)) : v.toFixed(2));
  if (fmt === "pct")   return (Math.round(v * 10) / 10) + "%";
  if (fmt === "int")   return askInt.format(Math.round(v));
  return String(Math.round(v * 10) / 10);
}
// Matches a whole word, allowing a short inflection (install -> installs,
// tactic -> tactics). The suffix allowance is withheld from words shorter than 4
// characters: creator country codes are two letters, and "TH" was matching "the",
// which made "what is the meaning of life" look like a question about Thailand.
// Short values must match exactly.
const askHas = (q, w) => {
  const lit = String(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tail = String(w).length >= 4 ? "[a-z]{0,3}" : "";
  return new RegExp("(^|[^a-z0-9])" + lit + tail + "([^a-z0-9]|$)", "i").test(q);
};

/* ---- parse ------------------------------------------------------------- */
function askParse(raw){
  const q = " " + raw.toLowerCase().trim() + " ";
  const f = {filters: [], intent: null, n: null, metric: null, asc: false, group: null};

  // Which collection? First whose words appear; else the default (first).
  const matched = (ASK.collections || []).find(c => (c.words || []).some(w => askHas(q, w)));
  f.collMatched = !!matched;
  f.coll = matched || (ASK.collections || [])[0];

  // A scalar KPI question ("what is total installs") short-circuits everything.
  f.scalar = (ASK.scalars || []).find(s => (s.words || []).some(w => askHas(q, w))) || null;
  f.scalarPhrase = !!(f.scalar && (f.scalar.words || []).some(w => w.includes(" ") && askHas(q, w)));
  f.series = (ASK.series  || []).find(s => (s.words || []).some(w => askHas(q, w))) || null;

  if (f.coll){
    for (const fac of (f.coll.facets || [])){
      let mapped = false;
      for (const m of (fac.map || [])){
        if ((m.words || []).some(w => askHas(q, w))){
          f.filters.push({key: fac.key, val: m.val, label: m.label || (m.words || [])[0]});
          mapped = true; break;
        }
      }
      if (mapped) continue;
      for (const val of (fac.values || [])){
        if (askHas(q, String(val).toLowerCase())){ f.filters.push({key: fac.key, val: val}); break; }
      }
      for (const w of (fac.words || [])){
        const m = q.match(new RegExp(w + "\\s+(?:is\\s+|=\\s*)?([a-z0-9_\\- ]{2,24})", "i"));
        if (m && !f.filters.some(x => x.key === fac.key)){
          const cand = (fac.values || []).find(v => m[1].trim().startsWith(String(v).toLowerCase()));
          if (cand) f.filters.push({key: fac.key, val: cand});
        }
      }
    }
    // Metric: explicit words win; otherwise the collection's first metric.
    for (const m of (f.coll.metrics || [])){
      if ((m.words || []).some(w => askHas(q, w))){ f.metric = m; break; }
    }
    // Numeric threshold ("over 1000 installs", "under $2 ecpi")
    const over = q.match(/(?:over|above|more than|at least|>)\s*\$?([\d.,]+)\s*([km])?/);
    const under = q.match(/(?:under|below|less than|cheaper than|<)\s*\$?([\d.,]+)\s*([km])?/);
    for (const [m, dir] of [[over, "gte"], [under, "lte"]]){
      if (!m) continue;
      let v = parseFloat(m[1].replace(/,/g, ""));
      if (m[2] === "k") v *= 1e3; if (m[2] === "m") v *= 1e6;
      f.threshold = {dir, v, metric: f.metric || (f.coll.metrics || [])[0]};
      f.filters.push({label: (dir === "gte" ? "over " : "under ") + askFmt(v, (f.threshold.metric || {}).fmt)});
    }
  }

  const nm = q.match(/\b(?:top|best|bottom|worst|first)\s+(\d+)/);
  if (nm) f.n = Math.min(parseInt(nm[1], 10), 50);

  if (/\bhow many\b|\bhow much\b|\bcount\b|\bnumber of\b/.test(q)) f.intent = "count";
  else if (/\baverage\b|\bavg\b|\bmean\b|\btypical\b/.test(q)) f.intent = "average";
  else if (/\btotal\b|\bsum\b|\bcombined\b|\ball together\b/.test(q)) f.intent = "sum";
  else if (/\bbreak ?down\b|\bdistribution\b|\bsplit\b/.test(q)) f.intent = "breakdown";
  else if (/\bcheapest\b|\blowest\b|\bworst\b|\bbottom\b|\bleast\b|\bsmallest\b/.test(q)) { f.intent = "top"; f.want = "low"; }
  else if (/\btop\b|\bbest\b|\bhighest\b|\bmost\b|\bbiggest\b|\blargest\b|\bstrongest\b/.test(q)) { f.intent = "top"; f.want = "high"; }
  // LITERAL direction words name a direction on the metric itself rather than a
  // quality judgement, and must not be inverted by lower_is_better: "most expensive"
  // means the highest eCPI even though a low eCPI is the good outcome.
  if (/\bexpensive\b|\bpriciest\b|\bdearest\b|\bcostliest\b/.test(q)) { f.intent = "top"; f.literal = "high"; }
  else if (/\bcheapest\b|\bcheap\b/.test(q)) { f.intent = "top"; f.literal = "low"; }
  else if (/\blist\b|\bshow me\b|\bwhich\b|\bwho\b|\bwhat are\b/.test(q)) f.weakList = true;

  // Direction. A literal word wins outright. Otherwise a quality word is read
  // against the metric's polarity: "best eCPI" is the lowest, "worst eCPI" the
  // highest, while "top installs" is simply the highest.
  const lib = !!(f.metric || (f.coll && (f.coll.metrics || [])[0]) || {}).lower_is_better;
  f.asc = f.literal ? (f.literal === "low") : ((f.want === "low") !== lib);
  f.lib = lib;

  // A weak interrogative only becomes a listing when the question is demonstrably
  // about this dashboard's data; otherwise it falls through to the refusal.
  if (f.weakList && !f.intent && (f.collMatched || f.filters.length || f.metric)) f.intent = "top";

  if (f.coll){
    const g = q.match(/\bby\s+([a-z_ ]+)/);
    if (g){
      const want = g[1].trim();
      // A metric named in "by X" is a ranking instruction, never a grouping one.
      const isMetric = (f.coll.metrics || []).some(m => [m.key].concat(m.words || [])
        .some(w => want.startsWith(String(w).toLowerCase())));
      const fac = isMetric ? null : (f.coll.facets || []).find(x => [x.key].concat(x.words || [])
        .some(w => { const ww = String(w).toLowerCase(); return want === ww || want.startsWith(ww + " ") || ww.startsWith(want); }));
      if (fac){ f.group = fac.key; if (!f.intent) f.intent = "breakdown"; }
      // "by <metric>" is a ranking instruction, not a grouping one — leave the
      // intent alone so "cheapest channel by eCPI" stays a ranking.
    }
  }
  return f;
}

function askApply(f){
  const rows = (f.coll && f.coll.rows) || [];
  return rows.filter(r => {
    for (const flt of f.filters){
      if (flt.key && String(r[flt.key]) !== String(flt.val)) return false;
    }
    if (f.threshold){
      const mk = (f.threshold.metric || {}).key;
      const v = askNum(r[mk]);
      if (v == null) return false;
      if (f.threshold.dir === "gte" && !(v >= f.threshold.v)) return false;
      if (f.threshold.dir === "lte" && !(v <= f.threshold.v)) return false;
    }
    return true;
  });
}

const askScope = f => {
  const parts = f.filters.map(x => x.label || x.val);
  return parts.length ? parts.join(" · ") : ("all " + ((f.coll && f.coll.name) || ASK.noun || "rows"));
};
const askProv = n => `<div class="ask-prov">Computed in-browser from ${askInt.format(n)} row${n === 1 ? "" : "s"} embedded in this page`
  + (ASK.generated_at ? ` · data baked ${askEsc(ASK.generated_at)}` : "")
  + ` · cross-check any number against the tables on this page.</div>`;

function askRowLine(coll, r, metric){
  const name = askEsc(r[coll.label] != null ? r[coll.label] : "(unnamed)");
  const bits = [];
  if (metric) bits.push(`${askEsc(metric.label)} ${askFmt(askNum(r[metric.key]), metric.fmt)}`);
  // Metric-less collection (e.g. strategy tactics): fall back to the first detail
  // field after the label so the row still says something useful.
  if (!metric){
    const k = (coll.detail || []).find(x => x !== coll.label && r[x] != null && r[x] !== "");
    if (k) return `<li><span>${name}</span><span class="ask-meta">${askEsc(String(r[k]).slice(0, 70))}</span></li>`;
  }
  for (const m of (coll.metrics || [])){
    if (metric && m.key === metric.key) continue;
    const v = askNum(r[m.key]);
    if (v != null && bits.length < 3) bits.push(`${askEsc(m.label)} ${askFmt(v, m.fmt)}`);
  }
  return `<li><span>${name}</span><span class="ask-meta">${bits.join(" · ")}</span></li>`;
}

/* ---- answer ----------------------------------------------------------- */
function answerQuestion(raw){
  if (!raw.trim()) return `<span class="ask-empty">Type a question.</span>`;
  const f = askParse(raw);

  // Single scalar KPI. A multi-word trigger ("total installs", "d1 retention") is
  // specific enough to win even though the phrasing also looks like an aggregation.
  if (f.scalar && (!f.intent || f.scalarPhrase)) {
    return `<div class="ask-head">${askEsc(f.scalar.label)}: <b>${askFmt(askNum(f.scalar.value), f.scalar.fmt)}</b></div>`
      + (f.scalar.note ? askEsc(f.scalar.note) : "") + askProv(1);
  }
  // A named series (retention curve etc).
  if (f.series){
    const rows = (f.series.labels || []).map((l, i) =>
      `<li><span>${askEsc(l)}</span><span class="ask-meta">${askFmt(askNum((f.series.values || [])[i]), f.series.fmt)}</span></li>`).join("");
    return `<div class="ask-head">${askEsc(f.series.label)}</div><ul class="ask-list">${rows}</ul>`
      + (f.series.note ? `<div class="ask-prov">${askEsc(f.series.note)}</div>` : "")
      + askProv((f.series.labels || []).length);
  }
  if (!f.coll) return askRefuse();

  // A named row beats a generic intent ("tell me about Money20/20").
  const ql = raw.toLowerCase();
  let named = null;
  const order = f.collMatched
    ? [f.coll].concat((ASK.collections || []).filter(c => c !== f.coll))
    : (ASK.collections || []);
  for (const c of order){
    for (const r of (c.rows || [])){
      const nm = String(r[c.label] || "").toLowerCase();
      if (nm.length > 3 && ql.includes(nm) && (!named || nm.length > String(named.r[named.c.label]).length)) named = {c, r};
    }
  }
  if (named && !f.intent){
    const c = named.c, r = named.r;
    const lines = (c.detail || Object.keys(r)).filter(k => r[k] != null && String(r[k]) !== "")
      .map(k => `${askEsc(k.replace(/_/g, " "))}: <b>${askEsc(typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k])}</b>`);
    return `<div class="ask-head">${askEsc(r[c.label])}</div>${lines.join(" · ")}` + askProv(1);
  }

  const rows = askApply(f);
  const metric = f.metric || (f.coll.metrics || [])[0];

  if (f.intent === "count"){
    const ordered = metric
      ? rows.slice().sort((a, b) => (askNum(b[metric.key]) || 0) - (askNum(a[metric.key]) || 0))
      : rows.slice();
    return `<div class="ask-head"><b>${askInt.format(rows.length)}</b> ${askEsc(f.coll.name)} match ${askEsc(askScope(f))}.</div>`
      + (rows.length ? `<ul class="ask-list">${ordered
          .slice(0, 5).map(r => askRowLine(f.coll, r, metric)).join("")}</ul>`
        + (rows.length > 5 ? `<div class="ask-prov">Showing 5 of ${askInt.format(rows.length)}.</div>` : "") : "")
      + askProv(rows.length);
  }

  if (f.intent === "sum" || f.intent === "average"){
    if (!metric) return `<span class="ask-miss">There is no numeric measure on ${askEsc(f.coll.name)} to total or average.</span>`;
    const vals = rows.map(r => askNum(r[metric.key]))
      .filter(v => v != null && !(metric.exclude_zero && v === 0));
    if (!vals.length) return `<span class="ask-miss">No ${askEsc(f.coll.name)} match ${askEsc(askScope(f))}, so there is nothing to total.</span>`;
    const total = vals.reduce((a, b) => a + b, 0);
    if (f.intent === "sum"){
      return `<div class="ask-head">Total ${askEsc(metric.label)} for ${askEsc(askScope(f))}: <b>${askFmt(total, metric.fmt)}</b></div>`
        + `Across <b>${askInt.format(vals.length)}</b> ${askEsc(f.coll.name)}.` + askProv(rows.length);
    }
    const sorted = vals.slice().sort((a, b) => a - b);
    const med = sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
    return `<div class="ask-head">Average ${askEsc(metric.label)} for ${askEsc(askScope(f))}: <b>${askFmt(total / vals.length, metric.fmt)}</b></div>`
      + `Median <b>${askFmt(med, metric.fmt)}</b> · min <b>${askFmt(sorted[0], metric.fmt)}</b> · max <b>${askFmt(sorted[sorted.length - 1], metric.fmt)}</b>`
      + ` · across <b>${askInt.format(vals.length)}</b> ${askEsc(f.coll.name)}.` + askProv(rows.length);
  }

  if (f.intent === "breakdown"){
    const key = f.group || ((f.coll.facets || [])[0] || {}).key;
    if (!key) return askRefuse();
    const counts = {};
    rows.forEach(r => { const k = r[key] == null || r[key] === "" ? "unknown" : r[key]; counts[k] = (counts[k] || 0) + 1; });
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return `<span class="ask-miss">No ${askEsc(f.coll.name)} match ${askEsc(askScope(f))}.</span>`;
    return `<div class="ask-head">${askInt.format(rows.length)} ${askEsc(f.coll.name)} by ${askEsc(key.replace(/_/g, " "))}`
      + (f.filters.length ? ` (${askEsc(askScope(f))})` : "") + `</div><ul class="ask-list">`
      + entries.map(([k, v]) => `<li><span>${askEsc(k)}</span><span class="ask-meta">${askInt.format(v)} · ${(v / rows.length * 100).toFixed(1)}%</span></li>`).join("")
      + `</ul>` + askProv(rows.length);
  }

  if (f.intent === "top"){
    if (!rows.length) return `<span class="ask-miss">Nothing matches ${askEsc(askScope(f))}. Try loosening the question.</span>`;
    const n = f.n || 10;
    let ranked = rows, dropped = 0;
    if (metric && metric.exclude_zero){
      ranked = rows.filter(r => { const v = askNum(r[metric.key]); return v != null && v !== 0; });
      dropped = rows.length - ranked.length;
      if (!ranked.length) return `<span class="ask-miss">Every matching row has no ${askEsc(metric.label)}, so there is nothing to rank.</span>`;
    }
    const sorted = metric ? ranked.slice().sort((a, b) => {
      const av = askNum(a[metric.key]), bv = askNum(b[metric.key]);
      if (av == null) return 1; if (bv == null) return -1;
      return f.asc ? av - bv : bv - av;
    }) : rows.slice();
    // Header wording, kept honest three ways: a metric-less collection is "listed"
    // not "ranked"; ascending on a lower-is-better metric is "Best", not "Lowest"
    // (rank 1 and the cheapest eCPI are the good end, and "Lowest by rank" read as
    // if it were the bottom of the list); and when nothing is truncated it says
    // "All N" rather than the nonsense "Lowest 6 of 6".
    const showing = Math.min(n, sorted.length);
    const complete = showing >= ranked.length;
    const word = metric ? (f.asc ? (f.lib ? "Best" : "Lowest") : "Top") : "Listing";
    const lead = complete ? `All ${askInt.format(ranked.length)}`
                          : `${word} ${showing} of ${askInt.format(ranked.length)}`;
    const order = metric ? ` by ${askEsc(metric.label)}, ${f.asc ? "best" : "highest"} first` : "";
    return `<div class="ask-head">${lead} ${askEsc(f.coll.name)}${order}`
      + `${f.filters.length ? " — " + askEsc(askScope(f)) : ""}</div>`
      + `<ul class="ask-list">${sorted.slice(0, n).map(r => askRowLine(f.coll, r, metric)).join("")}</ul>`
      + (dropped ? `<div class="ask-prov">${askInt.format(dropped)} row(s) with no ${askEsc(metric.label)} excluded from this ranking (e.g. organic, which has no media cost).</div>` : "")
      + askProv(rows.length);
  }

  // Filters but no recognised intent: show what matches rather than guess.
  if (f.filters.length){
    return `<div class="ask-head">I did not recognise the question, so here is what matches ${askEsc(askScope(f))}.</div>`
      + `<b>${askInt.format(rows.length)}</b> ${askEsc(f.coll.name)}.`
      + `<ul class="ask-list">${rows.slice(0, 10).map(r => askRowLine(f.coll, r, metric)).join("")}</ul>` + askProv(rows.length);
  }
  return askRefuse();
}

function askRefuse(){
  const cols = (ASK.collections || []).map(c => askEsc(c.name)).join(", ");
  return `<span class="ask-miss">I can't answer that from the data on this page.</span><br>`
    + `This panel computes answers from what this dashboard actually carries (${cols}), so it handles counts, `
    + `totals, averages, rankings, breakdowns, and single-row lookups. It does not do open-ended questions. `
    + `Try one of the examples above.`;
}

/* ---- wiring ----------------------------------------------------------- */
function askRun(q){
  const el = document.getElementById("askA");
  el.classList.remove("ask-empty");
  el.innerHTML = answerQuestion(q);
}
(function askInitPanel(){
  const eg = document.getElementById("askEg");
  (ASK.examples || []).forEach(x => {
    const s = document.createElement("span");
    s.className = "ask-chip"; s.textContent = x;
    s.addEventListener("click", () => { document.getElementById("askQ").value = x; askRun(x); });
    eg.appendChild(s);
  });
  document.getElementById("askGo").addEventListener("click", () => askRun(document.getElementById("askQ").value));
  document.getElementById("askQ").addEventListener("keydown", e => { if (e.key === "Enter") askRun(e.target.value); });
  const total = (ASK.collections || []).reduce((a, c) => a + ((c.rows || []).length), 0);
  document.getElementById("askA").textContent =
    `Ask a question about the ${askInt.format(total)} rows on this page.`;
})();
"""
