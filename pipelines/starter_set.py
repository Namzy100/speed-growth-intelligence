"""Shared "start here" strip — a small curated set at the top of each tool.

WHY THIS EXISTS. Every tool opens on its full list (696 creators, 93 venues, …).
That is the right thing to hold, but it is the wrong thing to *land on*: a reader
with no filter in mind has no idea which row deserves the first minute. This puts a
short, ranked, reasoned shortlist above the list, so the page answers "what should I
do first" before it answers "what have you got".

ONE MODULE, NOT FIVE COPIES. Same principle as pipelines/ask_panel.py — the strip is
injected at a single anchor common to all the built pages (the first `<section>`),
so a per-builder templating scheme is not needed and the markup exists in one place.

THE RANKING IS EACH TOOL'S OWN JUDGEMENT, NOT A SHARED RULE. "First" means something
different per tool and the callers decide it (see each builder's `_starter`):
  * creator  — highest score among individuals with real scraped data, not yet contacted
  * creative — WORST paid deposit CPA, i.e. where the money is leaking (act-on-this,
               not celebrate-this)
  * trend    — highest estimated reach among concepts still at "suggested"
  * merchant — highest relevance among on-topic, URL-verified, not-yet-contacted venues
Strategy deliberately gets NO strip: it holds 3 markets / 3 channel plans / 4 tactics
/ 3 competitors, so the whole page is already the shortlist and a "first 5" would be
manufactured rather than curated. Same call, and same reasoning, as the deliberate
list-collapse exclusion for that page.

Each item is a dict: {"title", "meta", "stat", "stat_label"} — `stat` is the number
that JUSTIFIES the row being here, so the shortlist shows its own reasoning rather
than asking to be trusted.
"""


def css() -> str:
    """Strip styles. Uses each page's existing custom properties, adds no new tokens."""
    return """
  /* "Start here" strip (shared — pipelines/starter_set.py) */
  .sh-wrap{margin:0 0 22px;}
  .sh-head{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:11px;}
  .sh-head h2{margin:0; font-size:16px; font-weight:720; letter-spacing:-0.02em; color:var(--text);}
  .sh-head .sh-note{font-size:12px; color:var(--muted);}
  .sh-grid{display:grid; grid-template-columns:repeat(5,1fr); gap:10px;}
  @media(max-width:1100px){.sh-grid{grid-template-columns:repeat(2,1fr);}}
  @media(max-width:620px){.sh-grid{grid-template-columns:1fr;}}
  .sh-card{position:relative; padding:12px 13px; border:1px solid var(--hairline);
    border-radius:var(--r-sm,9px); background:var(--panel); overflow:hidden;}
  .sh-card::before{content:""; position:absolute; left:0; top:0; bottom:0; width:2px;
    background:var(--accent); opacity:.85;}
  .sh-rank{font-size:10px; font-weight:800; letter-spacing:.1em; color:var(--faint); text-transform:uppercase;}
  .sh-title{margin:3px 0 4px; font-size:13.5px; font-weight:680; color:var(--text); line-height:1.32;
    overflow-wrap:anywhere;}
  .sh-meta{font-size:11.5px; color:var(--muted); line-height:1.4;}
  .sh-stat{margin-top:7px; font-size:12px; color:var(--accent-2,var(--accent)); font-weight:650;
    font-variant-numeric:tabular-nums;}
  .sh-stat span{color:var(--faint); font-weight:600;}"""


def _e(s) -> str:
    return (str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def html_section(heading: str, note: str, items: list[dict]) -> str:
    """The strip markup. Renders nothing at all when there are no items, rather than
    an empty box — a tool with nothing worth starting on should say nothing."""
    if not items:
        return ""
    cards = []
    for n, it in enumerate(items, 1):
        stat = ""
        if it.get("stat") is not None:
            stat = (f'<div class="sh-stat">{_e(it["stat"])}'
                    f'<span> {_e(it.get("stat_label", ""))}</span></div>')
        cards.append(
            f'<div class="sh-card"><div class="sh-rank">{n}</div>'
            f'<div class="sh-title">{_e(it.get("title"))}</div>'
            f'<div class="sh-meta">{_e(it.get("meta"))}</div>{stat}</div>')
    return f"""  <section class="sh-wrap">
    <div class="sh-head"><h2>{_e(heading)}</h2><span class="sh-note">{_e(note)}</span></div>
    <div class="sh-grid">
{chr(10).join("      " + c for c in cards)}
    </div>
  </section>
"""


def inject(html: str, heading: str, note: str, items: list[dict]) -> str:
    """Splice the strip in above the page's first <section>.

    Idempotent, and a no-op when there is nothing to show. Raises if the anchor is
    missing rather than silently producing a page without the strip — a missing
    anchor means the builder's markup changed and the caller needs to know.
    """
    if not items or 'class="sh-wrap"' in html:
        return html
    if "<section" not in html:
        raise ValueError("starter_set.inject: no <section> anchor found")
    if "</style>" not in html:
        raise ValueError("starter_set.inject: no </style> anchor found")
    html = html.replace("</style>", css() + "\n</style>", 1)
    return html.replace("<section", html_section(heading, note, items) + "  <section", 1)
