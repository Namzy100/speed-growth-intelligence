"""Config for merchant-discovery: target verticals, automatable channel types,
seed aggregator listicles to harvest, per-vertical web_search query templates, a
known-brand list (feeds the audience tier), and the verified iGaming seed set.

Merchants = businesses in ANY industry that could accept/switch to Bitcoin or
stablecoin payments (iGaming, entertainment, fintech, PSPs, ...). This file is
the single place to extend to new verticals — add queries + (optionally) seed
venues and the pipeline/dashboard pick them up.
"""

# Channel types we CAN automate (public, crawlable). LinkedIn communities are
# deliberately excluded: LinkedIn's ToS bans scraping, it blocks bots, and its
# API exposes no group-discovery — so LinkedIn stays a MANUAL play, never
# attempted here. The dashboard shows that boundary explicitly.
CHANNEL_TYPES = ["forum", "publication", "association_event", "directory"]
EXCLUDED_CHANNELS = {"linkedin": "manual only — LinkedIn ToS/API make community discovery non-automatable"}

# Verticals (iGaming is the proven first slice; others are query-only until seeded).
VERTICALS = ["iGaming", "entertainment", "fintech", "psp"]

# Aggregator listicles the harvester fetches (NO API). A domain appearing across
# several of these is a real cross-listing signal, which feeds the audience tier.
SEED_AGGREGATORS = {
    "iGaming": [
        "https://www.igamingcalendar.com/",
        "https://affpapa.com/igaming-events/",
        "https://www.thegamblest.com/igaming-b2b-directory/",
        "https://gamblersconnect.com/ihub/b2b-providers/",
    ],
}

# Per-vertical web_search query templates (NEEDS credits; run behind --web-search).
# Scoped to the automatable channel types + a B2B/decision-maker framing so we
# don't pull player/consumer sites.
QUERY_TEMPLATES = {
    "iGaming": [
        "iGaming industry B2B forum for operators and affiliates",
        "iGaming trade publication for operators and executives payments",
        "iGaming association or conference fintech payments track",
        "iGaming B2B provider directory payment solutions",
    ],
    "entertainment": [
        "entertainment industry B2B trade publication payments executives",
        "streaming or events business association conference payments",
    ],
    "fintech": [
        "fintech industry B2B community forum for founders and executives",
        "fintech trade publication payments crypto stablecoin",
    ],
    "psp": [
        "payment service provider industry association forum",
        "payments industry trade publication merchant acquiring",
    ],
}

# Known B2B brands per vertical — a match lifts the audience tier (reputation
# signal, NOT traffic). Extend as verticals are validated.
KNOWN_BRANDS = {
    "igamingbusiness.com", "gamblinginsider.com", "sbcnews.co.uk", "igamingexpert.com",
    "gpwa.org", "affpapa.com", "igblive.com", "intergameonline.com",
}

# Outreach modes a venue supports (what the team can actually DO there).
OUTREACH_MODES = ["post content", "advertise", "get listed", "sponsor"]

# ------------------------------------------------------------------
# Verified iGaming seed (from the 2026-07-15 investigation).
# judge_source="manual-seed": relevance/payments scored by the human investigation,
# to be re-scored by the LLM judge once credits return. url_verified marks which
# URLs were confirmed in real search results vs named-but-URL-pending.
# ------------------------------------------------------------------

def _v(venue, url, url_verified, channel_type, relevance, payments, tier, outreach, reason):
    return {
        "venue": venue, "url": url, "url_verified": url_verified,
        "channel_type": channel_type, "vertical": "iGaming",
        "relevance": relevance, "payments_access": payments, "audience_tier": tier,
        "outreach_mode": outreach, "reason": reason,
        "judge_source": "manual-seed", "source": "investigation-2026-07-15",
    }

IGAMING_SEED = [
    # Trade publications (B2B, operators/execs)
    _v("iGaming Business", "https://igamingbusiness.com/", True, "publication", 10, 8, "T1",
       ["advertise", "post content"],
       "B2B trade press; C-suite ('iGB Executive'), Finance + Tech sections, covers payments."),
    _v("Gambling Insider", "https://www.gamblinginsider.com/", True, "publication", 9, 7, "T1",
       ["advertise", "post content", "sponsor"], "B2B execs; large industry events calendar."),
    _v("SBC News / iGaming Expert", "https://igamingexpert.com/", True, "publication", 9, 8, "T1",
       ["advertise", "post content", "sponsor"],
       "Major B2B network (SBC); SBC also runs operator events with a payments track."),
    _v("InterGame", "https://www.intergameonline.com/publications", True, "publication", 8, 6, "T2",
       ["advertise", "post content"], "Leading igaming trade magazine; industry-leader interviews."),
    _v("Global Gaming Business (GGB)", "", False, "publication", 8, 6, "T2",
       ["advertise", "post content"], "Casino management/tech/exec coverage. URL pending verification."),
    _v("iGaming Future", "https://igamingfuture.com/", True, "publication", 7, 6, "T2",
       ["advertise", "post content"], "B2B magazine series on industry trends."),
    _v("iGaming News", "https://www.igamingnews.com/", True, "publication", 7, 5, "T2",
       ["post content"], "Exec appointments / company news since 1996."),
    # Forums / communities / B2B directories
    _v("GPWA (Gambling Portal Webmasters Assoc.)", "https://www.gpwa.org/forum/forum.php", True, "forum", 8, 6, "T1",
       ["post content", "get listed"],
       "~35k members, 150 forums; affiliates + operators discuss programs, compliance, payments."),
    _v("AffPapa", "https://affpapa.com/", True, "directory", 8, 8, "T1",
       ["get listed", "advertise", "post content"],
       "Affiliate+operator community + B2B provider directory (payments listing) + events."),
    _v("AffRoom", "", False, "forum", 6, 5, "T3",
       ["post content", "get listed"], "Modern affiliate community. URL pending verification."),
    _v("TheGamblest B2B Directory", "https://www.thegamblest.com/igaming-b2b-directory/", True, "directory", 7, 8, "T2",
       ["get listed"], "B2B provider directory — visibility to operators seeking providers."),
    _v("GamblersConnect iHub", "https://gamblersconnect.com/ihub/b2b-providers/", True, "directory", 7, 8, "T2",
       ["get listed"], "B2B providers directory."),
    # Associations / events (decision-maker gatherings; payments-relevant)
    _v("HIPTHER Prague Summit", "", False, "association_event", 9, 9, "T1",
       ["sponsor", "advertise"],
       "C-level; explicitly iGaming + fintech + blockchain + regulatory. Top payments fit. URL pending verification."),
    _v("iGaming 3Tech (Amsterdam)", "", False, "association_event", 8, 9, "T1",
       ["sponsor"], "Dedicated iGaming FinTech track. URL pending verification."),
    _v("iGB L!VE London", "https://www.igblive.com/", True, "association_event", 9, 7, "T1",
       ["sponsor", "advertise"], "Operators / affiliates / vendors networking event."),
    _v("SBC Summit (Payment Expert Summit track)", "", False, "association_event", 9, 9, "T1",
       ["sponsor", "advertise"], "Major operator event with a payments track. URL pending verification."),
]

SEEDS = {"iGaming": IGAMING_SEED}
