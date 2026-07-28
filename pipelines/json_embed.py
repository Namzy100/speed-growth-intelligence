"""Safe JSON embedding for the self-contained dashboards.

Every dashboard bakes its data into a `<script>` block as a JSON literal. Plain
`json.dumps` is NOT safe there: it does not escape `<`, so any attacker-influenced
string containing a closing script tag ends the element early and everything after
it is parsed as HTML. On pages served publicly from namzy100.github.io that is
stored XSS, and the strings really are third-party — scraped TikTok/YouTube
captions become the trend dashboard's "hooks", and creator display names come
straight from the platforms.

Found 2026-07-28 while assessing two CodeAnt prompt-injection findings; the scan
did not flag this one. The *rendering* paths were already escaped correctly
(`_e()` server-side, `escg()` client-side), so this was specifically the
JSON-embed hole, in four builders at once.

Why this escapes more than the closing tag: escaping `<` outright subsumes
`</script>` and also neutralises `<!--`, which can equally break out of a script
element. U+2028/U+2029 are escaped because they are legal inside a JSON string but
are line terminators in JavaScript, so unescaped they break the literal. `>` and
`&` go too, which costs nothing and removes the need to reason about context.
Every replacement is a `\\uXXXX` escape that JSON decodes back to the identical
character, so the embedded data is bit-for-bit unchanged once parsed — only the
HTML tokenizer is stopped from seeing a tag. Same hardening as Django's
`json_script`.
"""

import json

__all__ = ["dumps_for_script"]

# U+2028/U+2029 are spelled as chr() calls, not literal characters: as literals
# they are invisible in an editor and get silently mangled by anything that
# normalises whitespace. Keeping this source pure ASCII makes the rule auditable.
_SCRIPT_UNSAFE = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (chr(0x2028), "\\u2028"),
    (chr(0x2029), "\\u2029"),
)


def dumps_for_script(obj) -> str:
    """`json.dumps(obj)`, safe to paste inside an HTML <script> element."""
    out = json.dumps(obj)
    for raw, escaped in _SCRIPT_UNSAFE:
        out = out.replace(raw, escaped)
    return out
