"""Supabase read/write operations for the Speed Wallet creator intelligence system."""

import os
from datetime import datetime, timezone
from typing import Literal

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_TABLE = "creators"

OutreachStatus = Literal["not_contacted", "contacted", "responded", "in_negotiation"]
_VALID_STATUSES: frozenset[str] = frozenset(
    {"not_contacted", "contacted", "responded", "in_negotiation"}
)

# Run this once in the Supabase SQL editor to create the table and grant API access.
# Dashboard: https://supabase.com/dashboard/project/<your-project-ref>/sql/new
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS creators (
    id                      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name                    TEXT NOT NULL,
    platform                TEXT NOT NULL,
    followers               INTEGER DEFAULT 0,
    engagement_rate         FLOAT DEFAULT 0,
    engagement_quality      INTEGER DEFAULT 5,
    crypto_content_pct      FLOAT DEFAULT 0,
    fintech_content_pct     FLOAT DEFAULT 0,
    sponsorship_count       INTEGER DEFAULT 0,
    niche_tags              TEXT[] DEFAULT '{}',
    composite_score         FLOAT DEFAULT 0,
    deposit_relevance_score FLOAT DEFAULT 0,
    segment_tag             TEXT DEFAULT 'general',
    reasoning               TEXT,
    audience_fit            FLOAT DEFAULT 0,
    engagement_quality_score FLOAT DEFAULT 0,
    content_alignment       FLOAT DEFAULT 0,
    acquisition_potential   FLOAT DEFAULT 0,
    sponsorship_score       FLOAT DEFAULT 0,
    outreach_status         TEXT DEFAULT 'not_contacted'
                            CHECK (outreach_status IN (
                                'not_contacted', 'contacted',
                                'responded', 'in_negotiation'
                            )),
    brief                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT now(),
    updated_at              TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, platform)
);

-- Tables created via the SQL editor are not automatically exposed to the
-- PostgREST API. These grants make the table visible to the Supabase client.
GRANT ALL ON public.creators TO anon, authenticated, service_role;
""".strip()

# If the table already exists but is unreachable (PGRST125), the grants are
# likely missing. Run just this block in the SQL editor to fix it.
GRANT_SQL = "GRANT ALL ON public.creators TO anon, authenticated, service_role;"

# Migration for tables created before deposit_relevance_score was persisted.
# Idempotent — safe to run repeatedly.
ADD_DEPOSIT_COLUMN_SQL = (
    "ALTER TABLE public.creators "
    "ADD COLUMN IF NOT EXISTS deposit_relevance_score FLOAT DEFAULT 0;"
)


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------

def _client() -> Client:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    # The supabase client appends /rest/v1 itself; strip it if already present in the env var.
    for suffix in ("/rest/v1", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return create_client(url, key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists() -> bool:
    """Return True if the creators table is reachable via a zero-row probe SELECT."""
    try:
        _client().table(_TABLE).select("id").limit(0).execute()
        return True
    except Exception:
        return False


def _guard_table(exc: Exception) -> None:
    """Re-raise exc, swapping in a helpful message only when the table is confirmed missing."""
    if not _table_exists():
        raise RuntimeError(
            "The 'creators' table is not accessible via the Supabase API.\n"
            "This usually means either the table doesn't exist, or it was created\n"
            "via the SQL editor without the required grants.\n\n"
            f"If the table doesn't exist, run:\n\n{CREATE_TABLE_SQL}\n\n"
            f"If the table exists but is unreachable, run just:\n\n{GRANT_SQL}"
        ) from exc
    raise exc


# ------------------------------------------------------------------
# Write operations
# ------------------------------------------------------------------

def save_creator(creator_dict: dict, score_dict: dict) -> dict:
    """Upsert a creator record.

    If a creator with the same name and platform already exists their scores
    are updated in-place; outreach_status and brief are left unchanged.

    Args:
        creator_dict: Raw creator dict from youtube.py or apify_tiktok.py.
        score_dict:   Score dict returned by CreatorScorer.score().

    Returns:
        The inserted or updated record as a dict.
    """
    sb = _client()
    record = _merge_record(creator_dict, score_dict)

    try:
        existing = (
            sb.table(_TABLE)
            .select("id, outreach_status")
            .eq("name", record["name"])
            .eq("platform", record["platform"])
            .execute()
        )
    except Exception as e:
        _guard_table(e)

    if existing.data:
        # Update scores and metadata; preserve outreach_status and brief.
        update_payload = {k: v for k, v in record.items() if k != "outreach_status"}
        result = (
            sb.table(_TABLE)
            .update(update_payload)
            .eq("id", existing.data[0]["id"])
            .execute()
        )
    else:
        result = sb.table(_TABLE).insert(record).execute()

    return result.data[0] if result.data else {}


def update_outreach_status(creator_id: str, new_status: OutreachStatus) -> dict:
    """Update the outreach_status for a single creator.

    Args:
        creator_id: The creator's UUID (id column).
        new_status: One of not_contacted / contacted / responded / in_negotiation.

    Returns:
        The updated record as a dict.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{new_status}'. "
            f"Must be one of: {sorted(_VALID_STATUSES)}"
        )
    result = (
        _client().table(_TABLE)
        .update({"outreach_status": new_status, "updated_at": _now()})
        .eq("id", creator_id)
        .execute()
    )
    return result.data[0] if result.data else {}


def save_brief(creator_id: str, brief_text: str) -> dict:
    """Save a generated brief for a creator.

    Args:
        creator_id: The creator's UUID (id column).
        brief_text: The brief text to store.

    Returns:
        The updated record as a dict.
    """
    result = (
        _client().table(_TABLE)
        .update({"brief": brief_text, "updated_at": _now()})
        .eq("id", creator_id)
        .execute()
    )
    return result.data[0] if result.data else {}


# ------------------------------------------------------------------
# Read operations
# ------------------------------------------------------------------

def get_all_creators() -> list[dict]:
    """Return all creators ordered by composite_score descending."""
    try:
        result = (
            _client().table(_TABLE)
            .select("*")
            .order("composite_score", desc=True)
            .execute()
        )
    except Exception as e:
        _guard_table(e)
    return result.data


def get_creators_by_segment(segment_tag: str) -> list[dict]:
    """Return creators filtered by segment_tag, ordered by score."""
    try:
        result = (
            _client().table(_TABLE)
            .select("*")
            .eq("segment_tag", segment_tag)
            .order("composite_score", desc=True)
            .execute()
        )
    except Exception as e:
        _guard_table(e)
    return result.data


def get_creators_by_status(outreach_status: OutreachStatus) -> list[dict]:
    """Return creators filtered by outreach_status, ordered by score.

    Args:
        outreach_status: not_contacted / contacted / responded / in_negotiation.
    """
    if outreach_status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{outreach_status}'. "
            f"Must be one of: {sorted(_VALID_STATUSES)}"
        )
    try:
        result = (
            _client().table(_TABLE)
            .select("*")
            .eq("outreach_status", outreach_status)
            .order("composite_score", desc=True)
            .execute()
        )
    except Exception as e:
        _guard_table(e)
    return result.data


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _merge_record(creator_dict: dict, score_dict: dict) -> dict:
    """Flatten creator dict + score dict into a single DB-ready record."""
    scores = score_dict.get("scores", {})
    return {
        # Raw creator fields
        "name":                 creator_dict["name"],
        "platform":             creator_dict["platform"],
        "followers":            creator_dict.get("followers", 0),
        "engagement_rate":      creator_dict.get("engagement_rate", 0.0),
        "engagement_quality":   creator_dict.get("engagement_quality", 5),
        "crypto_content_pct":   creator_dict.get("crypto_content_pct", 0.0),
        "fintech_content_pct":  creator_dict.get("fintech_content_pct", 0.0),
        "sponsorship_count":    creator_dict.get("sponsorship_count", 0),
        "niche_tags":           creator_dict.get("niche_tags", []),
        # Scoring fields
        "composite_score":          score_dict.get("composite_score", 0.0),
        "deposit_relevance_score":  score_dict.get("deposit_relevance_score", 0.0),
        "segment_tag":              score_dict.get("segment_tag", "general"),
        "reasoning":                score_dict.get("reasoning", ""),
        "audience_fit":             scores.get("audience_fit", 0.0),
        "engagement_quality_score": scores.get("engagement_quality", 0.0),
        "content_alignment":        scores.get("content_alignment", 0.0),
        "acquisition_potential":    scores.get("acquisition_potential", 0.0),
        "sponsorship_score":        scores.get("sponsorship_history", 0.0),
        # Defaults for new records
        "outreach_status": "not_contacted",
        "updated_at":      _now(),
    }


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from creators.scorer import CreatorScorer

    SAMPLE_CREATOR = {
        "name": "_speed_test_creator_",
        "platform": "YouTube",
        "followers": 150_000,
        "engagement_rate": 0.038,
        "engagement_quality": 7,
        "crypto_content_pct": 0.55,
        "fintech_content_pct": 0.10,
        "sponsorship_count": 4,
        "niche_tags": ["bitcoin", "lightning", "crypto", "investing"],
    }

    scorer = CreatorScorer()
    score = scorer.score(SAMPLE_CREATOR)

    print("Saving sample creator to Supabase...")
    try:
        saved = save_creator(SAMPLE_CREATOR, score)
    except RuntimeError as e:
        # Table doesn't exist yet — print setup instructions and exit.
        print(f"\n{e}\n")
        sys.exit(1)

    creator_id = saved.get("id")
    print(f"  Saved  : {saved['name']} ({saved['platform']}) — id={creator_id}")
    print(f"  Score  : {saved['composite_score']} / 100  segment={saved['segment_tag']}")

    # Read it back via the segment filter to confirm round-trip
    segment_results = get_creators_by_segment(saved["segment_tag"])
    match = next((r for r in segment_results if r["id"] == creator_id), None)
    if match:
        print(f"  Read back via get_creators_by_segment('{saved['segment_tag']}'): OK")
    else:
        print("  WARNING: record not found in read-back query")

    # Test outreach status update
    updated = update_outreach_status(creator_id, "contacted")
    print(f"  Status updated to: {updated.get('outreach_status')}")

    # Test brief save
    briefed = save_brief(creator_id, "Test brief: strong crypto-curious candidate.")
    print(f"  Brief saved: {bool(briefed.get('brief'))}")

    # Confirm the record is in get_creators_by_status
    status_results = get_creators_by_status("contacted")
    in_status = any(r["id"] == creator_id for r in status_results)
    print(f"  Appears in get_creators_by_status('contacted'): {in_status}")

    print("\nAll checks passed.")
