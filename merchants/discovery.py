"""Outbound merchant-discovery pipeline.

Finds public places (forums, trade publications, associations/events, B2B
directories) where Speed could get visibility with decision-makers at potential
merchant businesses. Two candidate sources merge into one deduped, ranked pool:

  1. Aggregator harvest  — deterministic HTTP fetch + parse of known industry
     listicles. NO API. Runs by default.
  2. web_search discovery — Claude's built-in web_search server tool per vertical
     query. NEEDS Anthropic credits. Behind --web-search.

Relevance gate + 3-criteria score (merchant-vertical + decision-maker relevance
[gate], payments/outreach access, audience tier). The real judgment is a Claude
call (--judge); without it a deterministic keyword FALLBACK runs so the whole
pipeline works end-to-end with zero credits (degraded), exactly like
trend_pipeline's fallback. Seed venues carry human ("manual-seed") judgments.

LinkedIn communities are never attempted (see config.EXCLUDED_CHANNELS).

Run from repo root:
  python merchants/discovery.py                 # seed + harvest + fallback (NO API)
  python merchants/discovery.py --web-search     # + Claude web_search discovery (credits)
  python merchants/discovery.py --judge          # + Claude relevance re-judging (credits)
"""

import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from merchants import config

_OUT = _ROOT / "data" / "processed" / "merchant_candidates.json"
_SEARCH_MODEL = "claude-sonnet-4-6"     # web_search_20260209 needs Opus 4.6+/Sonnet 4.6
_JUDGE_MODEL = "claude-haiku-4-5"       # cheap per-venue judge (no web_search needed)
_TIER_SCORE = {"T1": 9.0, "T2": 6.0, "T3": 3.0}

# Domains that are never merchant-discovery venues (social, aggregators' own
# infra, generic) — dropped from harvested candidates before judging.
_SKIP_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "t.me", "telegram.org", "wa.me", "whatsapp.com", "tiktok.com",
    "google.com", "apple.com", "cloudflare.com", "gravatar.com", "gstatic.com",
    "googletagmanager.com", "doubleclick.net",
}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def normalize_domain(url: str) -> str:
    """Registrable-ish domain: lowercase host, strip www + leading subdomains
    down to the last two labels (good enough for dedupe here)."""
    if not url:
        return ""
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class _LinkParser(HTMLParser):
    """Collect (href, anchor-text) pairs — stdlib only (no bs4)."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text).split())[:120]))
            self._href, self._text = None, []


def _fetch(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0 (SpeedMerchantDiscovery)"})
        return r.text if r.status_code == 200 else ""
    except requests.RequestException:
        return ""


# ------------------------------------------------------------------
# 1. Aggregator harvest (NO API)
# ------------------------------------------------------------------

def harvest_aggregators(vertical: str, log=print) -> dict:
    """Fetch each seed aggregator, extract outbound venue domains + a name.
    Returns {domain: {"venue","url","cross_listing","sources"}}. Cross-listing =
    how many distinct aggregators referenced the domain (a real reputation signal)."""
    found: dict[str, dict] = {}
    for agg in config.SEED_AGGREGATORS.get(vertical, []):
        agg_domain = normalize_domain(agg)
        html = _fetch(agg)
        if not html:
            log(f"  aggregator unreachable/empty: {agg}")
            continue
        p = _LinkParser()
        try:
            p.feed(html)
        except Exception:
            pass
        seen_here = set()
        for href, text in p.links:
            if not href.startswith("http"):
                continue
            d = normalize_domain(href)
            if (not d or d == agg_domain or d in _SKIP_DOMAINS
                    or d.endswith(".gov") or len(d) < 4 or d in seen_here):
                continue
            seen_here.add(d)
            rec = found.setdefault(d, {"venue": text or d, "url": f"https://{d}",
                                       "cross_listing": 0, "sources": []})
            rec["cross_listing"] += 1
            rec["sources"].append(agg_domain)
            if text and (not rec["venue"] or rec["venue"] == d):
                rec["venue"] = text
        log(f"  harvested {len(seen_here)} outbound domains from {agg_domain}")
    return found


# ------------------------------------------------------------------
# 2. web_search discovery (NEEDS credits — behind --web-search)
# ------------------------------------------------------------------

def discover_via_web_search(vertical: str, client, log=print) -> list[dict]:
    """One Claude call per query template, using the built-in web_search tool, to
    surface candidate venues as JSON. Returns [] (logged) on any failure incl.
    exhausted credits — the pipeline still runs on seed + harvest."""
    out = []
    for q in config.QUERY_TEMPLATES.get(vertical, []):
        prompt = (
            f"Use web_search to find real, currently-live {vertical} industry venues where a "
            "Bitcoin/stablecoin PAYMENTS company could reach BUSINESS decision-makers "
            "(operators, executives, PSPs) — NOT players/consumers. Only these channel types: "
            "forum/community, trade publication, association/event, B2B directory. Do NOT include "
            "LinkedIn. Return ONLY a JSON array of "
            '{"venue","url","channel_type","reason"} (channel_type one of '
            'forum|publication|association_event|directory).\n\nQuery focus: ' + q)
        try:
            r = client.messages.create(
                model=_SEARCH_MODEL, max_tokens=2000,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            arr = json.loads(text[text.find("["): text.rfind("]") + 1])
            for c in arr:
                if c.get("channel_type") in config.CHANNEL_TYPES and c.get("url"):
                    out.append({"venue": c.get("venue", ""), "url": c["url"],
                                "channel_type": c["channel_type"], "vertical": vertical,
                                "reason": c.get("reason", ""), "source": "web_search"})
        except Exception as e:
            log(f"  web_search '{q[:40]}' failed (credits/API?): {str(e)[:100]}")
    return out


# ------------------------------------------------------------------
# 3. Relevance judge — LLM (--judge) with a deterministic fallback
# ------------------------------------------------------------------

_VERTICAL_KW = {
    "iGaming": {"igaming", "gambling", "casino", "betting", "sportsbook", "affiliate", "poker"},
    "fintech": {"fintech", "payments", "banking", "neobank", "financial"},
    "entertainment": {"entertainment", "streaming", "media", "events", "music"},
    "psp": {"payment", "acquiring", "psp", "merchant", "processing"},
}
_CONSUMER_KW = {"tips", "bonus", "free spins", "how to win", "best casino", "review"}


def _fallback_judge(cand: dict) -> dict:
    """Deterministic, no-API provisional judgment (like trend's fallback). Rough:
    keyword match on venue/url for vertical fit; gates obvious consumer/player text."""
    blob = f"{cand.get('venue','')} {cand.get('url','')} {cand.get('reason','')}".lower()
    kw = _VERTICAL_KW.get(cand.get("vertical", "iGaming"), set())
    on = any(k in blob for k in kw) and not any(k in blob for k in _CONSUMER_KW)
    return {"on_topic": on, "relevance": 5.0 if on else 1.0,
            "payments_access": 4.0 if on else 0.0,
            "reason": "keyword fallback (no LLM judgment yet)", "judge_source": "fallback"}


def judge_venue(cand: dict, client, log=print) -> dict:
    """Claude (Haiku) reads the venue's real fetched content and judges the two
    LLM criteria + the gate. Falls back to _fallback_judge on any failure."""
    content = re.sub(r"<[^>]+>", " ", _fetch(cand["url"]))[:3500] if cand.get("url") else ""
    if not content:
        return _fallback_judge(cand)
    prompt = (
        "You assess whether a website is a good OUTBOUND venue for Speed Wallet (a "
        "Bitcoin + stablecoin PAYMENTS product) to reach BUSINESS decision-makers at "
        f"potential {cand.get('vertical','')} merchant businesses (operators, executives, "
        "PSPs) — NOT players/consumers. Return ONLY JSON:\n"
        '{"on_topic": true/false,  // genuinely a target-vertical, decision-maker (B2B) venue, not player/consumer\n'
        ' "relevance": 0-10,        // vertical + decision-maker fit\n'
        ' "payments_access": 0-10,  // how well it puts a payments vendor in front of decision-makers\n'
        ' "reason": "one line"}\n\n'
        f"Venue: {cand.get('venue','')}\nURL: {cand.get('url','')}\n"
        f"Content excerpt:\n{content}")
    try:
        r = client.messages.create(model=_JUDGE_MODEL, max_tokens=200,
                                   messages=[{"role": "user", "content": prompt}])
        t = r.content[0].text.strip()
        j = json.loads(t[t.find("{"): t.rfind("}") + 1])
        return {"on_topic": bool(j.get("on_topic")),
                "relevance": max(0.0, min(10.0, float(j.get("relevance", 0)))),
                "payments_access": max(0.0, min(10.0, float(j.get("payments_access", 0)))),
                "reason": str(j.get("reason", ""))[:200], "judge_source": "llm"}
    except Exception as e:
        log(f"  judge '{cand.get('venue','')[:30]}' failed (credits/API?): {str(e)[:80]}")
        return _fallback_judge(cand)


# ------------------------------------------------------------------
# Scoring + assembly
# ------------------------------------------------------------------

def audience_tier(domain: str, cross_listing: int) -> str:
    if domain in config.KNOWN_BRANDS or cross_listing >= 3:
        return "T1"
    if cross_listing == 2:
        return "T2"
    return "T3"


def fit_score(rec: dict) -> float:
    """0.5 relevance + 0.3 payments_access + 0.2 audience; gated to 0 if off-topic.
    Audience is the tier score (reputation/cross-listing tier, NOT real traffic)."""
    if not rec.get("on_topic"):
        return 0.0
    aud = _TIER_SCORE.get(rec.get("audience_tier", "T3"), 3.0)
    return round(0.5 * rec["relevance"] + 0.3 * rec["payments_access"] + 0.2 * aud, 1)


def run(verticals=None, use_web_search=False, use_judge=False, log=print) -> dict:
    verticals = verticals or ["iGaming"]
    client = None
    if (use_web_search or use_judge) and os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        except Exception as e:
            log(f"Anthropic client unavailable ({e}); running credit-free path only.")

    by_domain: dict[str, dict] = {}

    for vertical in verticals:
        log(f"=== {vertical} ===")
        # (a) verified seed
        for s in config.SEEDS.get(vertical, []):
            d = normalize_domain(s["url"]) or f"seed:{s['venue']}"
            by_domain[d] = {**s, "domain": d, "cross_listing": 0, "on_topic": True}
        log(f"  seeded {len(config.SEEDS.get(vertical, []))} verified venues")

        # (b) aggregator harvest (NO API)
        for d, rec in harvest_aggregators(vertical, log=log).items():
            if d in by_domain:
                by_domain[d]["cross_listing"] = max(by_domain[d].get("cross_listing", 0),
                                                    rec["cross_listing"])
                continue
            by_domain[d] = {"venue": rec["venue"], "url": rec["url"], "domain": d,
                            "url_verified": True, "channel_type": None, "vertical": vertical,
                            "cross_listing": rec["cross_listing"], "source": "aggregator",
                            "outreach_mode": [], **_fallback_judge(
                                {"venue": rec["venue"], "url": rec["url"], "vertical": vertical})}

        # (c) web_search discovery (NEEDS credits)
        if use_web_search and client:
            for c in discover_via_web_search(vertical, client, log=log):
                d = normalize_domain(c["url"])
                if not d or d in by_domain:
                    continue
                by_domain[d] = {**c, "domain": d, "url_verified": True, "cross_listing": 0,
                                "outreach_mode": [], **_fallback_judge(c)}
            log("  web_search discovery done")
        elif use_web_search:
            log("  --web-search requested but no Anthropic client (credits?); skipped")

    # (d) judge (NEEDS credits) — upgrade non-manual-seed rows
    for rec in by_domain.values():
        rec["audience_tier"] = audience_tier(rec["domain"], rec.get("cross_listing", 0)) \
            if rec.get("judge_source") != "manual-seed" else rec.get("audience_tier", "T2")
        if use_judge and client and rec.get("judge_source") != "manual-seed":
            rec.update(judge_venue(rec, client, log=log))
        rec["fit_score"] = fit_score(rec)

    ranked = sorted(by_domain.values(), key=lambda r: r["fit_score"], reverse=True)
    from collections import Counter
    data = {
        "vertical_focus": verticals,
        "excluded_channels": config.EXCLUDED_CHANNELS,
        "counts": {"total": len(ranked),
                   "by_judge_source": dict(Counter(r.get("judge_source") for r in ranked)),
                   "on_topic": sum(1 for r in ranked if r.get("on_topic"))},
        "venues": ranked,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"Wrote {_OUT.relative_to(_ROOT)}: {len(ranked)} venues "
        f"(sources: {data['counts']['by_judge_source']})")
    return data


if __name__ == "__main__":
    args = sys.argv[1:]
    run(use_web_search="--web-search" in args, use_judge="--judge" in args)
