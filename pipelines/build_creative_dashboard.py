"""Build a self-contained creative-performance dashboard HTML from live Sheets data.

Pulls the Channel Overview / Campaign Installs / Retention / Last Updated tabs
from the Google Sheet at build time, computes the headline KPIs + highlights,
asks Claude (claude-sonnet-4-6) for structured insight cards, and bakes
everything into a single self-contained file at docs/creative_dashboard.html.

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
_D1_TARGET = 0.25                # D1 retention KPI threshold (green above, red below)

# Insight cache: the Claude call is the only paid step in a daily run, so we
# skip it unless the headline numbers have moved meaningfully. Thresholds below
# define "meaningful" per metric ("rel" = relative change, "abs" = absolute).
_INSIGHTS_CACHE = _ROOT / "data" / "processed" / "creative_dashboard_insights.cache.json"
_FP_THRESHOLDS: dict[str, tuple[str, float]] = {
    "total_installs":   ("rel", 0.03),   # 3% relative
    "best_paid_ecpi":   ("rel", 0.05),   # 5% relative
    "d1_retention":     ("abs", 0.01),   # 1 percentage point
    "reengagement_cvr": ("rel", 0.15),   # 15% relative
    "meta_spend":       ("rel", 0.05),   # 5% relative — Meta total spend
    "meta_installs":    ("rel", 0.05),   # 5% relative — Meta total installs
}


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


def _records_optional(ss, tab: str) -> list[dict]:
    """Like _records, but returns [] if the tab doesn't exist yet.

    Used for newer tabs (e.g. Meta Campaigns) so an older sheet still builds.
    """
    from gspread.exceptions import WorksheetNotFound
    try:
        return _records(ss, tab)
    except WorksheetNotFound:
        return []


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

    total_installs = sum(c["installs"] for c in channels)

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

    # Re-engagement channel (high clicks, ~no installs) drives the CVR KPI.
    re_pat = re.compile(r"re-?engag", re.I)
    re_ch = next((c for c in channels if re_pat.search(c["channel"])), None)
    reengagement = None
    if re_ch and re_ch["clicks"] > 0:
        reengagement = {
            "channel": re_ch["channel"],
            "clicks": re_ch["clicks"],
            "installs": re_ch["installs"],
            "cvr": round(re_ch["installs"] / re_ch["clicks"] * 100, 3),
        }

    # The table shows only channels that actually drove installs — drop zero rows.
    display = [c for c in channels if c["installs"] > 0]

    return {
        "channels": display,
        "total_installs": total_installs,
        "most_efficient": most_efficient["channel"] if most_efficient else None,
        "most_efficient_detail": most_efficient,
        "facebook_flag": facebook_flag,
        "min_meaningful_installs": _MIN_MEANINGFUL_INSTALLS,
        "reengagement": reengagement,
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


def build_meta(rows: list[dict]) -> dict:
    """Meta campaign performance from the Meta Campaigns tab, ranked by spend."""
    camps = []
    for r in rows:
        name = str(r.get("campaign_name", "")).strip()
        if not name:
            continue
        camps.append({
            "campaign": name,
            "spend": round(_num(r.get("spend")), 2),
            "impressions": int(_num(r.get("impressions"))),
            "clicks": int(_num(r.get("clicks"))),
            "installs": int(_num(r.get("mobile_app_install"))),
        })
    camps.sort(key=lambda c: c["spend"], reverse=True)
    totals = {
        "spend": round(sum(c["spend"] for c in camps), 2),
        "impressions": sum(c["impressions"] for c in camps),
        "clicks": sum(c["clicks"] for c in camps),
        "installs": sum(c["installs"] for c in camps),
    }
    return {"campaigns": camps, "totals": totals}


# ------------------------------------------------------------------
# Claude insights (structured cards)
# ------------------------------------------------------------------

def generate_insights(channels: dict, campaigns: dict, retention: dict, meta: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"insights": [], "critical_index": None,
                "source": "unavailable (ANTHROPIC_API_KEY not set)"}

    summary = {
        "channels_top": channels["channels"][:8],
        "most_efficient_channel": channels["most_efficient_detail"],
        "facebook_vs_apple_ecpi": channels["facebook_flag"],
        "reengagement_channel": channels["reengagement"],
        "top_campaigns": campaigns["campaigns"],
        "retention_curve_d1_d7": dict(zip(retention["labels"], retention["values"])),
        "retention_cohorts_used": retention["cohort_count"],
        "meta_paid_campaigns": meta["campaigns"],
        "meta_paid_totals": meta["totals"],
    }

    prompt = (
        "You are a performance-marketing analyst for Speed Wallet, a Bitcoin "
        "Lightning payments app. Below is REAL data pulled live from the user "
        "acquisition dashboard (installs, eCPI, top campaigns, the D1-D7 "
        "retention curve from matured cohorts, and Meta/Facebook paid campaign "
        "spend, clicks and mobile-app-install counts).\n\n"
        "Write 4-5 insight cards. Each card is a JSON object with exactly these keys:\n"
        '  "type": one of "positive", "warning", or "neutral"\n'
        '  "headline": a punchy 4-8 word headline, no trailing period\n'
        '  "detail": ONE sentence citing the specific numbers (channel names, '
        "eCPI values, install counts, retention %, or Meta campaign spend/installs)\n\n"
        "Be specific, no fluff, no generic advice. Ordering: the FIRST card "
        "covers organic install dominance (type positive); the SECOND card is "
        "the re-engagement funnel inefficiency — clicks converting to almost no "
        'installs — and MUST be type "warning" (the single most urgent issue). '
        "Then the rest, AT LEAST ONE of which MUST reference the Meta paid "
        "campaigns by name, citing real spend (USD) and mobile_app_install "
        "counts (e.g. cost-per-install efficiency across the Meta campaigns).\n\n"
        "Return ONLY a JSON array of these objects. No prose.\n\n"
        f"DATA:\n{json.dumps(summary, indent=2)}"
    )

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_INSIGHTS_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        cards = _parse_insights(resp.content[0].text.strip())
        if cards:
            cards, critical_index = _reorder_insights(cards)
            return {"insights": cards, "critical_index": critical_index,
                    "source": _INSIGHTS_MODEL}
        return {"insights": [], "critical_index": None,
                "source": f"{_INSIGHTS_MODEL} (unparseable response)"}
    except Exception as e:
        return {"insights": [], "critical_index": None,
                "source": f"error: {type(e).__name__}: {e}"}


def _parse_insights(text: str) -> list[dict]:
    """Parse Claude's JSON array of insight cards into normalised dicts."""
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
    if not isinstance(val, list):
        return []
    cards = []
    for item in val:
        if isinstance(item, dict):
            headline = str(item.get("headline", "")).strip()
            detail = str(item.get("detail", "")).strip()
            typ = str(item.get("type", "neutral")).strip().lower()
            if typ not in ("positive", "warning", "neutral"):
                typ = "neutral"
            if headline or detail:
                cards.append({"type": typ, "headline": headline, "detail": detail})
        elif str(item).strip():
            # Fallback if the model returned plain strings instead of objects.
            cards.append({"type": "neutral", "headline": "", "detail": str(item).strip()})
    return cards


def _reorder_insights(cards: list[dict]) -> tuple[list[dict], int | None]:
    """Force the re-engagement card to position 2; return its index (critical)."""
    pat = re.compile(r"re-?engag", re.I)
    idx = next(
        (i for i, c in enumerate(cards)
         if pat.search(f"{c.get('headline', '')} {c.get('detail', '')}")),
        None,
    )
    if idx is None:
        return cards, None
    if len(cards) >= 2 and idx != 1:
        card = cards.pop(idx)
        cards.insert(1, card)
        return cards, 1
    return cards, idx


# ------------------------------------------------------------------
# Insight cache (cost control) — skip the Claude call on unchanged data
# ------------------------------------------------------------------

def _load_cache() -> dict | None:
    try:
        return json.loads(_INSIGHTS_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_cache(fingerprint: dict, result: dict) -> None:
    _INSIGHTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "insights": result["insights"],
        "critical_index": result.get("critical_index"),
        "source": result.get("source", _INSIGHTS_MODEL),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    _INSIGHTS_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _significant_change(old_fp: dict, new_fp: dict) -> tuple[bool, list[str]]:
    """Return (changed, reasons) — True if any metric moved past its threshold."""
    reasons = []
    for key, (mode, thr) in _FP_THRESHOLDS.items():
        old, new = old_fp.get(key), new_fp.get(key)
        if old is None or new is None:
            if old != new:  # a metric appeared or disappeared
                reasons.append(f"{key} {old}→{new}")
            continue
        if mode == "abs":
            if abs(new - old) >= thr:
                reasons.append(f"{key} {old}→{new} (Δ{abs(new-old):.4g})")
        else:  # relative
            base = abs(old)
            if base == 0:
                if new != old:
                    reasons.append(f"{key} {old}→{new}")
            elif abs(new - old) / base >= thr:
                reasons.append(f"{key} {old}→{new} ({abs(new-old)/base:.0%})")
    return (len(reasons) > 0, reasons)


def get_insights(fingerprint: dict, channels: dict, campaigns: dict, retention: dict, meta: dict) -> dict:
    """Cache-aware insights: only call Claude when the headline numbers shifted.

    Returns the same shape as generate_insights() plus 'cache_hit'. On a run
    where the data hasn't moved past the thresholds, the cached insights are
    reused and no Claude request is made.
    """
    cache = _load_cache()
    if cache and cache.get("insights"):
        changed, reasons = _significant_change(cache.get("fingerprint", {}), fingerprint)
        if not changed:
            print(f"  cache HIT — data unchanged since {cache.get('generated_at', '?')}; "
                  f"skipping Claude call")
            return {
                "insights": cache["insights"],
                "critical_index": cache.get("critical_index"),
                "source": f"{cache.get('source', _INSIGHTS_MODEL)} (cached)",
                "cache_hit": True,
            }
        print(f"  cache MISS — regenerating; shifted: {'; '.join(reasons)}")
    else:
        print("  cache MISS — no usable cache; generating")

    result = generate_insights(channels, campaigns, retention, meta)

    # If Claude failed but a cache exists, reuse it rather than show nothing.
    if not result["insights"] and cache and cache.get("insights"):
        print(f"  regeneration failed ({result['source']}) — falling back to cache")
        return {
            "insights": cache["insights"],
            "critical_index": cache.get("critical_index"),
            "source": f"{cache.get('source', _INSIGHTS_MODEL)} (cached; regen failed)",
            "cache_hit": True,
        }

    if result["insights"]:
        _save_cache(fingerprint, result)
    result["cache_hit"] = False
    return result


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

    print("Reading tabs: Channel Overview, Campaign Installs, Retention, Meta Campaigns, Last Updated...")
    channels = build_channels(_records(ss, "Channel Overview"))
    campaigns = build_campaigns(_records(ss, "Campaign Installs"))
    retention = build_retention(_records(ss, "Retention"))
    meta = build_meta(_records_optional(ss, "Meta Campaigns"))

    last_updated = ""
    lu = _records(ss, "Last Updated")
    if lu:
        last_updated = str(list(lu[0].values())[0])

    d1 = retention["values"][0] if retention["values"] else 0.0
    fingerprint = {
        "total_installs": channels["total_installs"],
        "best_paid_ecpi": channels["most_efficient_detail"]["ecpi"] if channels["most_efficient_detail"] else None,
        "d1_retention": round(d1, 4),
        "reengagement_cvr": channels["reengagement"]["cvr"] if channels["reengagement"] else None,
        "meta_spend": meta["totals"]["spend"] or None,
        "meta_installs": meta["totals"]["installs"] or None,
    }

    print("Resolving insights (cache-aware)...")
    insights = get_insights(fingerprint, channels, campaigns, retention, meta)
    print(f"  insights: {insights['source']}  ({len(insights['insights'])} cards)")

    kpis = {
        "total_installs": channels["total_installs"],
        "channel_count": len(channels["channels"]),
        "best_paid": channels["most_efficient_detail"],
        "d1_retention": d1,
        "d1_above": d1 >= _D1_TARGET,
        "d1_target": _D1_TARGET,
        "matured_cohorts": retention["cohort_count"],
        "reengagement": channels["reengagement"],
    }

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "last_updated": last_updated,
        "kpis": kpis,
        **channels,
        **campaigns,
        "retention": retention,
        "meta": meta,
        "insights": insights["insights"],
        "insights_critical_index": insights.get("critical_index"),
        "insights_source": insights["source"],
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render_html(data), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  channels={len(data['channels'])} campaigns={len(data['campaigns'])} "
          f"retention_cohorts={retention['cohort_count']} meta_campaigns={len(meta['campaigns'])} "
          f"total_installs={kpis['total_installs']:,}")


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
    --bg:#0a0c11;
    --surface:rgba(255,255,255,0.026);
    --surface-2:rgba(255,255,255,0.05);
    --hairline:rgba(255,255,255,0.09);
    --hairline-strong:rgba(255,255,255,0.15);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#6e40c9; --accent-2:#a371f7;
    --good:#3fb950; --good-2:#56d364; --warn:#e3b341;
    --bad:#f85149; --bad-2:#ff7b72;
    --grad:linear-gradient(120deg,#6e40c9,#a371f7);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.65);
    --shadow-lift:0 18px 44px -16px rgba(0,0,0,0.75);
    --r-lg:16px; --r-md:12px; --r-sm:9px;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{
    margin:0; color:var(--text); min-height:100vh;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.5; -webkit-font-smoothing:antialiased; letter-spacing:-0.005em;
    background:
      radial-gradient(1100px 600px at 50% -8%, rgba(110,64,201,0.22), transparent 58%),
      radial-gradient(820px 520px at 100% 2%, rgba(163,113,245,0.10), transparent 52%),
      radial-gradient(720px 480px at 0% 10%, rgba(63,185,80,0.05), transparent 50%),
      var(--bg);
    background-attachment:fixed;
  }
  body::before{ /* film grain */
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:0.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }
  .wrap{position:relative; z-index:1; max-width:1180px; margin:0 auto; padding:0 24px 80px;}

  /* Brand bar */
  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand{font-weight:750; font-size:16px; letter-spacing:-0.02em; display:flex; align-items:center;}
  .brand .bolt{
    margin-right:8px; font-size:17px;
    background:linear-gradient(180deg,#ffd66e,#f0a02a); -webkit-background-clip:text; background-clip:text;
    -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 6px rgba(240,160,42,0.45));
  }
  .brandbar .sync{font-size:12px; color:var(--muted);}
  .brandbar .sync b{color:var(--text); font-weight:600;}

  /* Title */
  .title-block{margin:32px 0 24px;}
  h1{
    font-size:27px; margin:0 0 5px; font-weight:760; letter-spacing:-0.03em;
    background:linear-gradient(180deg,#ffffff,#c9c3e8); -webkit-background-clip:text; background-clip:text;
    -webkit-text-fill-color:transparent;
  }
  .title-block .sub{color:var(--muted); font-size:13px;}

  /* KPI cards */
  .kpi-grid{display:grid; grid-template-columns:repeat(4,1fr); gap:16px;}
  @media(max-width:880px){.kpi-grid{grid-template-columns:repeat(2,1fr);}}
  @media(max-width:480px){.kpi-grid{grid-template-columns:1fr;}}
  .kpi{
    position:relative; overflow:hidden; padding:20px;
    background:linear-gradient(180deg,var(--surface),rgba(255,255,255,0.008));
    border:1px solid var(--hairline); border-radius:var(--r-lg);
    box-shadow:var(--shadow); transition:transform .25s ease, border-color .25s ease, box-shadow .25s ease;
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both;
  }
  .kpi::before{content:""; position:absolute; inset:0 0 auto 0; height:2px; background:var(--grad); opacity:.85;}
  .kpi:has(.val.good)::before{background:linear-gradient(90deg,var(--good),var(--good-2));}
  .kpi:has(.val.bad)::before{background:linear-gradient(90deg,var(--bad),var(--bad-2));}
  .kpi:hover{transform:translateY(-3px); border-color:var(--hairline-strong); box-shadow:var(--shadow-lift);}
  .kpi.flag{border-color:rgba(248,81,73,0.42); box-shadow:0 0 0 1px rgba(248,81,73,0.12), var(--shadow);}
  .kpi .val{font-size:clamp(28px,3.4vw,34px); font-weight:760; letter-spacing:-0.03em; line-height:1.04; font-variant-numeric:tabular-nums;}
  .kpi .val:not(.good):not(.bad){background:linear-gradient(180deg,#ffffff,#cfd6e4); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .kpi .val.good{color:var(--good);} .kpi .val.bad{color:var(--bad);}
  .kpi .lab{font-size:10.5px; text-transform:uppercase; letter-spacing:0.09em; color:var(--faint); margin-top:10px; font-weight:700;}
  .kpi .sub{font-size:12px; color:var(--muted); margin-top:7px;}

  section{margin:52px 0; animation:rise .55s cubic-bezier(.2,.7,.2,1) both;}
  .sec-head{display:flex; align-items:center; gap:11px; margin-bottom:18px;}
  h2{font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted); margin:0; font-weight:700; display:flex; align-items:center; gap:10px;}
  h2::before{content:""; width:3px; height:13px; border-radius:2px; background:var(--grad);}
  .note{font-size:12px; color:var(--faint);}
  .panel{
    background:linear-gradient(180deg,var(--surface),rgba(255,255,255,0.006));
    border:1px solid var(--hairline); border-radius:var(--r-lg); padding:22px;
    box-shadow:var(--shadow); backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
  }
  .grid-2{display:grid; grid-template-columns:1fr 1fr; gap:24px;}
  @media(max-width:880px){.grid-2{grid-template-columns:1fr;}}

  /* Channel table */
  .table-wrap{overflow-x:auto;}
  table{width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; min-width:520px;}
  th,td{text-align:right; padding:8px 12px; border-bottom:1px solid var(--hairline); font-size:13px;}
  th{color:var(--faint); font-weight:700; font-size:10px; text-transform:uppercase; letter-spacing:0.07em;}
  th:first-child,td:first-child{text-align:left;}
  tbody tr{transition:background .15s ease;}
  tbody tr:hover{background:rgba(255,255,255,0.03);}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr.eff{background:linear-gradient(90deg,rgba(63,185,80,0.10),transparent 75%); box-shadow:inset 3px 0 0 var(--good);}
  td.ch{font-weight:600;}
  .badge{display:inline-block; font-size:10px; font-weight:700; padding:2px 8px; border-radius:20px; margin-left:8px; vertical-align:middle; letter-spacing:0.03em;}
  .badge.good{background:rgba(63,185,80,0.16); color:var(--good);}
  .badge.bad{background:rgba(248,81,73,0.16); color:var(--bad);}
  .num-good{color:var(--good); font-weight:650;}
  .num-bad{color:var(--bad); font-weight:650;}
  .muted{color:var(--muted);}
  .chart-box{position:relative; height:340px;}

  /* Insight cards */
  .ins-grid{display:grid; grid-template-columns:1fr 1fr; gap:16px;}
  @media(max-width:880px){.ins-grid{grid-template-columns:1fr;}}
  .ins-card{
    display:flex; gap:13px; padding:17px 18px;
    background:linear-gradient(180deg,var(--surface),rgba(255,255,255,0.006));
    border:1px solid var(--hairline); border-radius:var(--r-md);
    box-shadow:var(--shadow); transition:transform .25s ease, border-color .25s ease, box-shadow .25s ease;
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both;
  }
  .ins-card:hover{transform:translateY(-2px); border-color:var(--hairline-strong); box-shadow:var(--shadow-lift);}
  .ins-card.critical{border-color:rgba(248,81,73,0.5); background:linear-gradient(120deg,rgba(248,81,73,0.10),rgba(255,255,255,0.006) 70%); box-shadow:0 0 0 1px rgba(248,81,73,0.14), var(--shadow);}
  .ins-ico{flex:0 0 auto; width:30px; height:30px; border-radius:var(--r-sm); display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:700;}
  .ins-positive{color:var(--good); background:rgba(63,185,80,0.15);}
  .ins-warning{color:var(--warn); background:rgba(227,179,65,0.15);}
  .ins-neutral{color:var(--accent-2); background:rgba(110,64,201,0.18);}
  .ins-card.critical .ins-ico{color:#fff; background:linear-gradient(180deg,var(--bad-2),var(--bad));}
  .ins-head{font-size:14.5px; font-weight:680; letter-spacing:-0.01em; margin-bottom:4px;}
  .ins-detail{font-size:12.5px; color:var(--muted); line-height:1.5;}
  .src{font-size:11px; color:var(--faint); margin-top:16px; text-align:right;}

  /* Meta empty state */
  .empty{
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    padding:52px 20px; text-align:center; border:1px dashed var(--hairline-strong); border-radius:var(--r-lg);
    background:radial-gradient(420px 200px at 50% 0%, rgba(110,64,201,0.10), transparent 70%);
  }
  .empty .ico{color:var(--accent-2); margin-bottom:14px; opacity:0.85;}
  .empty .msg{color:var(--muted); font-size:14px; max-width:440px;}

  .fallback{color:var(--warn); font-size:13px; padding:20px; text-align:center;}
  footer{margin-top:52px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint);}
  code{background:var(--surface-2); padding:1px 6px; border-radius:5px; font-size:12px; color:var(--accent-2);}

  /* Motion */
  @keyframes rise{from{opacity:0; transform:translateY(12px);} to{opacity:1; transform:none;}}
  .kpi:nth-child(1){animation-delay:.05s} .kpi:nth-child(2){animation-delay:.11s}
  .kpi:nth-child(3){animation-delay:.17s} .kpi:nth-child(4){animation-delay:.23s}
  .ins-card:nth-child(2){animation-delay:.06s} .ins-card:nth-child(3){animation-delay:.12s}
  .ins-card:nth-child(4){animation-delay:.18s} .ins-card:nth-child(5){animation-delay:.24s}
  @media (prefers-reduced-motion: reduce){ *{animation:none!important; transition:none!important;} }
</style>
</head>
<body>
<div class="wrap">

  <div class="brandbar">
    <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    <div class="sync">Synced: <b id="syncTime">—</b></div>
  </div>

  <div class="title-block">
    <h1>Creative Performance Dashboard</h1>
    <div class="sub">User acquisition &amp; retention · last 30 days</div>
  </div>

  <!-- KPI summary row -->
  <div class="kpi-grid" id="kpis"></div>

  <!-- 1. Channel Performance -->
  <section>
    <div class="sec-head"><h2>Channel Performance</h2><span class="note" id="chNote"></span></div>
    <div class="panel"><div class="table-wrap"><table id="chTable">
      <thead><tr><th>Channel</th><th>Installs</th><th>eCPI</th><th>Impressions</th><th>Clicks</th></tr></thead>
      <tbody></tbody>
    </table></div></div>
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

  <!-- 4. Meta Campaign Performance -->
  <section>
    <div class="sec-head"><h2>Meta Campaign Performance</h2><span class="note" id="metaNote"></span></div>
    <div class="panel"><div class="table-wrap"><table id="metaTable">
      <thead><tr><th>Campaign</th><th>Spend</th><th>Impressions</th><th>Clicks</th><th>App Installs</th></tr></thead>
      <tbody></tbody>
    </table></div></div>
  </section>

  <!-- 5. Key Insights -->
  <section>
    <div class="sec-head"><h2>Key Insights</h2></div>
    <div class="ins-grid" id="insights"></div>
    <div class="src" id="insightsSrc"></div>
  </section>

  <!-- 6. Meta Creative Analysis placeholder -->
  <section>
    <div class="sec-head"><h2>Meta Creative Analysis</h2></div>
    <div class="empty">
      <svg class="ico" width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <line x1="3" y1="20.5" x2="21" y2="20.5"/>
        <rect x="4.5" y="11" width="3.6" height="9.5"/>
        <rect x="10.2" y="6" width="3.6" height="14.5"/>
        <rect x="15.9" y="14" width="3.6" height="6.5"/>
      </svg>
      <div class="msg">Campaign-level Meta data is now live above. Creative-level (ad &amp; asset) breakdown is pending and will populate once wired in.</div>
    </div>
  </section>

  <footer>
    Data pulled live from Google Sheets at build time and inlined into this file.
    Charts via Chart.js (cdnjs). Insights by <code id="modelName">—</code>.
    Built <span id="builtAt">—</span>.
  </footer>
</div>

<script>
const DATA = /*__DATA__*/;

const intFmt = new Intl.NumberFormat("en-US");
const fmtInt = n => intFmt.format(Math.round(n||0));
const fmtEcpi = v => (v && v > 0) ? "$" + Number(v).toFixed(2) : "—";
const fmtCost = v => (v && v > 0) ? "$" + intFmt.format(Math.round(v)) : "—";
const usdFmt = new Intl.NumberFormat("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtMoney = v => (v || v === 0) ? "$" + usdFmt.format(Number(v)) : "—";
const esc = s => { const d=document.createElement("div"); d.textContent = (s==null?"":s); return d.innerHTML; };

function kpiCard(val, valCls, lab, sub, cardCls, count){
  const data = count
    ? ` data-count="${count.target}" data-prefix="${count.prefix||""}" data-suffix="${count.suffix||""}" data-decimals="${count.decimals||0}"`
    : "";
  return `<div class="kpi ${cardCls||""}">
    <div class="val ${valCls||""}"${data}>${val}</div>
    <div class="lab">${lab}</div>
    <div class="sub">${sub}</div>
  </div>`;
}

// Animated count-up for KPI numbers (eased), respecting reduced-motion.
function animateVal(el){
  const target = parseFloat(el.dataset.count);
  if (isNaN(target)) return;
  const dec = parseInt(el.dataset.decimals||"0", 10);
  const pre = el.dataset.prefix||"", suf = el.dataset.suffix||"";
  const fmt = v => pre + (dec>0 ? v.toFixed(dec) : intFmt.format(Math.round(v))) + suf;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches){ el.textContent = fmt(target); return; }
  const dur = 950, t0 = performance.now();
  el.textContent = fmt(0);
  requestAnimationFrame(function step(now){
    const p = Math.min(1, (now-t0)/dur), e = 1 - Math.pow(1-p, 3);
    el.textContent = fmt(target * e);
    if (p < 1) requestAnimationFrame(step); else el.textContent = fmt(target);
  });
}

function renderKPIs(){
  const k = DATA.kpis, g = document.getElementById("kpis");
  const cards = [];
  cards.push(kpiCard(fmtInt(k.total_installs), "", "Total Installs",
    `Last 30 days · ${k.channel_count} active channels`, "",
    {target:k.total_installs, decimals:0}));

  if (k.best_paid)
    cards.push(kpiCard("$" + k.best_paid.ecpi.toFixed(2), "good", "Best Paid eCPI",
      `${esc(k.best_paid.channel)} · ${fmtInt(k.best_paid.installs)} installs`, "",
      {target:k.best_paid.ecpi, prefix:"$", decimals:2}));
  else
    cards.push(kpiCard("—", "", "Best Paid eCPI", "no paid channels", ""));

  const above = k.d1_above;
  cards.push(kpiCard((k.d1_retention*100).toFixed(1) + "%", above ? "good" : "bad",
    "D1 Retention",
    `${above ? "▲" : "▼"} ${above ? "above" : "below"} ${Math.round(k.d1_target*100)}% target · ${k.matured_cohorts} cohorts`,
    "", {target:k.d1_retention*100, suffix:"%", decimals:1}));

  if (k.reengagement)
    cards.push(kpiCard(k.reengagement.cvr.toFixed(2) + "%", "bad", "Re-engagement CVR",
      `⚠ ${fmtInt(k.reengagement.clicks)} clicks → ${fmtInt(k.reengagement.installs)} installs`, "flag",
      {target:k.reengagement.cvr, suffix:"%", decimals:2}));
  else
    cards.push(kpiCard("—", "", "Re-engagement CVR", "no re-engagement channel", ""));

  g.innerHTML = cards.join("");
  g.querySelectorAll(".val[data-count]").forEach(animateVal);
}

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
    const ecpiCls = (c.channel === eff) ? "num-good"
      : ((fb && fb.flagged && c.channel.toLowerCase()==="facebook") ? "num-bad" : "");
    tr.innerHTML =
      `<td class="ch">${esc(c.channel)}${badge}</td>` +
      `<td>${fmtInt(c.installs)}</td>` +
      `<td class="${ecpiCls}">${fmtEcpi(c.ecpi)}</td>` +
      `<td class="muted">${fmtInt(c.impressions)}</td>` +
      `<td class="muted">${fmtInt(c.clicks)}</td>`;
    tb.appendChild(tr);
  });
  const parts = [];
  if (DATA.most_efficient_detail)
    parts.push(`Most efficient: ${eff} ($${DATA.most_efficient_detail.ecpi.toFixed(2)} eCPI)`);
  if (fb && fb.flagged)
    parts.push(`Facebook $${fb.facebook_ecpi.toFixed(2)} > Apple $${fb.apple_ecpi.toFixed(2)}`);
  parts.push(`${DATA.channels.length} channels with installs`);
  document.getElementById("chNote").textContent = parts.join("  ·  ");
}

function renderCampaigns(){
  document.getElementById("cmpNote").textContent =
    `Top ${DATA.campaigns.length} by installs (excl. ${DATA.excluded_organic_rows} unattributed Organic)`;
  if (typeof Chart === "undefined"){ chartFallback("cmpChart"); return; }
  const trunc = s => s.length > 25 ? s.slice(0, 25) + "..." : s;
  const labels = DATA.campaigns.map(c => trunc(c.campaign));
  const installs = DATA.campaigns.map(c => c.installs);
  const ctx = document.getElementById("cmpChart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 560, 0);
  grad.addColorStop(0, "#6e40c9"); grad.addColorStop(1, "#a371f7");
  new Chart(ctx, {
    type:"bar",
    data:{labels, datasets:[{
      label:"Installs", data:installs,
      backgroundColor:grad, borderRadius:4, maxBarThickness:24,
    }]},
    options:{
      indexAxis:"y", responsive:true, maintainAspectRatio:false,
      layout:{padding:{left:10, right:16, top:4, bottom:4}},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:"rgba(14,16,24,0.96)", borderColor:"rgba(255,255,255,0.10)", borderWidth:1,
          cornerRadius:9, padding:11, titleColor:"#edf1f7", bodyColor:"#9aa4b2", displayColors:false,
          callbacks:{
            title:(items)=> DATA.campaigns[items[0].dataIndex].campaign,
            afterLabel:(c)=>{
              const x = DATA.campaigns[c.dataIndex];
              return `Channel: ${x.channel}\nCost: ${fmtCost(x.cost)}`;
            },
          }},
      },
      scales:{
        x:{ticks:{color:"#8b949e"}, grid:{color:"rgba(255,255,255,0.06)"}},
        y:{ticks:{color:"#e6edf3", font:{size:11}, autoSkip:false}, grid:{display:false}},
      },
    },
  });
}

function renderRetention(){
  const r = DATA.retention;
  document.getElementById("retNote").textContent =
    `Avg of ${r.cohort_count} matured cohorts (excl. ${r.excluded_days.length} most-recent days)`;
  if (typeof Chart === "undefined"){ chartFallback("retChart"); return; }
  const rctx = document.getElementById("retChart").getContext("2d");
  const fill = rctx.createLinearGradient(0, 0, 0, 340);
  fill.addColorStop(0, "rgba(163,113,245,0.42)");
  fill.addColorStop(1, "rgba(110,64,201,0.02)");
  new Chart(rctx, {
    type:"line",
    data:{labels:r.labels, datasets:[{
      label:"Retention", data:r.values.map(v=>+(v*100).toFixed(2)),
      borderColor:"#a371f7", backgroundColor:fill, borderWidth:2.5,
      fill:true, tension:0.35,
      pointRadius:4, pointHoverRadius:6, pointBackgroundColor:"#a371f7",
      pointBorderColor:"#0a0c11", pointBorderWidth:2,
    }]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:"rgba(14,16,24,0.96)", borderColor:"rgba(255,255,255,0.10)", borderWidth:1,
          cornerRadius:9, padding:11, titleColor:"#edf1f7", bodyColor:"#9aa4b2", displayColors:false,
          callbacks:{label:(c)=>` ${c.parsed.y.toFixed(1)}% retained`}},
      },
      scales:{
        x:{ticks:{color:"#e6edf3"}, grid:{color:"rgba(255,255,255,0.06)"}},
        y:{ticks:{color:"#8b949e", callback:v=>v+"%"}, grid:{color:"rgba(255,255,255,0.06)"}, beginAtZero:true},
      },
    },
  });
}

function renderMeta(){
  const m = DATA.meta;
  const tb = document.querySelector("#metaTable tbody");
  if (!m || !m.campaigns || !m.campaigns.length){
    document.getElementById("metaNote").textContent = "No active Meta campaigns this period";
    tb.innerHTML = `<tr><td colspan="5" class="muted">No Meta campaign data for this period.</td></tr>`;
    return;
  }
  m.campaigns.forEach(c => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="ch">${esc(c.campaign)}</td>` +
      `<td>${fmtMoney(c.spend)}</td>` +
      `<td class="muted">${fmtInt(c.impressions)}</td>` +
      `<td class="muted">${fmtInt(c.clicks)}</td>` +
      `<td class="num-good">${fmtInt(c.installs)}</td>`;
    tb.appendChild(tr);
  });
  const t = m.totals;
  document.getElementById("metaNote").textContent =
    `${m.campaigns.length} active campaigns  ·  ${fmtMoney(t.spend)} spend  ·  ${fmtInt(t.installs)} installs`;
}

function renderInsights(){
  const g = document.getElementById("insights");
  const ICON = {positive:"▲", warning:"⚠", neutral:"●"};
  if (!DATA.insights || !DATA.insights.length){
    g.innerHTML = '<div class="ins-card"><div class="ins-detail">Insights unavailable for this build.</div></div>';
  } else {
    g.innerHTML = DATA.insights.map((c, i) => {
      const crit = (i === DATA.insights_critical_index) ? "critical" : "";
      const head = c.headline || c.detail;
      const detail = c.headline ? c.detail : "";
      return `<div class="ins-card ${crit}">
        <div class="ins-ico ins-${c.type}">${ICON[c.type] || "●"}</div>
        <div>
          <div class="ins-head">${esc(head)}</div>
          ${detail ? `<div class="ins-detail">${esc(detail)}</div>` : ""}
        </div>
      </div>`;
    }).join("");
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
    document.getElementById("syncTime").textContent = DATA.last_updated || "—";
    document.getElementById("builtAt").textContent = DATA.generated_at || "—";
    renderKPIs();
    renderChannels();
    renderCampaigns();
    renderRetention();
    renderMeta();
    renderInsights();
    console.log("Dashboard rendered OK:",
      DATA.channels.length, "channels,",
      DATA.campaigns.length, "campaigns,",
      DATA.retention.cohort_count, "retention cohorts,",
      (DATA.insights||[]).length, "insights");
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
