# Market-entry data review: Germany, UK, Portugal

**Prepared for:** Niyati / Sumit · **Date:** 2026-07-14
**Source:** Adjust (installs, attribution, retention) + Meta Ads (geo spend), last 30 days. Numbers are live pulls, not estimates.
**Why now:** Germany's organic install signal was flagged weeks ago as worth a real market-entry conversation and never followed up. This is the follow-up: what the data actually shows for the three markets flagged in earlier strategy work.

---

## The one-line version

All three markets are running **almost entirely on organic installs with effectively zero paid spend**, and all three **retain better than the US** — the market we currently put ~$7,900/mo of Meta spend behind. Germany is the standout (1,807 installs/30d, 2nd only to the US). This is a real unpaid-demand signal. It is **not** proof that a paid push would convert efficiently, because we have never spent enough in any of them to find out.

---

## The numbers (last 30 days)

| Market | Total installs | Share of global | Organic + owned | Paid spend | Retention D1 / D7 / D14 |
|---|---|---|---|---|---|
| **Germany** | **1,807** | 3.8% | ~1,776 (98%) | **$10.26** | 31.3% / 16.3% / 14.3% |
| **United Kingdom** | **840** | 1.8% | ~828 (99%) | **$0.00** | 28.4% / 17.3% / 11.1% |
| **Portugal** | **213** | 0.4% | ~212 (99%) | **$0.00** | 30.8% / 19.2% / **19.3%** |
| _US (benchmark)_ | _18,721_ | _39%_ | _mixed_ | _~$7,900 (Meta)_ | _25.1% / 13.0% / 9.6%_ |

Global total: **47,489 installs/30d.**

- **Paid spend is the ground truth from Adjust's cost column.** Germany's entire paid footprint is **$10.26** on Apple Search Ads (26 installs). UK and Portugal are **$0.00**. A handful of Google/Meta-attributed installs appear at **$0 cost** (attribution spillover from global/branded activity, not a funded in-market campaign).
- **Retention differentiates by country, and it favors these markets.** Every one of the three beats the US at D1, D7, and D14. Portugal is the strongest cohort we have (D14 19.3%, roughly 2x the US's 9.6%), despite being the smallest.

---

## What it would take to enter: is there existing paid infrastructure?

**No. All three would be starting from zero paid.**

- **Meta:** ad delivery in the last 30 days went to **only 3 countries — US ($7,900), and negligible/zero elsewhere.** DE, UK, and PT had **no Meta delivery or spend at all.** There is no campaign, audience, or creative currently geo-targeting these markets.
- **Google / Apple Search Ads:** the only paid line item anywhere in the three is Germany's $10 of Apple Search Ads, which reads as global-ASA spillover rather than a deliberate German campaign. No Google Ads spend of note.

So "enter Germany/UK/PT" means **standing up paid acquisition from scratch** (campaigns, geo audiences, localized creative), not scaling something already warm.

---

## Honest read

- **The signal is real and unpaid.** ~1,800 German installs a month with ~$0 spend, and cohorts that retain better than our funded US market, is a genuine demand signal. Germany specifically is already our second-largest install source globally on zero investment.
- **It is a demand signal, not a go signal.** We have never spent meaningfully in any of these markets, so **paid CAC, in-market CPI, and creative-market fit are completely unmeasured.** Organic pull does not guarantee paid efficiency — it tells us there's latent interest, not that dollars convert.
- **Retention deltas are directional, not statistically firm**, especially Portugal (213 installs is a small cohort). Treat the ordering (all three > US) as more reliable than the exact percentages.

---

## Open questions (out of scope for this pull, but they gate a real decision)

These are not answerable from install/spend data and should be named, not glossed:

1. **Creator/influencer landscape in-market** — do we have, or can we source, credible DE/UK/PT creators? (Our creator DB is US-focused today.)
2. **Product localization** — language, local payment rails, and onboarding fit for each market.
3. **Regulatory posture for a Bitcoin + stablecoin product** — the EU's **MiCA** framework would apply to Germany and Portugal, and the **UK's FCA** crypto regime applies post-Brexit. This pull does **not** assess either; it needs real legal/compliance review before any market-entry commitment.
4. **Qualitative synthesis / prioritized recommendation** — turning these numbers into a ranked "enter X first, here's the play" would use the Anthropic API for the strategic write-up. **The org's Anthropic credit balance is currently exhausted**, so I've deliberately left that out rather than hand-wave it. It's a fast follow-up once credits are restored; the numbers above stand on their own in the meantime.

---

*All figures pulled 2026-07-14 from Adjust (`get_installs_by_country` + country×network + country retention) and the Meta Graph API (spend by country). Adjust and Anthropic are separate services; nothing here was affected by the Anthropic credit issue.*
