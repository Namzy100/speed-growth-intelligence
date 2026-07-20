# Blockers & watch items

Maintained by hand. The morning brief (pipelines/morning_brief.py) surfaces
`- [BLOCKED]` lines as alerts and `- [WATCH]` lines as routine watch items.
Delete a line when it's resolved.

- [BLOCKED] Anthropic credits exhausted (confirmed 2026-07-20) — daily-sync fails every run on "credit balance is too low". Strategy + Trend dashboards fully stale since 07-14; Creative dashboard's AI insight cards stale (data/timestamp still current). Fix is funding the balance, not code. Verify green via a manual daily-sync run once funded.
- [BLOCKED] Repo migration to TrySpeed/Marketing-Agent — the initial import to `main` is rejected by the org `require-linked-issue` ruleset. Awaiting a one-time ruleset bypass from Pranav (requested 2026-07-18). New repo `main` is still the placeholder README; dashboards still live from the current repo.
- [WATCH] gh-pages deploy prep is committed on `feat/gh-pages-deploy` (pushed, not merged on purpose) — fold into main as part of the import once the bypass lands.
