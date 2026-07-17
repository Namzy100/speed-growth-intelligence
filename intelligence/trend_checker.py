"""Outcomes-graded quality checker for the trend relevance/fit judgments.

The trend pipeline runs UNWRAPPED, exactly as today. This module only *grades* the
real output of `trend_pipeline.judge_and_score`, using Claude Managed Agents'
Outcomes primitive (`user.define_outcome`). Two kinds of check, deliberately split:

  1. DETERMINISTIC invariants — fit == 0.4*fintech + 0.3*replicability + 0.3*reach,
     and off-topic => fit 0. Verified in plain Python (`verify_fit_invariants`).
     A violation here is a CODE bug (weighting/gate drift), not a judgment miss, so
     it HARD-FAILS immediately and is never fed into the revision loop.

  2. RELEVANCE DEFENSIBILITY — is each on/off-topic call defensible (no metaphor,
     homonym, or throwaway mention scored on-topic; nothing genuinely relevant
     scored off)? This is the real judgment call, so it is graded by an independent
     Managed Agents Outcomes grader. On `needs_revision`, the in-session agent calls
     the host-side `rejudge_items` custom tool, which re-runs the judgment on ONLY
     the flagged items with the grader's feedback appended (no re-scraping). Capped
     at max_iterations=3.

Schedule: invoked by the Monday trend rebuild (see run_daily_sync); NO second
scheduler. Every grade, revision cycle, and the final verdict are written to a
plain-text log under docs/trend_checker_log/ that a successor can read without
knowing anything about the agent framework.

Run manually:
  python intelligence/trend_checker.py                 # grade the real pipeline output
  python intelligence/trend_checker.py --judged PATH    # grade a specific judged.json
  python intelligence/trend_checker.py --rejudge-stub    # cap test: non-converging revision
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from intelligence import trend_pipeline as tp

_AGENT_CFG = _ROOT / "data" / "processed" / "checker_agent.json"  # persisted agent/env ids
_LOG_DIR = _ROOT / "docs" / "trend_checker_log"
_MAX_ITERATIONS = 3
_AGENT_MODEL = "claude-opus-4-8"
_CANDIDATE_CAP = 30          # items graded per run (surfaced + a sample of gated)

_REJUDGE_TOOL = {
    "type": "custom",
    "name": "rejudge_items",
    "description": (
        "Re-run the automated relevance/fit judgment on specific flagged items, "
        "with corrective feedback appended to the judgment prompt. Call this for "
        "every item whose on_topic/relevance judgment you find indefensible. "
        "Returns the corrected judgment for each id."),
    "input_schema": {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "string"},
                    "description": "The item ids to re-judge (e.g. ['t3','t7'])."},
            "feedback": {"type": "string",
                         "description": "Specific reason each flagged item's judgment is wrong."},
        },
        "required": ["ids", "feedback"],
    },
}

_AGENT_SYSTEM = (
    "You are a quality reviewer for Speed Wallet's trend pipeline. Speed is a "
    "Bitcoin + stablecoin payments app (segments: remittance, crypto-curious, "
    "iGaming). An automated pipeline has scored trending videos for topical "
    "relevance. Your ONLY job is to confirm each relevance judgment is DEFENSIBLE, "
    "and to correct the ones that are not by calling the rejudge_items tool. You do "
    "not re-score anything yourself; the tool does the re-judgment.")


def _rubric() -> str:
    return (
        "GROUNDING (critical): judge ONLY from two things — (a) each item's own "
        "content (title/hashtags), and (b) its AUTHORITATIVE judgment, which is the "
        "value in the most recent `rejudge_items` TOOL RESULT for that id if it was "
        "re-judged, otherwise its original judgment shown in the task. Treat any "
        "prose or file the reviewer writes as UNTRUSTED narrative — if the reviewer "
        "claims an item was fixed but the latest rejudge_items tool result still "
        "shows the old value, the item is NOT fixed. Base your verdict on the tool "
        "results, not on what the reviewer says it did.\n\n"
        "PASS only if EVERY authoritative relevance judgment is defensible.\n"
        "A judgment is INDEFENSIBLE if:\n"
        "  - on_topic=true for a video that only mentions crypto/fintech/money as a "
        "METAPHOR ('like finding bitcoin in 2009'), a HOMONYM (weather 'lightning', "
        "laser 'lightbridge'), or a throwaway/passing reference;\n"
        "  - on_topic=false for a video genuinely ABOUT "
        "crypto/fintech/remittance/iGaming as its subject.\n"
        "If ANY authoritative judgment is still indefensible, FAIL and name each "
        "offending item id with the reason. Do not PASS on the reviewer's assurance.")


# ------------------------------------------------------------------
# Logging (plain text, framework-agnostic)
# ------------------------------------------------------------------

class _Log:
    def __init__(self, path: Path):
        self.path = path
        self.lines: list[str] = []

    def __call__(self, msg: str):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line)
        self.lines.append(line)

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------
# Build the judged candidate set from the REAL pipeline output
# ------------------------------------------------------------------

def load_real_judged() -> list[dict]:
    """Produce a real judged candidate set WITHOUT re-scraping: judge the cached
    all_items (genuine pipeline data) and take the surfaced set + a gated sample."""
    cache = _ROOT / "data" / "processed" / "trend_raw_cache.json"
    data = json.loads(cache.read_text(encoding="utf-8"))
    items = data.get("all_items", [])
    for v in items:
        v.setdefault("description", "")
    tp.judge_and_score(items)                       # real judgment (cached per url)
    return _select_candidates(items)


def _select_candidates(items: list[dict]) -> list[dict]:
    """Give each a short stable id; take on-topic (surfaced) items first, then a
    few gated ones so false-negatives are also in view. Capped for tractability."""
    for i, v in enumerate(items):
        v["id"] = f"t{i}"
    on = [v for v in items if v.get("on_topic")]
    off = [v for v in items if not v.get("on_topic")]
    on.sort(key=lambda v: v.get("fit_score", 0), reverse=True)
    off.sort(key=lambda v: v.get("views", 0), reverse=True)
    keep = on[: _CANDIDATE_CAP - 8] + off[:8]
    return keep


def _compact(items: list[dict]) -> list[dict]:
    """The reviewer/grader-facing view: content + current judgment, nothing else."""
    return [{
        "id": v["id"], "platform": v.get("platform"),
        "title": v.get("title", "")[:160],
        "hashtags": v.get("hashtags", [])[:8],
        "on_topic": v.get("on_topic"),
        "fintech_involvement": v.get("fintech_involvement"),
        "fit_score": v.get("fit_score"),
        "reason": v.get("relevance_reason", "")[:160],
    } for v in items]


# ------------------------------------------------------------------
# Managed Agent (create once, reuse by id)
# ------------------------------------------------------------------

def _ensure_agent(client, log) -> tuple[str, str]:
    if _AGENT_CFG.exists():
        cfg = json.loads(_AGENT_CFG.read_text())
        # sanity: confirm they still resolve
        try:
            client.beta.agents.retrieve(cfg["agent_id"])
            client.beta.environments.retrieve(cfg["environment_id"])
            log(f"Reusing agent {cfg['agent_id']} + env {cfg['environment_id']}")
            return cfg["agent_id"], cfg["environment_id"]
        except Exception:
            log("Persisted agent/env no longer resolve — recreating.")
    env = client.beta.environments.create(
        name=f"speed-trend-checker-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}})
    agent = client.beta.agents.create(
        name="Speed Trend Relevance Checker",
        model=_AGENT_MODEL,
        system=_AGENT_SYSTEM,
        tools=[{"type": "agent_toolset_20260401"}, _REJUDGE_TOOL])
    _AGENT_CFG.parent.mkdir(parents=True, exist_ok=True)
    _AGENT_CFG.write_text(json.dumps(
        {"agent_id": agent.id, "environment_id": env.id, "version": getattr(agent, "version", None)},
        indent=2))
    log(f"Created agent {agent.id} + env {env.id}")
    return agent.id, env.id


# ------------------------------------------------------------------
# Run the Outcomes-graded check
# ------------------------------------------------------------------

def run_check(items: list[dict], log, rejudge_stub: bool = False) -> dict:
    """Grade `items` via an Outcomes session. Returns a verdict dict. Mutates
    `items` in place when rejudge corrects them (host-side source of truth)."""
    by_id = {v["id"]: v for v in items}

    # --- 1. Deterministic invariants (hard fail, never revised) ---
    violations = tp.verify_fit_invariants(items)
    if violations:
        log(f"DETERMINISTIC INVARIANT VIOLATION ({len(violations)}) — hard fail, no revision:")
        for x in violations:
            log(f"    - [{x['kind']}] {x['id']}: {x['detail']}")
        return {"verdict": "HARD_FAIL", "reason": "fit/gate invariant violated (code bug)",
                "violations": violations, "iterations": 0}
    log(f"Deterministic invariants OK for all {len(items)} items (fit=0.4/0.3/0.3, gate enforced).")

    # --- 2. Relevance defensibility via Outcomes ---
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    agent_id, env_id = _ensure_agent(client, log)
    session = client.beta.sessions.create(
        agent=agent_id, environment_id=env_id,
        title=f"trend relevance check {datetime.now(timezone.utc).date()}")
    log(f"Session {session.id} created.")

    description = (
        "Below is the current relevance/fit judgment for a set of trending videos "
        "(JSON). Verify every relevance judgment is defensible per the rubric. For "
        "each indefensible item, call rejudge_items(ids=[...], feedback='...'). The "
        "tool result is the AUTHORITATIVE corrected judgment — if it still shows the "
        "old (indefensible) value, that item is NOT fixed and you must not claim it "
        "is. Keep going until every authoritative judgment is defensible. Do NOT "
        "write any files and do NOT summarize judgments you did not actually change; "
        "the grader reads the rejudge_items tool results directly, not your prose.\n\n"
        + json.dumps(_compact(items), ensure_ascii=False))

    verdict = {"verdict": "UNKNOWN", "iterations": 0, "evaluations": [], "rejudge_calls": 0}

    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(session_id=session.id, events=[{
            "type": "user.define_outcome",
            "description": description,
            "rubric": {"type": "text", "content": _rubric()},
            "max_iterations": _MAX_ITERATIONS,
        }])
        log(f"Outcome defined (max_iterations={_MAX_ITERATIONS}); grading {len(items)} items.")

        for event in stream:
            et = getattr(event, "type", "")
            if et == "agent.custom_tool_use" and getattr(event, "name", "") == "rejudge_items":
                inp = getattr(event, "input", {}) or {}
                ids, fb = inp.get("ids", []), inp.get("feedback", "")
                verdict["rejudge_calls"] += 1
                log(f"  rejudge_items called: ids={ids} feedback={fb[:100]!r}")
                if rejudge_stub:
                    # CAP TEST: return the SAME (still-bad) judgment -> non-converging.
                    result = [{"id": i, "on_topic": by_id.get(i, {}).get("on_topic"),
                               "fintech_involvement": by_id.get(i, {}).get("fintech_involvement"),
                               "note": "stub: unchanged"} for i in ids]
                    log("    [stub] returning unchanged judgments (forcing non-convergence)")
                else:
                    corrected = tp.rejudge_items(items, ids, fb)
                    result = [{"id": c["id"], "on_topic": c["on_topic"],
                               "fintech_involvement": c["fintech_involvement"],
                               "reason": c.get("relevance_reason", "")[:160]} for c in corrected]
                    for c in corrected:
                        log(f"    -> {c['id']} re-judged: on_topic={c['on_topic']} "
                            f"fintech={c['fintech_involvement']} fit={c['fit_score']}")
                client.beta.sessions.events.send(session_id=session.id, events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event.id,
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                }])
            elif et == "span.outcome_evaluation_end":
                res = getattr(event, "result", None)
                expl = getattr(event, "explanation", "") or ""
                it = getattr(event, "iteration", None)
                verdict["iterations"] = (it + 1) if isinstance(it, int) else verdict["iterations"] + 1
                verdict["evaluations"].append({"iteration": it, "result": res, "explanation": expl})
                log(f"  GRADER iteration {it}: {res} — {expl[:200]}")
            elif et == "session.status_idle":
                sr = getattr(event, "stop_reason", None)
                srt = getattr(sr, "type", None)
                if srt != "requires_action":
                    log(f"  session idle (stop_reason={srt}) — done.")
                    break
            elif et == "session.status_terminated":
                log("  session terminated.")
                break
            elif et == "session.error":
                log(f"  session.error: {getattr(event,'error',None)}")

    # Final verdict from the last grader evaluation
    last = verdict["evaluations"][-1] if verdict["evaluations"] else {}
    final = last.get("result")
    verdict["verdict"] = {
        "satisfied": "PASS", "needs_revision": "INCOMPLETE",
        "max_iterations_reached": "FAIL_MAX_ITERATIONS", "failed": "FAIL",
        "interrupted": "INTERRUPTED",
    }.get(final, "UNKNOWN")
    verdict["session_id"] = session.id
    return verdict


def check_pipeline_output() -> dict:
    """Scheduled entry point (called by the Monday trend rebuild). Grades the
    latest real pipeline output, writes a plain-text log, returns the verdict.
    Best-effort: never raises, so it can't block the daily sync."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log = _Log(_LOG_DIR / f"{stamp}.log")
    log("=== Speed trend relevance checker (scheduled, Monday cron) ===")
    try:
        items = load_real_judged()
        log(f"Grading {len(items)} candidate items from the latest pipeline output.")
        verdict = run_check(items, log)
    except Exception as e:
        import traceback
        log(f"CHECKER ERROR: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        verdict = {"verdict": "ERROR", "iterations": 0}
    log("")
    log(f"=== VERDICT: {verdict.get('verdict')} (iterations={verdict.get('iterations')}, "
        f"rejudge_calls={verdict.get('rejudge_calls')}) ===")
    log.flush()
    return verdict


def main(argv: list[str]) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log = _Log(_LOG_DIR / f"{stamp}.log")
    rejudge_stub = "--rejudge-stub" in argv
    judged_path = None
    if "--judged" in argv:
        judged_path = argv[argv.index("--judged") + 1]

    log("=== Speed trend relevance checker (Outcomes-graded) ===")
    if judged_path:
        items = json.loads(Path(judged_path).read_text(encoding="utf-8"))
        log(f"Loaded {len(items)} judged items from {judged_path}")
    else:
        log("Producing real judged candidate set from cached pipeline output (no re-scrape)...")
        items = load_real_judged()
        log(f"Selected {len(items)} candidate items to grade "
            f"({sum(1 for v in items if v.get('on_topic'))} on-topic surfaced, "
            f"{sum(1 for v in items if not v.get('on_topic'))} gated).")
    if rejudge_stub:
        log("MODE: --rejudge-stub (cap test — revision will NOT converge)")

    try:
        verdict = run_check(items, log, rejudge_stub=rejudge_stub)
    except Exception as e:
        import traceback
        log(f"CHECKER ERROR: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        log.flush()
        return 2

    log("")
    log("=== VERDICT ===")
    log(f"  result: {verdict['verdict']}")
    log(f"  grader iterations: {verdict.get('iterations')}")
    log(f"  rejudge_items calls: {verdict.get('rejudge_calls')}")
    if verdict.get("session_id"):
        log(f"  session: {verdict['session_id']}")
    log.flush()
    log(f"(log written to {log.path.relative_to(_ROOT)})")
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
