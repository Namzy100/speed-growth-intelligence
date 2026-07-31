"""Shared "back to the hub" link — one implementation for all five tools.

WHY THIS IS SHARED RATHER THAN PER-BUILDER. It used to be per-builder, and it
silently regressed. On 2026-07-22 the link was added to all five pages (309d4eb) and
deployed (PR #15), but only `build_merchant_dashboard.py` carried the change in its
BUILDER on origin/main. The other four got built HTML only, so the next daily sync
regenerated those pages from the un-updated scripts and quietly removed the link
again. By 2026-07-31 four of the five live tools had no way back to the hub, and
nothing failed — the pages just lost a control.

That is the exact failure CLAUDE.md warns about (ship the build-script change, not
just the built HTML). Making the link a post-render injection off the SAME single
call site the Ask panel already uses removes the per-page copy that drifted, so
"all five have it" is true by construction rather than by five files agreeing.

Injected from ask_panel.inject, following the precedent set there for
list_collapse: both exist so a reader can move around the page, and neither is worth
five more builder edits.
"""


def css() -> str:
    """Pill styling. Ported verbatim from the merchant tool's surviving copy, so the
    four pages that lost it come back looking identical to the one that kept it."""
    return """
  /* Hub back-link (shared — pipelines/hub_link.py) */
  .hub-link{font-size:12.5px; font-weight:650; color:var(--accent-2); text-decoration:none;
    background:rgba(47,93,251,0.10); border:1px solid var(--hairline-strong);
    padding:5px 12px; border-radius:999px; transition:border-color .15s, transform .15s;
    white-space:nowrap;}
  .hub-link:hover{border-color:var(--accent); transform:translateX(-2px);}
  .hub-wrap{display:inline-flex; align-items:center; gap:16px; flex-wrap:wrap;}"""


_ANCHOR = '<div class="brand">'
_LINK = '<a class="hub-link" href="index.html">← Tools</a>'


def inject(html: str, label: str = "← Tools") -> str:
    """Put the back-link immediately before the page's brand mark.

    No-op when the page already renders one (merchant builds its own inline, inside a
    `.brand-left` flex wrapper) so this cannot produce two. Raises on a missing
    anchor rather than returning a page without the control — silently shipping a
    tool with no way back to the hub is the bug this module exists to stop.
    """
    if "hub-link" in html:
        return html
    if _ANCHOR not in html:
        raise ValueError("hub_link.inject: no '<div class=\"brand\">' anchor found")
    if "</style>" not in html:
        raise ValueError("hub_link.inject: no </style> anchor found")
    link = f'<a class="hub-link" href="index.html">{label}</a>'
    html = html.replace("</style>", css() + "\n</style>", 1)
    # Wrapped so the pill and the brand mark sit on one row, matching merchant.
    return html.replace(_ANCHOR,
                        f'<div class="hub-wrap">{link}{_ANCHOR}', 1) \
               .replace("Speed Wallet</div>", "Speed Wallet</div></div>", 1)
