"""EU-market creator discovery — Germany, UK, Portugal.

Closes the "our creator DB is US-focused" gap flagged in the EU market research
(Germany 2nd-largest global install market on ~$10 incidental spend; UK + PT
organic-only with better retention than the US). Real discovery, structured to
work TODAY without Anthropic credits and refine automatically once they're back.

  * YouTube — real region targeting via the fetcher's new region_code +
    relevance_language (DE/de, GB/en, PT/pt). Gives real creator_country from
    snippet.country at save time.
  * TikTok / X — term-based only (no regionCode on those platforms), so market
    targeting relies on LANGUAGE in the search terms. Works well for German/
    Portuguese content; UK (English) can't be cleanly separated from US on
    TikTok/X, so those are best-effort and noted as such.

Scoring uses the DETERMINISTIC path (CreatorScorer(use_llm_fallback=False)): real
engagement, reach, and keyword audience-fit — no API. The subtler calls
(is_influencer, precise segment) use the keyword fallback, so EVERY creator in
this batch is tagged for LLM refinement once credits return (see MARKER_TAG).

Additive-only: creators already in the DB (by name+platform) are skipped, not
overwritten. Dry-run by default; --write persists.

Run from repo root:
  python creators/eu_discovery.py            # dry-run (real API calls, no DB write)
  python creators/eu_discovery.py --write     # persist new EU creators to Supabase
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from creators import database
from creators.scorer import CreatorScorer
from creators.youtube import YouTubeCreatorFetcher, QuotaExceededError
from creators.apify_tiktok import TikTokCreatorFetcher
from creators.apify_x import XCreatorFetcher

MARKER_TAG = "eu_discovery_2026_07"   # batch id AND the pending-LLM-refinement scope

# Scoped per-market config (kept small to bound YouTube quota + Apify spend).
MARKETS = {
    "DE": {"region": "DE", "lang": "de",
           "yt": ["Bitcoin investieren", "Krypto Wallet Deutschland"],
           "tt": ["krypto deutschland"], "x": ["bitcoin krypto deutschland"]},
    "GB": {"region": "GB", "lang": "en",
           "yt": ["UK crypto investing", "bitcoin fintech UK"],
           "tt": ["uk crypto"], "x": ["uk crypto fintech"]},
    "PT": {"region": "PT", "lang": "pt",
           "yt": ["investir em Bitcoin Portugal", "criptomoedas Portugal"],
           "tt": ["criptomoedas portugal"], "x": ["bitcoin portugal cripto"]},
}
_YT_MAX = 8
_TT_PER = 12
_X_PER = 15


def _dedup_key(c: dict) -> tuple:
    return (str(c.get("name", "")).strip().lower(), c.get("platform"))


def run(write: bool = False, log=print) -> dict:
    if not os.getenv("APIFY_API_KEY") or not os.getenv("YOUTUBE_API_KEY"):
        log("WARN: APIFY_API_KEY / YOUTUBE_API_KEY may be missing — some platforms will be skipped.")
    yt = YouTubeCreatorFetcher(os.getenv("YOUTUBE_API_KEY")) if os.getenv("YOUTUBE_API_KEY") else None
    tt = TikTokCreatorFetcher(os.getenv("APIFY_API_KEY")) if os.getenv("APIFY_API_KEY") else None
    xf = XCreatorFetcher(os.getenv("APIFY_API_KEY")) if os.getenv("APIFY_API_KEY") else None
    scorer = CreatorScorer(use_llm_fallback=False)   # deterministic, no Anthropic

    existing = {(_dedup_key(r)) for r in database.get_all_creators()}
    log(f"{len(existing)} creators already in DB (skipped if rediscovered).\n")

    found: dict[tuple, dict] = {}          # dedup within this run
    per = {}                               # per-market/platform tallies

    for mkt, cfg in MARKETS.items():
        per[mkt] = {"YouTube": 0, "TikTok": 0, "X": 0, "skipped_existing": 0}
        log(f"=== {mkt} (region={cfg['region']} lang={cfg['lang']}) ===")

        # --- YouTube (real region + language) ---
        if yt:
            for term in cfg["yt"]:
                try:
                    got = yt.search(term, max_results=_YT_MAX,
                                    region_code=cfg["region"], relevance_language=cfg["lang"])
                    log(f"  YouTube '{term}': {len(got)} channels")
                    _ingest(got, mkt, existing, found, per)
                except QuotaExceededError:
                    log("  [quota] YouTube daily quota exhausted — stopping YouTube."); break
                except Exception as e:
                    log(f"  YouTube '{term}' failed: {str(e)[:90]}")

        # --- TikTok (term/language) ---
        if tt:
            for term in cfg["tt"]:
                try:
                    got = tt.search([term], results_per_query=_TT_PER)
                    log(f"  TikTok '{term}': {len(got)} creators")
                    _ingest(got, mkt, existing, found, per)
                except Exception as e:
                    log(f"  TikTok '{term}' failed: {str(e)[:90]}")

        # --- X (term/language) ---
        if xf:
            for term in cfg["x"]:
                try:
                    got = xf.search([term], results_per_query=_X_PER)
                    log(f"  X '{term}': {len(got)} creators")
                    _ingest(got, mkt, existing, found, per)
                except Exception as e:
                    log(f"  X '{term}' failed: {str(e)[:90]}")
        log("")

    # Score (deterministic) + tag + optionally save
    saved, rows = 0, []
    for (key, c) in found.items():
        try:
            score = scorer.score(c)
        except Exception as e:
            log(f"  score failed for {c.get('name')}: {str(e)[:80]}"); continue
        # marker tag added AFTER scoring so it can never affect audience_fit;
        # per-market suffix keeps the set queryable by market.
        c.setdefault("niche_tags", [])
        c["niche_tags"] = list(dict.fromkeys([*c["niche_tags"], f"{MARKER_TAG}_{c['_eu_market']}"]))
        rows.append((c, score))
        if write:
            try:
                database.save_creator(c, score)
                saved += 1
            except Exception as e:
                log(f"  save failed for {c.get('name')}: {str(e)[:80]}")

    _report(rows, per, saved, write, log)
    return {"rows": rows, "per_market": per, "saved": saved, "write": write}


def _ingest(creators, mkt, existing, found, per):
    for c in creators:
        key = _dedup_key(c)
        if key in existing:
            per[mkt]["skipped_existing"] += 1
            continue
        if key in found:
            continue
        c["_eu_market"] = mkt
        found[key] = c
        per[mkt][c.get("platform", "?")] = per[mkt].get(c.get("platform", "?"), 0) + 1


def _fmt_range(vals):
    vals = [v for v in vals if v is not None]
    return f"{min(vals):,}–{max(vals):,}" if vals else "n/a"


def _report(rows, per, saved, write, log):
    log("=" * 62)
    log(f"EU DISCOVERY {'(WRITTEN)' if write else '(DRY-RUN — nothing saved)'}")
    log("=" * 62)
    log(f"New EU creators found: {len(rows)}")
    for mkt, t in per.items():
        newn = t.get("YouTube", 0) + t.get("TikTok", 0) + t.get("X", 0)
        log(f"  {mkt}: {newn} new  (YT {t.get('YouTube',0)} · TikTok {t.get('TikTok',0)} · "
            f"X {t.get('X',0)}; skipped {t['skipped_existing']} already in DB)")

    if rows:
        foll = [c.get("followers", 0) or 0 for c, _ in rows]
        comp = [s.get("composite_score", 0) or 0 for _, s in rows]
        eq = [c.get("engagement_quality") for c, _ in rows if c.get("engagement_quality") is not None]
        log(f"\n  followers range: {_fmt_range(foll)}")
        log(f"  composite score range: {min(comp):.1f}–{max(comp):.1f}")
        log(f"  engagement_quality range: {_fmt_range(eq)}")
        from collections import Counter
        from creators.database import derive_creator_country
        ccs = Counter(derive_creator_country(c.get("niche_tags"), c.get("channel_country"))
                      for c, _ in rows)
        log(f"  creator_country (real — YouTube snippet.country / tags; 'unknown' = "
            f"no country signal, mostly TikTok/X): {dict(ccs)}")
        log(f"  platforms: {dict(Counter(c.get('platform') for c,_ in rows))}")
        log(f"  segment (keyword-fallback, pending LLM): {dict(Counter(s.get('segment_tag') for _,s in rows))}")
        log(f"  is_influencer (keyword-fallback, pending LLM): {dict(Counter(s.get('is_influencer') for _,s in rows))}")

    log(f"\n  ALL {len(rows)} are tagged '{MARKER_TAG}_*' and scored deterministically "
        f"(use_llm_fallback=False) → the exact scoped list to re-run through the LLM "
        f"classifier once Anthropic credits are back.")
    if write:
        log(f"  Saved to Supabase: {saved}")


if __name__ == "__main__":
    run(write="--write" in sys.argv[1:])
