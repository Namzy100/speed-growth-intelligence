"""Build the Strategy & Market Intelligence dashboard (docs/strategy_dashboard.html).

A self-contained, presentation-grade HTML page for leadership. Uses Claude
(claude-sonnet-4-6) to extract and condense the strategy source docs into clean
structured JSON per section, then renders that into the dark-theme template
(matches creative_dashboard.html / creator_dashboard.html).

Sources (latest by filename date):
  - docs/eu_market_analysis_*.txt        -> EU Market Priority
  - docs/eu_channel_strategy_*.txt       -> Per-Market Channel Strategy
  - docs/competitor_influencer_analysis_*.txt
    + data/processed/competitor_analysis_{robinhood,crypto.com,kraken}.json
                                         -> Competitive White Space
  - docs/fintech_marketing_strategies.txt -> High-Leverage Tactics

Run from repo root:  python pipelines/build_strategy_dashboard.py
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

_DOCS = _ROOT / "docs"
_PROCESSED = _ROOT / "data" / "processed"
_OUT = _DOCS / "strategy_dashboard.html"
_MODEL = "claude-sonnet-4-6"

# Competitors to assess for the white-space matrix.
_COMPETITORS = [
    ("Robinhood", "competitor_analysis_robinhood.json"),
    ("Crypto.com", "competitor_analysis_crypto.com.json"),
    ("Kraken", "competitor_analysis_kraken.json"),
]


# ------------------------------------------------------------------
# Source readers
# ------------------------------------------------------------------

def _latest(pattern: str) -> Path | None:
    files = sorted(_DOCS.glob(pattern))
    return files[-1] if files else None


def _read(path: Path | None) -> str:
    if path and path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _competitor_summaries() -> str:
    """Compact block of each competitor's messaging summary/angles/CTAs."""
    blocks = []
    for name, fname in _COMPETITORS:
        path = _PROCESSED / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ma = data.get("messaging_analysis", {}) or {}
        blocks.append(
            f"{name} (country={data.get('country', '?')}, {data.get('total_ads', '?')} ads):\n"
            f"  summary: {ma.get('summary', '')}\n"
            f"  messaging_angles: {ma.get('messaging_angles', [])[:5]}\n"
            f"  fees_messaging: {ma.get('fees_messaging', '')}"
        )
    return "\n\n".join(blocks)


# ------------------------------------------------------------------
# Claude extraction
# ------------------------------------------------------------------

def _claude_json(client: Anthropic, instruction: str, source: str) -> dict:
    """Ask Claude to return ONLY a JSON object; parse it (one retry on bad JSON)."""
    base = (
        f"{instruction}\n\n"
        "Return ONLY a single valid JSON object — no markdown fences, no prose.\n\n"
        "--- SOURCE ---\n" + source + "\n--- END SOURCE ---"
    )
    for attempt in range(2):
        msg = base if attempt == 0 else base + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": msg}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        try:
            return json.loads(text[text.find("{"): text.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError):
            continue
    raise RuntimeError(f"Claude did not return valid JSON for: {instruction[:60]}...")


def extract_sections(client: Anthropic) -> dict:
    eu_market = _read(_latest("eu_market_analysis_*.txt"))
    eu_channel = _read(_latest("eu_channel_strategy_*.txt"))
    comp_doc = _read(_latest("competitor_influencer_analysis_*.txt"))
    tactics_doc = _read(_DOCS / "fintech_marketing_strategies.txt")

    print("  extracting: EU market priority...")
    eu = _claude_json(client, (
        "From this EU market prioritization memo, extract the TOP 3 markets to enter first, in rank order. "
        "For each: the market name, the key install/demand number (short string, e.g. '1,657 organic installs'), "
        "and a single tight one-line rationale (<=22 words). "
        'JSON shape: {"markets":[{"rank":1,"name":"","metric":"","rationale":""}, ...exactly 3]}'
    ), eu_market)

    print("  extracting: per-market channel strategy...")
    channel = _claude_json(client, (
        "From this EU channel strategy playbook, extract for EACH of Germany, United Kingdom, and Portugal: "
        "the single top channel (short), the single best messaging angle (<=18 words), and the first creator "
        "segment to target (short). "
        'JSON shape: {"markets":[{"name":"Germany","top_channel":"","messaging_angle":"","first_segment":""}, ...3]}'
    ), eu_channel)

    print("  extracting: competitive white space...")
    whitespace = _claude_json(client, (
        "Using the competitor synthesis doc plus the per-competitor messaging summaries, assess Robinhood, "
        "Crypto.com, and Kraken on THREE Speed differentiators: (1) zero-fee remittance / corridor messaging, "
        "(2) iGaming instant deposits, (3) literal transaction speed as identity. For each competitor and each "
        "differentiator, set touches=true ONLY if they actively use that angle; otherwise false. Also give each "
        "competitor a 6-10 word 'focus' summary of what they emphasize instead. Then write one punchy headline "
        "(<=16 words) naming Speed's biggest uncontested opportunity, and list the uncontested angles. "
        'JSON shape: {"headline":"","axes":["Zero-fee remittance","iGaming instant deposits","Literal speed"],'
        '"competitors":[{"name":"Robinhood","focus":"","touches":[false,false,false]}, ...3],'
        '"uncontested":["",""]}'
    ), comp_doc + "\n\n=== COMPETITOR MESSAGING SUMMARIES ===\n" + _competitor_summaries())

    print("  extracting: high-leverage tactics...")
    tactics = _claude_json(client, (
        "From this growth report, select the 4 HIGHEST-LEVERAGE, most underused tactics for Speed Wallet. "
        "For each: a short punchy title (<=7 words), a one-word category "
        "(Remittance / iGaming / Crypto / Referral / Guerrilla), and a 'why it works' line (<=24 words). "
        'JSON shape: {"tactics":[{"title":"","category":"","why":""}, ...exactly 4]}'
    ), tactics_doc)

    return {
        "eu": eu, "channel": channel, "whitespace": whitespace, "tactics": tactics,
        "sources": [
            (_latest("eu_market_analysis_*.txt") or Path("eu_market_analysis")).name,
            (_latest("eu_channel_strategy_*.txt") or Path("eu_channel_strategy")).name,
            (_latest("competitor_influencer_analysis_*.txt") or Path("competitor_influencer_analysis")).name,
            "fintech_marketing_strategies.txt",
            "data/processed/competitor_analysis_*.json",
        ],
    }


# ------------------------------------------------------------------
# Render (server-side; no client JS needed)
# ------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _render_eu(eu: dict) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    cards = []
    for m in sorted(eu.get("markets", []), key=lambda x: x.get("rank", 9))[:3]:
        rank = m.get("rank", "")
        cards.append(f"""
      <div class="rank-card">
        <div class="rank-head"><span class="medal">{medals.get(rank, '')}</span>
          <span class="rank-no">Rank {_e(rank)}</span></div>
        <div class="rank-name">{_e(m.get('name'))}</div>
        <div class="rank-metric">{_e(m.get('metric'))}</div>
        <div class="rank-rationale">{_e(m.get('rationale'))}</div>
      </div>""")
    return f'<div class="rank-grid">{"".join(cards)}</div>'


def _render_channel(channel: dict) -> str:
    cols = []
    for m in channel.get("markets", [])[:3]:
        cols.append(f"""
      <div class="strat-col">
        <div class="strat-market">{_e(m.get('name'))}</div>
        <div class="strat-row"><div class="strat-k">Top channel</div><div class="strat-v">{_e(m.get('top_channel'))}</div></div>
        <div class="strat-row"><div class="strat-k">Messaging angle</div><div class="strat-v">{_e(m.get('messaging_angle'))}</div></div>
        <div class="strat-row"><div class="strat-k">First creator segment</div><div class="strat-v">{_e(m.get('first_segment'))}</div></div>
      </div>""")
    return f'<div class="strat-grid">{"".join(cols)}</div>'


def _render_whitespace(ws: dict) -> str:
    axes = ws.get("axes", [])
    head = "".join(f"<th>{_e(a)}</th>" for a in axes)
    rows = []
    for c in ws.get("competitors", []):
        cells = []
        for t in c.get("touches", []):
            if t:
                cells.append('<td class="mx contested" title="actively competing">● Competing</td>')
            else:
                cells.append('<td class="mx open" title="not touching this angle">○ Open</td>')
        rows.append(
            f'<tr><td class="comp-name">{_e(c.get("name"))}'
            f'<span class="comp-focus">{_e(c.get("focus"))}</span></td>{"".join(cells)}</tr>'
        )
    chips = "".join(f'<span class="ws-chip">✓ {_e(u)}</span>' for u in ws.get("uncontested", []))
    return f"""
    <div class="ws-banner"><span class="ws-flag">UNCONTESTED TERRITORY</span>
      <div class="ws-headline">{_e(ws.get('headline'))}</div></div>
    <div class="table-wrap"><table class="ws-table">
      <thead><tr><th>Competitor</th>{head}</tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
    <div class="ws-legend"><span class="lg open">○ Open = no competitor presence (Speed's lane)</span>
      <span class="lg contested">● Competing = already contested</span></div>
    <div class="ws-chips">{chips}</div>"""


def _render_tactics(tactics: dict) -> str:
    cards = []
    for t in tactics.get("tactics", []):
        cards.append(f"""
      <div class="tac-card">
        <span class="tac-cat">{_e(t.get('category'))}</span>
        <div class="tac-title">{_e(t.get('title'))}</div>
        <div class="tac-why">{_e(t.get('why'))}</div>
      </div>""")
    return f'<div class="tac-grid">{"".join(cards)}</div>'


def render(data: dict) -> str:
    """Fill the template by replacing /*__NAME__*/ comment placeholders.

    Uses str.replace (not str.format) so the CSS braces in the template need no
    escaping — matching build_creative_dashboard.py / build_creator_dashboard.py
    and removing the risk that a future un-doubled CSS brace crashes the build.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sources = " · ".join(_e(s) for s in data["sources"])
    replacements = {
        "/*__SYNC__*/": now,
        "/*__EU__*/": _render_eu(data["eu"]),
        "/*__CHANNEL__*/": _render_channel(data["channel"]),
        "/*__WHITESPACE__*/": _render_whitespace(data["whitespace"]),
        "/*__TACTICS__*/": _render_tactics(data["tactics"]),
        "/*__SOURCES__*/": sources,
        "/*__GENDATE__*/": now,
    }
    html_out = _TEMPLATE
    for placeholder, value in replacements.items():
        html_out = html_out.replace(placeholder, value)
    return html_out


# ------------------------------------------------------------------
# Template
# ------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speed Wallet — Strategy &amp; Market Intelligence</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1b2230;
    --hairline:rgba(255,255,255,0.09); --hairline-strong:rgba(255,255,255,0.16);
    --text:#edf1f7; --muted:#9aa4b2; --faint:#6b7585;
    --accent:#2f5dfb; --accent-2:#6f9dff;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149; --gold:#f5c400;
    --grad:linear-gradient(120deg,#2f5dfb,#6f9dff);
    --shadow:0 10px 30px -14px rgba(0,0,0,0.7);
    --shadow-lift:0 18px 44px -16px rgba(0,0,0,0.8);
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
  .wrap{position:relative; z-index:1; max-width:1180px; margin:0 auto; padding:0 24px 90px;}

  .brandbar{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 0; border-bottom:1px solid var(--hairline);}
  .brand{font-weight:760; font-size:16px; display:flex; align-items:center;}
  .brand .bolt{margin-right:8px; font-size:17px; background:linear-gradient(180deg,#f5c400,#f0a02a);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; filter:drop-shadow(0 0 6px rgba(240,160,42,0.45));}
  .brandbar .sync{font-size:12px; color:var(--muted);} .brandbar .sync b{color:var(--text); font-weight:600;}

  .title-block{margin:36px 0 26px;}
  h1{font-size:30px; margin:0 0 6px; font-weight:790; letter-spacing:-0.03em;
    background:linear-gradient(180deg,#ffffff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .title-block .sub{color:var(--muted); font-size:13.5px;}

  section{margin:38px 0;}
  .sec-head{display:flex; align-items:center; gap:11px; margin-bottom:18px; flex-wrap:wrap;}
  h2{font-size:12.5px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted); margin:0; font-weight:700; display:flex; align-items:center; gap:10px;}
  h2::before{content:""; width:3px; height:13px; border-radius:2px; background:var(--grad);}
  .sec-note{font-size:12px; color:var(--faint);}

  /* Section 2 — EU rank cards */
  .rank-grid{display:grid; grid-template-columns:repeat(3,1fr); gap:16px;}
  @media(max-width:820px){.rank-grid{grid-template-columns:1fr;}}
  .rank-card{padding:20px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55));
    border:1px solid var(--hairline); border-radius:var(--r-lg); box-shadow:var(--shadow); position:relative; overflow:hidden;}
  .rank-card::before{content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--grad);}
  .rank-head{display:flex; align-items:center; gap:9px; margin-bottom:8px;}
  .medal{font-size:20px;}
  .rank-no{font-size:10px; text-transform:uppercase; letter-spacing:0.09em; color:var(--faint); font-weight:700;}
  .rank-name{font-size:22px; font-weight:770; letter-spacing:-0.02em;}
  .rank-metric{display:inline-block; margin:10px 0 12px; font-size:12.5px; font-weight:680; color:var(--gold);
    background:rgba(240,160,42,0.12); border:1px solid rgba(240,160,42,0.28); padding:3px 10px; border-radius:20px;}
  .rank-rationale{font-size:13.5px; color:var(--muted); line-height:1.55;}

  /* Section 3 — channel strategy columns */
  .strat-grid{display:grid; grid-template-columns:repeat(3,1fr); gap:16px;}
  @media(max-width:820px){.strat-grid{grid-template-columns:1fr;}}
  .strat-col{background:var(--panel); border:1px solid var(--hairline); border-radius:var(--r-md); padding:18px; box-shadow:var(--shadow);}
  .strat-market{font-size:16px; font-weight:740; letter-spacing:-0.01em; padding-bottom:11px; margin-bottom:11px; border-bottom:1px solid var(--hairline);
    background:linear-gradient(180deg,#fff,#c9c3e8); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
  .strat-row{margin:12px 0;}
  .strat-k{font-size:9.5px; text-transform:uppercase; letter-spacing:0.08em; color:var(--faint); font-weight:700; margin-bottom:3px;}
  .strat-v{font-size:13px; color:var(--text); line-height:1.5;}

  /* Section 4 — white space matrix */
  .ws-banner{display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:16px 20px; margin-bottom:16px;
    background:linear-gradient(120deg,rgba(63,185,80,0.12),rgba(47,93,251,0.10)); border:1px solid rgba(63,185,80,0.3); border-radius:var(--r-md);}
  .ws-flag{font-size:10px; font-weight:800; letter-spacing:0.12em; color:var(--good); background:rgba(63,185,80,0.16); padding:5px 11px; border-radius:6px; white-space:nowrap;}
  .ws-headline{font-size:16px; font-weight:700; letter-spacing:-0.01em;}
  .table-wrap{overflow-x:auto; border:1px solid var(--hairline); border-radius:var(--r-md); background:var(--panel);}
  .ws-table{width:100%; border-collapse:collapse; font-size:13px; min-width:640px;}
  .ws-table th,.ws-table td{padding:13px 15px; border-bottom:1px solid var(--hairline); text-align:center;}
  .ws-table th{background:#10151d; color:var(--faint); font-size:10px; text-transform:uppercase; letter-spacing:0.06em; font-weight:700;}
  .ws-table th:first-child,.ws-table td:first-child{text-align:left;}
  .ws-table tbody tr:last-child td{border-bottom:none;}
  .comp-name{font-weight:700; font-size:14px;}
  .comp-focus{display:block; font-size:11px; color:var(--faint); font-weight:500; margin-top:3px; max-width:220px;}
  td.mx{font-weight:700; font-variant-numeric:tabular-nums;}
  td.mx.open{color:var(--good); background:rgba(63,185,80,0.07);}
  td.mx.contested{color:var(--faint);}
  .ws-legend{display:flex; gap:18px; flex-wrap:wrap; margin-top:11px; font-size:11.5px;}
  .lg.open{color:var(--good);} .lg.contested{color:var(--faint);}
  .ws-chips{display:flex; gap:9px; flex-wrap:wrap; margin-top:14px;}
  .ws-chip{font-size:12px; font-weight:650; color:var(--good); background:rgba(63,185,80,0.1); border:1px solid rgba(63,185,80,0.28); padding:5px 12px; border-radius:20px;}

  /* Section 5 — tactics cards */
  .tac-grid{display:grid; grid-template-columns:repeat(2,1fr); gap:16px;}
  @media(max-width:760px){.tac-grid{grid-template-columns:1fr;}}
  .tac-card{padding:18px; background:linear-gradient(180deg,var(--panel),rgba(22,27,34,0.55));
    border:1px solid var(--hairline); border-radius:var(--r-lg); box-shadow:var(--shadow); transition:transform .2s ease,border-color .2s ease;}
  .tac-card:hover{transform:translateY(-3px); border-color:var(--hairline-strong);}
  .tac-cat{display:inline-block; font-size:9.5px; font-weight:800; letter-spacing:0.09em; text-transform:uppercase;
    color:var(--accent-2); background:rgba(111,157,255,0.13); border:1px solid rgba(111,157,255,0.3); padding:3px 9px; border-radius:20px;}
  .tac-title{font-size:16.5px; font-weight:740; letter-spacing:-0.015em; margin:11px 0 8px;}
  .tac-why{font-size:13px; color:var(--muted); line-height:1.55;}

  footer{margin-top:50px; padding-top:18px; border-top:1px solid var(--hairline); font-size:11.5px; color:var(--faint); line-height:1.7;}
  footer b{color:var(--muted); font-weight:600;}
  @media (prefers-reduced-motion: reduce){*{transition:none!important;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandbar">
    <div class="brand"><span class="bolt">⚡</span>Speed Wallet</div>
    <div class="sync">Synced: <b>/*__SYNC__*/</b></div>
  </div>

  <div class="title-block">
    <h1>Strategy &amp; Market Intelligence</h1>
    <div class="sub">EU expansion priorities, per-market playbooks, and competitive white space — synthesized from Speed's market &amp; competitor research.</div>
  </div>

  <section>
    <div class="sec-head"><h2>EU Market Priority</h2><span class="sec-note">Ranked entry order from install demand &amp; corridor fit</span></div>
    /*__EU__*/
  </section>

  <section>
    <div class="sec-head"><h2>Per-Market Channel Strategy</h2><span class="sec-note">Top channel · messaging angle · first creator segment</span></div>
    /*__CHANNEL__*/
  </section>

  <section>
    <div class="sec-head"><h2>Competitive White Space</h2><span class="sec-note">Where Robinhood, Crypto.com &amp; Kraken are absent</span></div>
    /*__WHITESPACE__*/
  </section>

  <section>
    <div class="sec-head"><h2>High-Leverage Marketing Tactics</h2><span class="sec-note">Highest-leverage, underused growth plays</span></div>
    /*__TACTICS__*/
  </section>

  <footer>
    <b>Data sources:</b> /*__SOURCES__*/<br>
    Generated /*__GENDATE__*/ · Sections extracted &amp; condensed by Claude (claude-sonnet-4-6) · rebuilt by pipelines/build_strategy_dashboard.py
  </footer>
</div>
</body>
</html>
"""


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def main() -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY must be set in .env")
    print("Extracting strategy sections via Claude (claude-sonnet-4-6)...")
    client = Anthropic(api_key=api_key)
    data = extract_sections(client)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(render(data), encoding="utf-8")
    print(f"Wrote {_OUT.relative_to(_ROOT)} ({_OUT.stat().st_size:,} bytes)")
    print(f"  EU markets: {len(data['eu'].get('markets', []))} · "
          f"channel cols: {len(data['channel'].get('markets', []))} · "
          f"competitors: {len(data['whitespace'].get('competitors', []))} · "
          f"tactics: {len(data['tactics'].get('tactics', []))}")


if __name__ == "__main__":
    main()
