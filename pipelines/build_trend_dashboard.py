"""Build the Trend Intelligence dashboard (docs/trend_dashboard.html) — US market.

A STATEFUL predict -> ship -> measure tool, not a disposable weekly report.

Data (unchanged): intelligence.trend_pipeline.collect_signals() pulls US YouTube +
TikTok + Instagram; Claude enriches the top hooks and writes an organic content
calendar + paid ad briefs.

What's new — the dashboard now persists across regenerations via a small state file
(docs/dashboard_state.json) that this script reads and merges on every rebuild:

  * Every organic post / paid brief carries a STATUS
    (suggested -> briefed -> in_production -> posted -> results_in), preserved
    across rebuilds and editable in the browser (click a status tag to cycle).
  * RESULTS logging (manual for now — see note) records actual views / ER / saves
    next to the benchmarked estimate, flagging >20% beats or misses.
  * A "This Week's Actions" block headlines what needs a decision / is awaiting
    results. The full trend breakdown becomes supporting evidence beneath it.
  * Top hooks are VERSIONED: tagged new / rising / falling / stable vs last week,
    with last week's hooks kept in a collapsed "previous week" section.
  * Full beat-by-beat scripts collapse behind an expand toggle.

Browser edits persist to localStorage immediately; click "Download state" to save
an updated dashboard_state.json and commit it so the next rebuild bakes it in.

NOTE (results automation): results logging is MANUAL entry for now. Once we settle
on which platform APIs to use (Adjust for installs, Meta Ads Manager for paid,
manual TikTok/IG insights pulls for organic), wire an importer that fills each
item's `results` block instead of hand entry.

Run from repo root:  python pipelines/build_trend_dashboard.py
"""

import html
import json
import os
import re
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
_STATE_FILE = _ROOT / "docs" / "dashboard_state.json"   # alongside the HTML, committed
_CACHE = _ROOT / "data" / "processed" / "trend_raw_cache.json"
_CACHE_MAX_AGE_H = 3
_MODEL = "claude-sonnet-4-6"
_BENCHMARK_CPI = 3.17
_SEGMENTS = ["remittance", "crypto-curious", "iGaming"]
_SEG_LABEL = {"remittance": "Remittance", "crypto-curious": "Crypto-Curious", "iGaming": "iGaming"}
_CANDIDATES = 24
_TOP_HOOKS = 10

# Recommendation lifecycle. Order matters — the UI cycles through it.
_STATUS_FLOW = ["suggested", "briefed", "in_production", "posted", "results_in"]
_STATUS_LABEL = {"suggested": "Suggested", "briefed": "Briefed",
                 "in_production": "In Production", "posted": "Posted",
                 "results_in": "Results In"}
_STATE_SCHEMA = 1


# ------------------------------------------------------------------
# Raw-scrape cache (unchanged)
# ------------------------------------------------------------------

def _load_cache() -> dict | None:
    if "--fresh" in sys.argv or not _CACHE.exists():
        return None
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(data["generated_at"])).total_seconds() / 3600
        if age_h <= _CACHE_MAX_AGE_H:
            print(f"Using cached raw scrape ({age_h:.1f}h old; pass --fresh to refetch).")
            return data
    except Exception:
        return None
    return None


def _save_cache(data: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_n(n) -> str:
    n = int(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:48] or "item"


def _item_id(kind: str, hook: str) -> str:
    return f"{kind}-{_slug(hook)}"


def _parse_est(s) -> int | None:
    """Pull a numeric estimate (e.g. '~50k reach', '120,000') to an int, else None."""
    if not s:
        return None
    m = re.search(r"([\d][\d.,]*)\s*([kKmM]?)", str(s))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2).lower()
    return int(num * (1_000_000 if unit == "m" else 1_000 if unit == "k" else 1))


def _hook_key(hook: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(hook or "").lower())[:40]


# ------------------------------------------------------------------
# State (persisted across rebuilds)
# ------------------------------------------------------------------

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"schema": _STATE_SCHEMA, "items": {}, "hook_history": {}}


def _recs_from_enrich(enrich: dict) -> list[dict]:
    """Flatten Claude's organic + paid output into rec dicts with a stable payload."""
    recs = []
    for c in enrich.get("organic_calendar", [])[:5]:
        recs.append({
            "type": "organic", "hook": c.get("hook", ""),
            "segment": c.get("segment", ""), "estimate": c.get("est_reach", ""),
            "payload": {"platform": c.get("platform", ""), "outline": c.get("outline", ""),
                        "production_notes": c.get("production_notes", "")},
        })
    for b in enrich.get("paid_briefs", [])[:3]:
        hook = b.get("hook", "")
        recs.append({
            "type": "paid", "hook": hook,
            "segment": trend_pipeline.classify_segment(hook),
            "estimate": b.get("est_cpi", ""),
            "payload": {"problem": b.get("problem", ""), "solution": b.get("solution", ""),
                        "cta": b.get("cta", ""), "audience": b.get("audience", "")},
        })
    return recs


def merge_state(prev: dict, recs: list[dict], week: str) -> dict:
    """Merge this week's fresh recs into the persisted item map.

    - Re-seen recs keep their status/results/first_seen (matched by stable id).
    - New recs enter as 'suggested'.
    - In-flight items from prior weeks (status != suggested) are carried forward
      even if not re-suggested, so nothing you've acted on disappears.
    - Stale 'suggested' items that weren't re-suggested this week are pruned.
    """
    prev_items = prev.get("items", {})
    items = {}
    for rec in recs:
        iid = _item_id(rec["type"], rec["hook"])
        old = prev_items.get(iid, {})
        items[iid] = {
            "id": iid, "type": rec["type"], "hook": rec["hook"], "segment": rec["segment"],
            "estimate": rec["estimate"],
            "estimate_num": _parse_est(rec["estimate"]) if rec["type"] == "organic" else None,
            "payload": rec["payload"],
            "status": old.get("status", "suggested"),
            "results": old.get("results", {"views": None, "er": None, "saves": None}),
            # Preserve auto-import provenance + the Meta match key across rebuilds.
            "results_source": old.get("results_source"),
            "ad_ref": old.get("ad_ref"),
            # Real live URL of the published post, pasted by hand when the card is
            # moved to "posted" (Tier 0 of posted-content tracking — automatic
            # brief->post detection isn't reliable, so we capture the real link).
            "posted_url": old.get("posted_url"),
            "first_seen": old.get("first_seen", week), "last_seen": week,
        }
    for iid, old in prev_items.items():
        if iid not in items and old.get("status", "suggested") != "suggested":
            old["carried"] = True  # in-flight from a prior week
            items[iid] = old
    return items


def hook_trajectory(top: list[dict], prev_hist: dict) -> tuple[dict, list[dict]]:
    """Tag current hooks new/rising/falling/stable vs last week; return (tags, prev_hooks)."""
    prev_hooks = prev_hist.get("hooks", [])
    prev_by_key = {h["key"]: h for h in prev_hooks}
    tags = {}
    for v in top:
        k = _hook_key(v["hook"])
        old = prev_by_key.get(k)
        if not old:
            tags[k] = "new"
        else:
            oe = old.get("er", 0) or 0
            if oe and v["er"] > oe * 1.1:
                tags[k] = "rising"
            elif oe and v["er"] < oe * 0.9:
                tags[k] = "falling"
            else:
                tags[k] = "stable"
    return tags, prev_hooks


# ------------------------------------------------------------------
# Claude enrichment (unchanged)
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
    seg_digest = [f"{s}: {len(data['by_segment'][s]['organic'])} organic-track, "
                  f"{len(data['by_segment'][s]['paid'])} paid-track items" for s in _SEGMENTS]
    prompt = (
        "You are Speed Wallet's growth-creative strategist. Speed is a Bitcoin Lightning "
        "payments app. Segments: remittance (zero-fee cross-border sends), crypto-curious "
        "(dead-simple first Bitcoin use), iGaming (instant deposits/withdrawals). Best paid "
        f"CPI benchmark: ${_BENCHMARK_CPI:.2f}.\n\n"
        "Below are candidate US trending hooks (YouTube + TikTok + Instagram), ranked by "
        "engagement. Keyword matching lets some IRRELEVANT viral novelty through (ASMR, memes, "
        "unrelated 'money' trends). Produce a single JSON object:\n\n"
        "1. hooks: SELECT the up-to-10 candidates a fintech/crypto/remittance/iGaming brand "
        "could actually learn from, DROP irrelevant novelty even if high engagement. Ranked "
        "best-first, each enriched: index (int), format_type (talking-head / text-on-screen / "
        "screen-record / reaction / animation / ugc), replication_score (int 1-10), "
        "production_cost ('$0 (phone video)' / '$50-200 (basic edit)' / '$200+ (produced)'), "
        "why_it_works (one line: fear of loss / social proof / curiosity gap / identity signal).\n"
        "2. organic_calendar: exactly 5 phone-filmable pieces to post THIS WEEK. Each: hook, "
        "outline (3-4 beats, <=60s), platform (TikTok/Instagram Reels), segment, est_reach "
        "(number based on the ER benchmarks), production_notes.\n"
        "3. paid_briefs: exactly 3 ad concepts. Each: hook (0-3s), problem (4-8s), solution "
        f"(9-15s), cta (final 3s), audience (Meta/TikTok), est_cpi (range vs ${_BENCHMARK_CPI:.2f}).\n\n"
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

def _status_tag(item: dict) -> str:
    st = item["status"]
    return (f'<button class="st st-{st}" data-act="cycle" data-id="{_e(item["id"])}" '
            f'title="Click to advance status">{_STATUS_LABEL.get(st, st)}</button>')


def _results_block(item: dict) -> str:
    r = item.get("results") or {}
    def val(k):
        return "" if r.get(k) in (None, "") else _e(r.get(k))
    # Manual inputs are always available (unchanged). An auto-import readout +
    # source badge appear on top when the importer has filled real numbers.
    posted_url = item.get("posted_url") or ""
    return f"""
        <div class="results" data-role="results">
          <div class="posted-url">
            <label>Posted URL <input type="url" data-f="posted_url" value="{_e(posted_url)}"
              placeholder="Paste the live post link (marks this as really shipped)"></label>
            <a class="posted-link" data-role="postedlink" target="_blank" rel="noopener"></a>
          </div>
          <div class="res-auto" data-role="autoread"></div>
          <div class="res-inputs">
            <label>Views <input type="number" data-f="views" value="{val('views')}" placeholder="—"></label>
            <label>ER % <input type="number" step="0.1" data-f="er" value="{val('er')}" placeholder="—"></label>
            <label>Saves <input type="number" data-f="saves" value="{val('saves')}" placeholder="—"></label>
          </div>
          <div class="res-compare" data-role="compare"></div>
        </div>"""


def render_actions(items: dict) -> str:
    buckets = {"decide": [], "production": [], "posted": [], "done": []}
    for it in items.values():
        st = it["status"]
        if st in ("suggested", "briefed"):
            buckets["decide"].append(it)
        elif st == "in_production":
            buckets["production"].append(it)
        elif st == "posted":
            buckets["posted"].append(it)
        elif st == "results_in":
            buckets["done"].append(it)

    def col(title, key, hint):
        rows = "".join(
            f'<li><span class="a-hook">“{_e(it["hook"][:64])}”</span>'
            f'<span class="st st-{it["status"]}">{_STATUS_LABEL[it["status"]]}</span></li>'
            for it in buckets[key][:6]
        ) or '<li class="a-empty">Nothing here yet.</li>'
        return (f'<div class="a-col"><div class="a-title">{title} '
                f'<span class="a-count">{len(buckets[key])}</span></div>'
                f'<div class="a-hint">{hint}</div><ul>{rows}</ul></div>')

    return ('<div class="actions-grid">'
            + col("Needs a decision", "decide", "Suggested / briefed — approve or brief out")
            + col("In production", "production", "Being made now")
            + col("Posted · awaiting results", "posted", "Live — log performance when it's in")
            + col("Results in", "done", "Measured vs estimate")
            + "</div>")


def _traj_tag(tag: str) -> str:
    if not tag:
        return ""
    return f'<span class="traj traj-{tag}">{tag}</span>'


def render_hooks(candidates: list[dict], enrich: dict, tags: dict) -> str:
    selected = [h for h in enrich.get("hooks", [])
                if isinstance(h.get("index"), int) and 0 <= h["index"] < len(candidates)]
    if not selected:
        selected = [{"index": i} for i in range(min(_CANDIDATES, len(candidates)))]
    rows, rank = [], 0
    for e in selected:
        v = candidates[e["index"]]
        if not trend_pipeline._is_english(v["hook"]):
            continue
        rank += 1
        if rank > _TOP_HOOKS:
            break
        plat_cls = {"YouTube": "yt", "TikTok": "tt", "Instagram": "ig"}.get(v["platform"], "tt")
        rows.append(f"""
      <div class="hook-card">
        <div class="hook-rank">{rank}</div>
        <div class="hook-main">
          <a class="hook-text" href="{_e(v['url'])}" target="_blank" rel="noopener">“{_e(v['hook'])}”</a>
          <div class="hook-meta">
            <span class="badge {plat_cls}">{_e(v['platform'])}</span>
            <span class="badge seg {v['segment'].replace('-','')}">{_e(_SEG_LABEL.get(v['segment'], v['segment']))}</span>
            {_traj_tag(tags.get(_hook_key(v['hook'])))}
            <span class="m">{_fmt_n(v['views'])} views</span>
            <span class="m er">{v['er']:.1%} ER</span>
            <span class="m fmt">{_e(e.get('format_type','—'))}</span>
          </div>
          <div class="hook-why">{_e(e.get('why_it_works',''))}</div>
        </div>
        <div class="hook-repl"><div class="repl-lab">Replication</div>{_repl_dots(e.get('replication_score'))}</div>
      </div>""")
    return "".join(rows) or '<div class="empty">No qualifying hooks this week.</div>'


def _repl_dots(score) -> str:
    try:
        n = max(0, min(10, int(score)))
    except (TypeError, ValueError):
        n = 0
    return f'<span class="repl"><span class="repl-fill" style="width:{n*10}%"></span></span><span class="repl-n">{n}/10</span>'


def render_calendar(items: dict) -> str:
    organic = [it for it in items.values() if it["type"] == "organic"]
    organic.sort(key=lambda it: (it.get("carried", False), it["hook"]))
    cards = []
    for it in organic:
        p = it.get("payload", {})
        carried = ' <span class="carried">carried over</span>' if it.get("carried") else ""
        cards.append(f"""
      <div class="rec-card" data-id="{_e(it['id'])}" data-est="{it.get('estimate_num') or ''}">
        <div class="rec-top">
          <span class="badge seg {str(it.get('segment','')).replace('-','')}">{_e(it.get('segment'))}</span>
          <span class="badge plat">{_e(p.get('platform'))}</span>
          {_status_tag(it)}<span class="src-badge" data-role="source"></span>{carried}
        </div>
        <div class="rec-hook">“{_e(it['hook'])}”</div>
        <div class="rec-line"><span class="rec-est">Est. reach ~{_e(it.get('estimate'))}</span></div>
        <button class="expand" data-act="expand">▾ script &amp; notes</button>
        <div class="rec-detail" hidden>
          <div class="rec-beats">{_e(p.get('outline'))}</div>
          <div class="rec-notes">🎬 {_e(p.get('production_notes'))}</div>
        </div>
        {_results_block(it)}
      </div>""")
    return "".join(cards) or '<div class="empty">No organic items tracked.</div>'


def render_paid(items: dict) -> str:
    paid = [it for it in items.values() if it["type"] == "paid"]
    paid.sort(key=lambda it: (it.get("carried", False), it["hook"]))
    cards = []
    for it in paid:
        p = it.get("payload", {})
        carried = ' <span class="carried">carried over</span>' if it.get("carried") else ""
        cards.append(f"""
      <div class="rec-card" data-id="{_e(it['id'])}" data-est="">
        <div class="rec-top">
          <span class="badge seg {str(it.get('segment','')).replace('-','')}">{_e(it.get('segment'))}</span>
          {_status_tag(it)}<span class="src-badge" data-role="source"></span>{carried}
        </div>
        <div class="rec-hook">“{_e(it['hook'])}”</div>
        <div class="rec-line"><span class="rec-est cpi">Est. CPI {_e(it.get('estimate'))}</span></div>
        <label class="adref">Meta ad / campaign name (for auto-import)
          <input type="text" data-f="ad_ref" value="{_e(it.get('ad_ref') or '')}" placeholder="e.g. Payday - Android - Broad+"></label>
        <button class="expand" data-act="expand">▾ full 15s script</button>
        <div class="rec-detail" hidden>
          <div class="paid-row"><span class="k">Problem · 4-8s</span> {_e(p.get('problem'))}</div>
          <div class="paid-row"><span class="k">Speed · 9-15s</span> {_e(p.get('solution'))}</div>
          <div class="paid-row"><span class="k">CTA · final 3s</span> {_e(p.get('cta'))}</div>
          <div class="paid-row"><span class="k">Audience</span> {_e(p.get('audience'))}</div>
        </div>
        {_results_block(it)}
      </div>""")
    return "".join(cards) or '<div class="empty">No paid briefs tracked.</div>'


def render_prev(prev_hooks: list[dict], prev_week: str) -> str:
    if not prev_hooks:
        return '<div class="empty">No prior week on record yet — trajectory tags start next rebuild.</div>'
    rows = "".join(
        f'<li>“{_e(h.get("hook","")[:80])}” <span class="m">{_fmt_n(h.get("views",0))} views · {(h.get("er",0) or 0):.1%} ER</span></li>'
        for h in prev_hooks[:10])
    return f'<div class="prev-week"><div class="prev-lab">Week of {_e(prev_week)}</div><ul>{rows}</ul></div>'


def render_died(data: dict) -> str:
    died = data.get("died", [])
    if not died:
        return ('<div class="died-empty">No week-over-week decline data yet — baseline week.</div>')
    return '<ul class="died-list">' + "".join(f"<li>{_e(d)}</li>" for d in died[:8]) + "</ul>"


def render_signal(data: dict) -> str:
    sig = data.get("platform_signal", {})
    rows = []
    for seg in _SEGMENTS:
        s = sig.get(seg, {})
        yt, tt, ig = s.get("youtube_er", 0), s.get("tiktok_er", 0), s.get("instagram_er", 0)
        mx = max(yt, tt, ig, 0.0001)
        ranked = {"YouTube": yt, "TikTok": tt, "Instagram": ig}
        top = max(ranked, key=ranked.get)
        winner = f"{top} stronger" if ranked[top] > 0 else "no data"
        rows.append(f"""
      <div class="sig-row">
        <div class="sig-seg">{_e(_SEG_LABEL[seg])}</div>
        <div class="sig-bars">
          <div class="sig-bar"><span class="sig-lab">YT</span><div class="sig-track"><div class="sig-fill yt" style="width:{yt/mx*100:.0f}%"></div></div><span class="sig-val">{yt:.1%}</span></div>
          <div class="sig-bar"><span class="sig-lab">TT</span><div class="sig-track"><div class="sig-fill tt" style="width:{tt/mx*100:.0f}%"></div></div><span class="sig-val">{tt:.1%}</span></div>
          <div class="sig-bar"><span class="sig-lab">IG</span><div class="sig-track"><div class="sig-fill ig" style="width:{ig/mx*100:.0f}%"></div></div><span class="sig-val">{ig:.1%}</span></div>
        </div>
        <div class="sig-win">{winner}</div>
      </div>""")
    return "".join(rows)


def render(data: dict, enrich: dict, candidates: list[dict], state: dict,
           tags: dict, prev_hooks: list[dict], prev_week: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fs = data.get("filter_stats", {})
    counts = (f"{len(data['youtube'])} YouTube · {len(data['tiktok'])} TikTok · "
              f"{len(data.get('instagram', []))} Instagram (US + English)")
    # Bake the FULL state so the browser can download a complete, valid state file.
    state_js = json.dumps(state)
    repl = {
        "/*__SYNC__*/": now, "/*__COUNTS__*/": counts,
        "/*__ACTIONS__*/": render_actions(state["items"]),
        "/*__CALENDAR__*/": render_calendar(state["items"]),
        "/*__PAID__*/": render_paid(state["items"]),
        "/*__HOOKS__*/": render_hooks(candidates, enrich, tags),
        "/*__PREV__*/": render_prev(prev_hooks, prev_week),
        "/*__DIED__*/": render_died(data),
        "/*__SIGNAL__*/": render_signal(data),
        "/*__STATE_JSON__*/": state_js,
    }
    out = _TEMPLATE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    week = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = _load_cache()
    if data is None:
        print("Collecting fresh US trend signals (YouTube + TikTok + Instagram)...")
        data = trend_pipeline.collect_signals()
        _save_cache(data)
    candidates = data["top_hooks"][:_CANDIDATES]
    print(f"Selecting + enriching hooks from {len(candidates)} candidates (Claude)...")
    enrich = generate_ai(data, candidates)

    prev_state = _load_state()
    items = merge_state(prev_state, _recs_from_enrich(enrich), week)

    # Hook versioning: tag vs last week, capture last week's hooks for display.
    top_display = candidates[:_TOP_HOOKS]
    tags, prev_hooks = hook_trajectory(top_display, prev_state.get("hook_history", {}))
    prev_week = prev_state.get("hook_history", {}).get("week", "—")

    state = {
        "schema": _STATE_SCHEMA, "updated_at": week, "items": items,
        # rotate hook history: this week's hooks become next week's "previous".
        "hook_history": {"week": week, "hooks": [
            {"key": _hook_key(v["hook"]), "hook": v["hook"], "views": v["views"], "er": v["er"]}
            for v in top_display]},
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render(data, enrich, candidates, state, tags, prev_hooks, prev_week),
                    encoding="utf-8")
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    counts = {s: sum(1 for it in items.values() if it["status"] == s) for s in _STATUS_FLOW}
    print(f"Wrote {_OUT.relative_to(_ROOT)} + {_STATE_FILE.relative_to(_ROOT)}")
    print(f"  tracked items: {len(items)} · status breakdown: {counts}")


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
    --accent:#2f5dfb; --accent-2:#6f9dff;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149; --gold:#f5c400; --blue:#2f5dfb;
    --yt:#ff4d4d; --tt:#25f4ee; --ig:#dd2a7b;
    --grad:linear-gradient(120deg,#2f5dfb,#6f9dff);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.7);
    --r-lg:16px; --r-md:12px; --r-sm:9px;
    --seg-remittance:#3fb950; --seg-cryptocurious:#6f9dff; --seg-iGaming:#e3b341;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  body{color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    letter-spacing:-0.005em; line-height:1.5; -webkit-font-smoothing:antialiased; min-height:100vh;
    background:radial-gradient(1100px 600px at 50% -10%, rgba(47,93,251,0.20), transparent 58%),
      radial-gradient(820px 520px at 100% 0%, rgba(111,157,255,0.09), transparent 52%), var(--bg);
    background-attachment:fixed;}
  .wrap{max-width:1180px; margin:0 auto; padding:0 24px 90px;}
  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand{font-weight:760; font-size:16px; display:flex; align-items:center; gap:8px;}
  .brand .bolt{background:linear-gradient(180deg,#f5c400,#f0a02a); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .sync{font-size:12px; color:var(--muted);} .sync b{color:var(--text);}
  .title-block{margin:34px 0 22px;}
  h1{font-size:30px; font-weight:790; letter-spacing:-0.03em; background:linear-gradient(180deg,#fff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .title-block .sub{color:var(--muted); font-size:13.5px; margin-top:5px;}
  section{margin:34px 0;}
  .sec-head{display:flex; align-items:baseline; gap:11px; margin-bottom:16px; flex-wrap:wrap;}
  h2{font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted); font-weight:700; display:flex; align-items:center; gap:10px;}
  h2::before{content:""; width:3px; height:13px; border-radius:2px; background:var(--grad);}
  .sec-note{font-size:12px; color:var(--faint);}
  details.support > summary{cursor:pointer; list-style:none; color:var(--muted); font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; font-weight:700; padding:8px 0; display:flex; align-items:center; gap:8px;}
  details.support > summary::-webkit-details-marker{display:none;}
  details.support > summary::before{content:"▸"; color:var(--faint);} details.support[open] > summary::before{content:"▾";}
  .badge{display:inline-flex; align-items:center; font-size:10px; font-weight:800; padding:2px 8px; border-radius:6px; letter-spacing:0.03em;}
  .badge.yt{background:var(--yt); color:#fff;} .badge.tt{background:var(--tt); color:#04201f;}
  .badge.ig{background:linear-gradient(120deg,#f58529,#dd2a7b,#8134af); color:#fff;}
  .badge.plat{background:var(--panel-2); color:var(--muted);}
  .badge.seg{color:#0d1117;} .badge.seg.remittance{background:var(--seg-remittance);} .badge.seg.cryptocurious{background:var(--seg-cryptocurious);} .badge.seg.iGaming{background:var(--seg-iGaming);}
  .empty{color:var(--faint); font-size:13px; padding:10px 0;}

  /* status tag */
  .st{border:none; cursor:pointer; font-size:9.5px; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; padding:3px 9px; border-radius:20px; color:#0d1117;}
  .st-suggested{background:#6b7585; color:#e9edf3;} .st-briefed{background:var(--accent-2);}
  .st-in_production{background:var(--warn);} .st-posted{background:var(--blue); color:#fff;}
  .st-results_in{background:var(--good);}

  /* This week's actions */
  .actions{background:linear-gradient(180deg,rgba(47,93,251,0.10),rgba(22,27,34,0.4)); border:1px solid var(--hairline-strong); border-radius:var(--r-lg); padding:20px;}
  .actions-grid{display:grid; grid-template-columns:repeat(4,1fr); gap:16px;}
  @media(max-width:900px){.actions-grid{grid-template-columns:1fr 1fr;}}
  @media(max-width:560px){.actions-grid{grid-template-columns:1fr;}}
  .a-col{min-width:0;}
  .a-title{font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:0.05em; display:flex; align-items:center; gap:7px;}
  .a-count{background:var(--panel-2); color:var(--accent-2); border-radius:20px; padding:0 8px; font-size:11px;}
  .a-hint{font-size:11px; color:var(--faint); margin:3px 0 9px;}
  .a-col ul{list-style:none; display:flex; flex-direction:column; gap:7px;}
  .a-col li{font-size:12px; display:flex; flex-direction:column; gap:4px; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-sm); padding:8px 10px;}
  .a-hook{color:var(--text); line-height:1.35;} .a-empty{color:var(--faint);}
  .a-col li .st{align-self:flex-start;}

  /* recommendation cards (organic + paid) */
  .rec-grid{display:grid; grid-template-columns:repeat(2,1fr); gap:14px;}
  @media(max-width:760px){.rec-grid{grid-template-columns:1fr;}}
  .rec-card{background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); padding:15px 16px;}
  .rec-top{display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin-bottom:9px;}
  .carried{font-size:9.5px; font-weight:700; color:var(--gold); text-transform:uppercase; letter-spacing:0.05em;}
  .rec-hook{font-size:14.5px; font-weight:700; line-height:1.35;}
  .rec-line{display:flex; gap:10px; align-items:center; margin:8px 0;}
  .rec-est{font-size:12px; color:var(--gold); font-weight:700;} .rec-est.cpi{color:var(--good);}
  .expand{background:none; border:none; color:var(--accent-2); font-size:12px; font-weight:600; cursor:pointer; padding:4px 0;}
  .rec-detail{margin:8px 0; padding:10px 12px; background:var(--panel-2); border-radius:var(--r-sm); font-size:12.5px; color:var(--muted); line-height:1.5;}
  .rec-beats{white-space:pre-line;} .rec-notes{color:var(--faint); margin-top:7px;}
  .paid-row{margin:5px 0;} .paid-row .k{display:inline-block; min-width:110px; font-size:9px; text-transform:uppercase; letter-spacing:0.06em; color:var(--faint); font-weight:700;}
  /* results */
  .results{margin-top:11px; padding-top:11px; border-top:1px dashed var(--hairline); display:none;}
  .results.show{display:block;}
  .res-inputs{display:flex; gap:12px; flex-wrap:wrap;}
  .res-inputs label{font-size:10px; text-transform:uppercase; letter-spacing:0.05em; color:var(--faint); font-weight:700; display:flex; flex-direction:column; gap:3px;}
  .res-inputs input{width:80px; background:#0e1117; border:1px solid var(--hairline); border-radius:6px; color:var(--text); padding:5px 7px; font-size:12px; font-family:inherit;}
  .res-compare{font-size:12px; margin-top:9px; font-weight:600;}
  .res-compare .beat{color:var(--good);} .res-compare .miss{color:var(--bad);} .res-compare .ontrack{color:var(--muted);}
  .res-auto{display:none; font-size:12px; font-weight:700; color:var(--good); margin-bottom:9px;}
  .src-badge{font-size:8.5px; font-weight:800; letter-spacing:0.06em; padding:2px 7px; border-radius:5px;}
  .src-badge.src-auto{background:rgba(63,185,80,0.16); color:var(--good);}
  .src-badge.src-manual{background:var(--panel-2); color:var(--faint);}
  .adref{display:block; font-size:9px; text-transform:uppercase; letter-spacing:0.05em; color:var(--faint); font-weight:700; margin:8px 0 4px;}
  .adref input{display:block; width:100%; margin-top:4px; background:#0e1117; border:1px solid var(--hairline); border-radius:6px; color:var(--text); padding:6px 8px; font-size:12px; font-family:inherit; text-transform:none; letter-spacing:0;}

  /* top hooks */
  .hook-card{display:flex; gap:15px; align-items:flex-start; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55)); border:1px solid var(--hairline); border-radius:var(--r-md); padding:14px 16px; margin-bottom:10px;}
  .hook-rank{font-size:20px; font-weight:850; color:var(--accent-2); min-width:24px; font-variant-numeric:tabular-nums;}
  .hook-main{flex:1; min-width:0;}
  .hook-text{display:block; font-size:15px; font-weight:680; color:var(--text); text-decoration:none; line-height:1.35;}
  .hook-text:hover{color:var(--accent-2);}
  .hook-meta{display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin:8px 0 6px;}
  .hook-meta .m{font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums;} .hook-meta .er{color:var(--good); font-weight:700;} .hook-meta .fmt{color:var(--accent-2);}
  .hook-why{font-size:12px; color:var(--muted); font-style:italic;}
  .hook-repl{flex:0 0 auto; text-align:right; min-width:90px;}
  .repl-lab{font-size:9px; text-transform:uppercase; letter-spacing:0.07em; color:var(--faint); font-weight:700; margin-bottom:5px;}
  .repl{display:inline-block; width:64px; height:6px; background:rgba(255,255,255,0.08); border-radius:5px; overflow:hidden; vertical-align:middle;}
  .repl-fill{display:block; height:100%; background:linear-gradient(90deg,#2c8c3c,var(--good));}
  .repl-n{font-size:11px; font-weight:700; color:var(--muted); margin-left:6px;}
  .traj{font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; padding:2px 7px; border-radius:20px;}
  .traj-new{background:rgba(75,139,245,0.18); color:var(--blue);} .traj-rising{background:rgba(63,185,80,0.16); color:var(--good);}
  .traj-falling{background:rgba(248,81,73,0.16); color:var(--bad);} .traj-stable{background:var(--panel-2); color:var(--muted);}

  .prev-week .prev-lab{font-size:11px; color:var(--faint); text-transform:uppercase; letter-spacing:0.06em; font-weight:700; margin-bottom:8px;}
  .prev-week ul{list-style:none; display:flex; flex-direction:column; gap:6px;}
  .prev-week li{font-size:12.5px; color:var(--muted); background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-sm); padding:8px 11px;}
  .prev-week li .m{color:var(--faint); font-size:11px;}

  .died-list{list-style:none; display:flex; flex-direction:column; gap:8px;}
  .died-list li{background:rgba(248,81,73,0.07); border:1px solid rgba(248,81,73,0.25); border-radius:var(--r-sm); padding:10px 14px; font-size:13px; color:var(--muted);}
  .died-list li::before{content:"↓ "; color:var(--bad); font-weight:800;}
  .died-empty{color:var(--faint); font-size:13px; background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:14px;}

  .signal{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:16px 20px;}
  .sig-row{display:grid; grid-template-columns:130px 1fr 130px; align-items:center; gap:16px; padding:11px 0; border-top:1px solid var(--hairline);}
  .sig-row:first-child{border-top:none;} .sig-seg{font-weight:700; font-size:14px;}
  .sig-bar{display:flex; align-items:center; gap:8px; margin:4px 0;} .sig-lab{font-size:10px; color:var(--faint); font-weight:700; width:20px;}
  .sig-track{flex:1; height:8px; background:rgba(255,255,255,0.06); border-radius:5px; overflow:hidden;}
  .sig-fill{height:100%;} .sig-fill.yt{background:var(--yt);} .sig-fill.tt{background:var(--tt);} .sig-fill.ig{background:linear-gradient(90deg,#dd2a7b,#8134af);}
  .sig-val{font-size:11px; font-variant-numeric:tabular-nums; color:var(--muted); width:44px; text-align:right;} .sig-win{font-size:12px; font-weight:700; color:var(--accent-2); text-align:right;}

  /* pipeline board (kanban) */
  .board{display:grid; grid-template-columns:repeat(5,1fr); gap:12px; align-items:start;}
  @media (max-width:1000px){ .board{grid-template-columns:repeat(2,1fr);} }
  @media (max-width:560px){ .board{grid-template-columns:1fr;} }
  .bcol{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:10px; min-height:96px; transition:background .12s,border-color .12s;}
  .bcol.dragover{background:var(--panel-2); border-color:var(--accent);}
  .bcol-head{display:flex; align-items:center; justify-content:space-between; margin-bottom:9px; padding:0 2px;}
  .bcol-title{font-size:11.5px; font-weight:700; letter-spacing:.03em; text-transform:uppercase; color:var(--muted);}
  .bcol-n{font-size:11px; font-weight:700; color:var(--faint); background:var(--panel-2); border-radius:999px; padding:1px 8px;}
  .bcol[data-status="posted"] .bcol-title{color:var(--blue);}
  .bcol[data-status="results_in"] .bcol-title{color:var(--good);}
  .bcard{background:var(--panel-2); border:1px solid var(--hairline); border-left:3px solid var(--faint); border-radius:var(--r-sm); padding:9px 10px; margin-bottom:8px; cursor:grab; box-shadow:0 2px 8px -6px rgba(0,0,0,.6);}
  .bcard:last-child{margin-bottom:0;}
  .bcard.dragging{opacity:.45;}
  .bcard[data-type="paid"]{border-left-color:var(--warn);}
  .bcard[data-type="organic"]{border-left-color:var(--accent-2);}
  .bcard-hook{font-size:12.5px; line-height:1.35; color:var(--text); margin-bottom:7px;}
  .bcard-meta{display:flex; align-items:center; gap:6px; flex-wrap:wrap;}
  .bcard .st{cursor:pointer; font-size:9.5px; padding:1px 7px;}
  .bcard-type{font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--faint);}
  .bcard-link{display:inline-flex; align-items:center; gap:3px; font-size:10.5px; color:var(--accent-2); text-decoration:none; margin-top:6px; word-break:break-all;}
  .bcard-link:hover{text-decoration:underline;}
  .bcol-empty{font-size:11px; color:var(--faint); font-style:italic; padding:6px 2px;}
  /* posted-url capture on detail cards */
  .posted-url{margin-bottom:8px;}
  .posted-url label{display:block; font-size:11px; color:var(--muted); font-weight:600;}
  .posted-url input{width:100%; margin-top:3px; background:var(--bg); border:1px solid var(--hairline-strong); color:var(--text); border-radius:7px; padding:6px 8px; font-size:12px;}
  .posted-link{display:inline-block; margin-top:5px; font-size:11px; color:var(--accent-2); text-decoration:none; word-break:break-all;}
  .posted-link:hover{text-decoration:underline;}
  .savebar{position:fixed; right:20px; bottom:20px; display:flex; gap:10px; z-index:20;}
  .savebar button{background:var(--accent); color:#fff; border:none; border-radius:10px; padding:11px 16px; font-size:13px; font-weight:700; cursor:pointer; box-shadow:var(--shadow);}
  .savebar button.ghost{background:var(--panel); border:1px solid var(--hairline-strong); color:var(--muted);}
  .savebar .dirty{color:var(--gold);}
  footer{margin-top:44px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint); line-height:1.7;}
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
    <div class="sub">Predict → ship → measure. Track every recommendation from suggestion to results. <span style="color:var(--faint)">/*__COUNTS__*/</span></div>
  </div>

  <section>
    <div class="sec-head"><h2>This Week's Actions</h2><span class="sec-note">What needs a decision · what's live · click a status to advance it</span></div>
    <div class="actions">/*__ACTIONS__*/</div>
  </section>

  <section>
    <div class="sec-head"><h2>Pipeline Board</h2><span class="sec-note">Drag a card between columns to update its status · or click the status pill to advance · edits persist like everything else (Download state → commit)</span></div>
    <div class="board" id="board"></div>
  </section>

  <section>
    <div class="sec-head"><h2>Organic Content Calendar</h2><span class="sec-note">Hook · estimate · status visible; expand for the full script</span></div>
    <div class="rec-grid">/*__CALENDAR__*/</div>
  </section>

  <section>
    <div class="sec-head"><h2>Paid Ad Creative Briefs</h2><span class="sec-note">Expand for the 15s beat breakdown</span></div>
    <div class="rec-grid">/*__PAID__*/</div>
  </section>

  <section>
    <details class="support" open>
      <summary>Top Hooks This Week — supporting evidence</summary>
      <div style="margin-top:14px;">/*__HOOKS__*/</div>
    </details>
  </section>

  <section>
    <details class="support">
      <summary>Previous Week's Hooks — trajectory history</summary>
      <div style="margin-top:14px;">/*__PREV__*/</div>
    </details>
  </section>

  <section>
    <details class="support">
      <summary>What's Dying — don't make these</summary>
      <div style="margin-top:14px;">/*__DIED__*/</div>
    </details>
  </section>

  <section>
    <details class="support">
      <summary>Platform Signal — engagement by platform &amp; segment</summary>
      <div class="signal" style="margin-top:14px;">/*__SIGNAL__*/</div>
    </details>
  </section>

  <footer>
    Data: YouTube Data API (US, last 7d) + TikTok &amp; Instagram via Apify · enrichment by Claude.<br>
    Status, posted URLs &amp; results persist in dashboard_state.json. Drag cards on the Pipeline Board to change status.
    Posted-content detection is manual by design: when a card goes live, paste its real URL on the card — automatic
    brief-to-post matching isn't reliable across TikTok/IG/YouTube, so we capture the true link rather than guess.
    Results logging is manual too — enter views/ER/saves on posted cards, then Download state and commit the JSON so the next rebuild keeps it.
  </footer>
</div>

<div class="savebar">
  <span class="dirty" id="dirty"></span>
  <button class="ghost" id="reset">Discard local edits</button>
  <button id="download">⭳ Download state</button>
</div>

<script>
const BAKED = /*__STATE_JSON__*/;
const FLOW = ["suggested","briefed","in_production","posted","results_in"];
const LABEL = {suggested:"Suggested",briefed:"Briefed",in_production:"In Production",posted:"Posted",results_in:"Results In"};
const LS_KEY = "speed_trend_overrides_v1";

const overrides = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
function save(){ localStorage.setItem(LS_KEY, JSON.stringify(overrides));
  document.getElementById("dirty").textContent = Object.keys(overrides).length ? "● unsaved edits" : ""; }

function eff(id){ // effective item = baked merged with local override
  const base = (BAKED.items && BAKED.items[id]) || {status:"suggested", results:{}};
  const o = overrides[id] || {};
  return {status: o.status || base.status,
          ad_ref: (o.ad_ref !== undefined ? o.ad_ref : (base.ad_ref || "")),
          posted_url: (o.posted_url !== undefined ? o.posted_url : (base.posted_url || "")),
          results_source: base.results_source || null,
          results: Object.assign({}, base.results||{}, o.results||{})};
}

function applyCard(card){
  const id = card.dataset.id; if(!id) return;
  const e = eff(id);
  // status tag
  const tag = card.querySelector('.st[data-act="cycle"]');
  if(tag){ tag.className = "st st-" + e.status; tag.textContent = LABEL[e.status]; }
  // results block visible only when posted / results_in
  const res = card.querySelector('[data-role="results"]');
  if(res){
    const show = (e.status === "posted" || e.status === "results_in");
    res.classList.toggle("show", show);
    res.querySelectorAll("input[data-f]").forEach(inp=>{
      if(inp.dataset.f === "ad_ref") return;   // ad_ref handled below, not a result
      const v = e.results[inp.dataset.f];
      if(document.activeElement !== inp) inp.value = (v==null?"":v);
    });
    compare(card, id, e);
  }
  // ad_ref input (paid cards)
  const ar = card.querySelector('input[data-f="ad_ref"]');
  if(ar && document.activeElement !== ar) ar.value = e.ad_ref || "";
  // posted URL input + live link (Tier 0 posted-content capture)
  const pu = card.querySelector('input[data-f="posted_url"]');
  if(pu && document.activeElement !== pu) pu.value = e.posted_url || "";
  const pl = card.querySelector('[data-role="postedlink"]');
  if(pl){
    if(e.posted_url){ pl.href = e.posted_url; pl.textContent = "↗ open live post"; pl.style.display = "inline-block"; }
    else { pl.removeAttribute("href"); pl.textContent = ""; pl.style.display = "none"; }
  }
  // source badge (AUTO / MANUAL)
  const sb = card.querySelector('[data-role="source"]');
  if(sb){
    if(e.results_source){ sb.textContent = e.results_source.toUpperCase(); sb.className = "src-badge src-" + e.results_source; }
    else { sb.textContent = ""; sb.className = "src-badge"; }
  }
  // auto-import readout
  const auto = card.querySelector('[data-role="autoread"]');
  if(auto){
    const r = e.results || {};
    if(e.results_source === "auto" && (r.cpi != null || r.spend != null)){
      auto.innerHTML = `Auto (Meta): $${Number(r.spend||0).toLocaleString()} spend · ${r.installs||0} installs · CPI ${r.cpi!=null?("$"+r.cpi):"n/a"}${r.imported_at?(" · "+r.imported_at):""}`;
      auto.style.display = "block";
    } else { auto.innerHTML = ""; auto.style.display = "none"; }
  }
}

function compare(card, id, e){
  const box = card.querySelector('[data-role="compare"]');
  if(!box) return;
  const views = parseFloat(e.results.views);
  if(isNaN(views)){ box.innerHTML=""; return; }   // nothing logged yet
  const est = parseFloat(card.dataset.est || "");
  if(!est){                                        // no reach estimate (e.g. paid CPI) — don't fail silently
    box.innerHTML = `<span class="ontrack">Logged ${views.toLocaleString()} views — no reach estimate to compare (paid CPI tracked separately).</span>`;
    return;
  }
  const dev = (views - est) / est;
  const pct = Math.round(dev*100);
  const cls = dev > 0.2 ? "beat" : dev < -0.2 ? "miss" : "ontrack";
  const word = dev > 0.2 ? "beat estimate" : dev < -0.2 ? "missed estimate" : "on track";
  box.innerHTML = `<span class="${cls}">Actual ${views.toLocaleString()} vs est ${est.toLocaleString()} — ${pct>=0?"+":""}${pct}% (${word})</span>`;
}

// single source of truth for a status change — used by click-to-advance AND drag
function setStatus(id, status){
  if(!id || FLOW.indexOf(status) < 0) return;
  overrides[id] = overrides[id] || {}; overrides[id].status = status; save();
  refreshAll();
}
function advanceStatus(id){
  const cur = eff(id).status;
  setStatus(id, FLOW[(FLOW.indexOf(cur)+1) % FLOW.length]);
}
// re-sync every view (detail cards + the live board) from current effective state
function refreshAll(){
  document.querySelectorAll(".rec-card").forEach(applyCard);
  renderBoard();
}

// status cycle (rec-card tags and board pills share the .st[data-act="cycle"] hook)
document.body.addEventListener("click", ev=>{
  const t = ev.target;
  if(t.matches('.st[data-act="cycle"]')){ advanceStatus(t.dataset.id); return; }
  if(t.matches('.expand')){
    const d = t.parentElement.querySelector(".rec-detail");
    if(d){ const open = d.hasAttribute("hidden"); if(open){ d.removeAttribute("hidden"); t.textContent = t.textContent.replace("▾","▴"); }
           else { d.setAttribute("hidden",""); t.textContent = t.textContent.replace("▴","▾"); } }
  }
});

// results entry
document.body.addEventListener("input", ev=>{
  const inp = ev.target;
  if(inp.matches('input[data-f]')){
    const card = inp.closest(".rec-card"); const id = card.dataset.id;
    overrides[id] = overrides[id] || {};
    if(inp.dataset.f === "ad_ref"){          // item-level match key, not a result
      overrides[id].ad_ref = inp.value; save(); return;
    }
    if(inp.dataset.f === "posted_url"){      // real live link, item-level string
      overrides[id].posted_url = inp.value; save();
      applyCard(card); renderBoard(); return;
    }
    overrides[id].results = overrides[id].results || {};
    overrides[id].results[inp.dataset.f] = inp.value === "" ? null : Number(inp.value);
    save(); compare(card, id, eff(id));
  }
});

document.getElementById("download").addEventListener("click", ()=>{
  const out = JSON.parse(JSON.stringify(BAKED));
  for(const id in overrides){
    if(!out.items[id]) continue;
    if(overrides[id].status) out.items[id].status = overrides[id].status;
    if(overrides[id].ad_ref !== undefined) out.items[id].ad_ref = overrides[id].ad_ref;
    if(overrides[id].posted_url !== undefined) out.items[id].posted_url = overrides[id].posted_url;
    if(overrides[id].results) out.items[id].results = Object.assign(out.items[id].results||{}, overrides[id].results);
  }
  out.updated_at = new Date().toISOString().slice(0,10);
  const blob = new Blob([JSON.stringify(out, null, 2)], {type:"application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "dashboard_state.json"; a.click();
});

document.getElementById("reset").addEventListener("click", ()=>{
  if(confirm("Discard local (unsaved) status/results edits and revert to the committed state?")){
    localStorage.removeItem(LS_KEY); location.reload();
  }
});

// ---- Pipeline Board (kanban): rendered live from BAKED.items + overrides ----
function escg(s){ return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function renderBoard(){
  const board = document.getElementById("board"); if(!board) return;
  const items = BAKED.items || {};
  const cols = {}; FLOW.forEach(s=>cols[s]=[]);
  Object.keys(items).forEach(id=>{ (cols[eff(id).status] || (cols[eff(id).status]=[])).push(id); });
  board.innerHTML = FLOW.map(status=>{
    const ids = cols[status] || [];
    const cards = ids.map(id=>{
      const base = items[id] || {}; const e = eff(id);
      const hook = base.hook || "";
      const link = e.posted_url
        ? `<a class="bcard-link" href="${escg(e.posted_url)}" target="_blank" rel="noopener">↗ live post</a>` : "";
      return `<div class="bcard" draggable="true" data-id="${escg(id)}" data-type="${escg(base.type||'')}">
        <div class="bcard-hook">“${escg(hook.slice(0,90))}${hook.length>90?"…":""}”</div>
        <div class="bcard-meta">
          <span class="bcard-type">${escg(base.type||"")}</span>
          <button class="st st-${status}" data-act="cycle" data-id="${escg(id)}" title="Click to advance status">${LABEL[status]}</button>
        </div>${link}
      </div>`;
    }).join("") || `<div class="bcol-empty">nothing here</div>`;
    return `<div class="bcol" data-status="${status}">
      <div class="bcol-head"><span class="bcol-title">${LABEL[status]}</span><span class="bcol-n">${ids.length}</span></div>
      ${cards}</div>`;
  }).join("");
}

// Drag-and-drop (listeners live on #board, which survives innerHTML swaps)
(function(){
  const board = document.getElementById("board"); if(!board) return;
  let dragId = null;
  board.addEventListener("dragstart", e=>{
    const c = e.target.closest(".bcard"); if(!c) return;
    dragId = c.dataset.id; c.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try{ e.dataTransfer.setData("text/plain", dragId); }catch(_){}
  });
  board.addEventListener("dragend", e=>{
    const c = e.target.closest(".bcard"); if(c) c.classList.remove("dragging");
    board.querySelectorAll(".bcol.dragover").forEach(x=>x.classList.remove("dragover"));
    dragId = null;
  });
  board.addEventListener("dragover", e=>{
    if(e.target.closest(".bcol")){ e.preventDefault(); e.dataTransfer.dropEffect = "move"; }
  });
  board.addEventListener("dragenter", e=>{
    const col = e.target.closest(".bcol"); if(col) col.classList.add("dragover");
  });
  board.addEventListener("dragleave", e=>{
    const col = e.target.closest(".bcol");
    if(col && !col.contains(e.relatedTarget)) col.classList.remove("dragover");
  });
  board.addEventListener("drop", e=>{
    e.preventDefault();
    const col = e.target.closest(".bcol"); if(!col) return;
    col.classList.remove("dragover");
    const id = dragId || (e.dataTransfer && e.dataTransfer.getData("text/plain"));
    if(id) setStatus(id, col.dataset.status);   // setStatus -> refreshAll -> renderBoard
  });
})();

document.querySelectorAll(".rec-card").forEach(applyCard);
renderBoard();
save();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        main()
    except (EnvironmentError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
