"""Collapse long data tables to a first-N preview with a "show all" control.

WHY. Every dashboard ends with its full data table, and the Ask panel sits below
that. Measured on the live pages at a 900px viewport, the panel started here:

    creator    38,201px  (42.4 screens)   696-row table = 36,324px of it
    merchant    8,187px  ( 9.1 screens)    94-row table =  7,418px
    creative    5,323px  ( 5.9 screens)    69-row table =  2,513px
    trend       4,205px  ( 4.7 screens)   no long list — five medium sections
    strategy    1,801px  ( 2.0 screens)   already fine

So the fix targets row-based tables. Trend and strategy have no long list and are
deliberately left alone rather than given a control that would do nothing.

SCOPE. A layout fix, not a redesign. The control reuses each dashboard's own
tokens and copies the established `.ask-chip` pill treatment (translucent accent
fill, 999px radius, `--accent-2` text) so it reads as part of the existing system
rather than a new component. No new colours, no new type scale, no motion library.

BEHAVIOUR NOTES that matter for these particular pages:
  * The creator and merchant tables are re-rendered by their own JS on every
    filter/sort change, so a one-shot pass would be undone the first time someone
    touches a filter. A MutationObserver watches childList only (never attributes,
    which our own row hiding would otherwise re-trigger) and re-applies.
  * A reader who expanded a table keeps it expanded across re-renders — flipping it
    back to collapsed under them on every keystroke would be worse than the problem.
  * A dashboard's own "N of M shown" counter becomes a contradiction the moment we
    draw only the first 10 of N — screenshot review caught "93 of 93 shown" sitting
    directly above ten rows. While truncated, such counters are reworded to say
    "match" (what the filters matched) and restored to "shown" on expand. Matched by
    text pattern, so no per-dashboard wiring is needed.
"""

__all__ = ["css", "js"]

_DEFAULT_LIMIT = 10


def css() -> str:
    """Control styling, built from tokens each dashboard already defines."""
    return """
  /* Collapsed long lists (shared — pipelines/list_collapse.py) */
  .lc-more{display:flex; justify-content:center; align-items:center; gap:10px; margin-top:10px;}
  .lc-btn{background:rgba(47,93,251,0.10); border:1px solid var(--hairline-strong,var(--hairline));
    color:var(--accent-2,var(--accent)); padding:5px 14px; border-radius:999px; cursor:pointer;
    font-family:inherit; font-size:12px; font-weight:650; transition:border-color .15s, filter .15s;}
  .lc-btn:hover{border-color:var(--accent); filter:brightness(1.08);}
  .lc-count{font-size:11.5px; color:var(--faint);}
  /* Fade the last visible row so a truncated table reads as truncated rather than
     as the end of the data. Only applied while collapsed. */
  .lc-clipped{position:relative;}
  .lc-clipped::after{content:""; position:absolute; left:0; right:0; bottom:0; height:38px;
    pointer-events:none; background:linear-gradient(to bottom, transparent, var(--panel));}
"""


def js(limit: int = _DEFAULT_LIMIT) -> str:
    """The collapser. ES5-safe: these pages target no build step."""
    return _ENGINE.replace("/*__LC_LIMIT__*/", str(int(limit)))


_ENGINE = r"""
/* ==========================================================================
   Collapse long tables to a preview (shared — pipelines/list_collapse.py).
   Keeps the Ask panel reachable without scrolling a multi-thousand-pixel table.
   Targets `.table-wrap > table > tbody` only: that is the shared pattern across
   these dashboards, and it means pages without a long list get no control.
   ========================================================================== */
(function(){
  var LIMIT = /*__LC_LIMIT__*/;
  var seq = 0;

  function rowsOf(tbody){
    var out = [], k;
    for (k = 0; k < tbody.children.length; k++){
      if (tbody.children[k].tagName === "TR") out.push(tbody.children[k]);
    }
    return out;
  }

  function apply(box, tbody, state){
    var rows = rowsOf(tbody), i;
    var expanded = state.expanded;
    // Nothing to do: fewer rows than the limit. Clean up any stale control so a
    // filter that narrows the set below the limit doesn't leave one behind.
    if (rows.length <= LIMIT){
      for (i = 0; i < rows.length; i++) rows[i].style.display = "";
      box.classList.remove("lc-clipped");
      if (state.ctrl) state.ctrl.style.display = "none";
      return;
    }
    for (i = 0; i < rows.length; i++){
      rows[i].style.display = (expanded || i < LIMIT) ? "" : "none";
    }
    if (expanded) box.classList.remove("lc-clipped");
    else box.classList.add("lc-clipped");

    if (!state.ctrl){
      var wrap = document.createElement("div");
      wrap.className = "lc-more";
      var btn = document.createElement("button");
      btn.className = "lc-btn";
      btn.type = "button";
      var note = document.createElement("span");
      note.className = "lc-count";
      wrap.appendChild(btn); wrap.appendChild(note);
      box.parentNode.insertBefore(wrap, box.nextSibling);
      state.ctrl = wrap; state.btn = btn; state.note = note;
      btn.addEventListener("click", function(){
        state.expanded = !state.expanded;
        apply(box, tbody, state);
        if (!state.expanded){
          // Collapsing from far down the table would otherwise leave the reader
          // stranded below the content that just disappeared.
          var y = box.getBoundingClientRect().top + window.scrollY - 90;
          window.scrollTo({top: y < 0 ? 0 : y, behavior: "smooth"});
        }
      });
    }
    state.ctrl.style.display = "";
    state.btn.textContent = expanded ? ("Show first " + LIMIT) : ("Show all " + rows.length);
    state.btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    state.note.textContent = expanded
      ? ("all " + rows.length + " shown")
      : ("showing " + LIMIT + " of " + rows.length);
    syncCounters(!expanded);
  }

  // A dashboard's own "N of M shown" counter becomes a contradiction the moment we
  // draw only the first 10 of N — the screenshot review caught "93 of 93 shown"
  // sitting directly above ten rows. Reword it to describe what MATCHED while the
  // table is truncated, and restore the original wording on expand. Matched by text
  // pattern rather than per-dashboard id, so it needs no builder-specific wiring.
  var COUNT_RE = /^\s*([\d,]+) of ([\d,]+) shown\s*$/;
  function syncCounters(truncated){
    var all = document.querySelectorAll("span,div,p,small"), i, el, m;
    for (i = 0; i < all.length; i++){
      el = all[i];
      if (el.children.length) continue;
      m = COUNT_RE.exec(el.textContent || "");
      if (m){
        if (truncated) el.textContent = m[1] + " of " + m[2] + " match";
        continue;
      }
      m = /^\s*([\d,]+) of ([\d,]+) match\s*$/.exec(el.textContent || "");
      if (m && !truncated) el.textContent = m[1] + " of " + m[2] + " shown";
    }
  }

  function attach(box){
    var tbody = box.querySelector("table > tbody");
    if (!tbody || box.getAttribute("data-lc")) return;
    box.setAttribute("data-lc", String(++seq));
    var state = {expanded: false, ctrl: null, btn: null, note: null, busy: false};
    apply(box, tbody, state);
    // These tables are redrawn by the page's own filter/sort code, which would
    // silently undo the collapse. Watch childList ONLY — observing attributes
    // would re-fire on our own row hiding.
    if (window.MutationObserver){
      var mo = new MutationObserver(function(){
        if (state.busy) return;
        state.busy = true;
        apply(box, tbody, state);
        state.busy = false;
      });
      mo.observe(tbody, {childList: true});
    }
  }

  function init(){
    var boxes = document.querySelectorAll(".table-wrap"), i;
    for (i = 0; i < boxes.length; i++) attach(boxes[i]);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
"""
