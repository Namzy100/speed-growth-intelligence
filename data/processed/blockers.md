# Blockers & watch items

Maintained by hand. The morning brief (pipelines/morning_brief.py) surfaces
`- [BLOCKED]` lines as alerts and `- [WATCH]` lines as routine watch items.
Delete a line when it's resolved.

- [WATCH] Handover batch on `feat/handover-batch` (gh-pages deploy + morning brief + merchant vertical expansion + segment rebuild + strategy rebuild + Pipeline-Board card UX) is pushed to TrySpeed/Marketing-Agent but not merged — Jay reviews, merges, and raises the PR from `main` himself. After it lands: point GitHub Pages at `gh-pages`/root (public) and let a daily-sync run seed that branch.
- [WATCH] Segmentation: rebuild DONE + live (2026-07-22); held-back set CLOSED OUT (2026-07-22). `description` column added + persisted (`database.py`, all fetchers: YouTube snippet.description / TikTok signature / X bio). Of the 41 held-back, 35 got a real description via name-based re-fetch and were re-judged with real content + written (`creators/backfill_description.py` + `creators/rejudge_heldback.py`) — nearly all confirmed → `general` (real bios proved they were keyword false-positives: tech reviewers, expat/travel vlogs, comedy, anti-gambling). **6 stay PERMANENTLY parked** (no fix implied): Kevin O'Shea, ruchi kokcha, Harley XVI (X) + Terrian, Cassie, Ethan Zohn (TikTok) — name-based re-fetch found no match and the DB stores no handle/URL, so there's no reliable path to their bio.
- [WATCH] Instagram description gap (honest, likely permanent for the legacy set): the 11 IG creators are Mimanshi spreadsheet imports — display names (not usernames), no stored handle, and there is NO IG creator fetcher (only the trend reel-scraper, which is content not profiles). So these 11 cannot get descriptions by re-fetch. The PERSISTENCE path is IG-ready (`database.py` persists `description` for any platform), so a future IG *creator* fetcher (username-mode profile actor) that returns a bio would just work — but it would need real usernames, which these legacy rows don't have. Not fixable for the existing 11 without manual username entry.

# Resolved (kept briefly for context, delete anytime)
# - Anthropic credits: FIXED 2026-07-22 — new key on funded org 1ddc103d-a7e2-468e-87af-58a47e7a2f70, verified HTTP 200. (Old key was on empty org eeb81bd9…)
# - Repo import: DONE — codebase merged into TrySpeed/Marketing-Agent main by complying with the require-linked-issue ruleset (issue #1984), no bypass needed.
