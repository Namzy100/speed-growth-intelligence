"""Quality checker for the trend relevance/fit judgments.

The trend pipeline runs UNWRAPPED, exactly as before. This module only reviews the
real output of `trend_pipeline.judge_and_score`. Two kinds of check, deliberately
separated:

  1. DETERMINISTIC invariants — fit == 0.4*fintech + 0.3*replicability + 0.3*reach,
     and off-topic => fit 0. Verified in plain Python (`verify_fit_invariants`).
     A violation here is a CODE bug (weighting/gate drift), not a judgment miss, so
     it HARD-FAILS immediately and is never sent to a model.

  2. RELEVANCE DEFENSIBILITY — is each on/off-topic call defensible (no metaphor,
     homonym, or throwaway mention scored on-topic; nothing genuinely relevant
     scored off; no substanceless hype scored as a strong fit)? A reviewer model
     works through the set and calls the host-side `rejudge_items` tool for each
     judgment it finds indefensible, which re-runs the judgment on ONLY those items
     with the reviewer's feedback appended (no re-scrape).

WHY THE OUTCOMES GRADER WAS REMOVED (2026-07-28). This used to wrap the reviewer in
a Claude Managed Agents Outcomes session, where an independent grader scored the
result against a rubric and drove a revise loop. That grader failed to complete in
4 of 4 real CI attempts across two different budget/iteration configurations
(600s/3 iterations, 1200s/3, 1200s/1) — an established failure rate, not bad luck.
The measured reason: ONE grader evaluation cost 15-17 minutes of wall clock while
the reviewer's own correction pass took under 4 minutes and all host-side
rejudge_items work took 7 seconds, so ~97% of every run was spent inside the
grading service and no run ever got past iteration 0. The correction pass, by
contrast, succeeded in 4 of 4 attempts and kept surfacing genuinely defensible
corrections. And the graded verdict was never gating anything: run_daily_sync
builds the trend dashboard BEFORE the checker runs, and the relevance cache the
corrections write to is not committed, so nothing downstream ever consumed it.
Dropping the grader trades an unreliable component that produced nothing usable
for a reliable one that was already doing the real work. The reviewer now runs as a
plain Messages-API tool loop — no session, no per-hour billing, no session-stop
problem, and no grader latency.

Because there is no graded verdict any more, THE LOG IS THE DELIVERABLE. Verdicts:
HARD_FAIL (invariant violation, a code bug), CLEAN (no item needed correcting),
CORRECTED (n judgments actually changed), FLAGGED_UNCHANGED (the reviewer objected
to items but every re-judgment came back identical — the judgments it objected to
are still in place, so a human should look), TIMEOUT, REFUSED, ERROR. Only CLEAN
and CORRECTED are successful reviews. A "correction" is counted only when the value
really moved, compared against a snapshot taken before rejudge_items mutates the
item in place — an unchanged re-judgment used to be miscounted as a fix.

Schedule: invoked by the Monday trend rebuild (see run_daily_sync); NO second
scheduler. Every correction and the final verdict are written to a plain-text log
under docs/trend_checker_log/, written line-by-line as it happens so a run killed
mid-flight still leaves a readable partial trace. The newest run is also mirrored
to docs/trend_checker_latest.log, which IS published by the deploy step — the
per-run logs live in a subdirectory that is not, so on CI they died with the runner.

TIME BOUND: the pass is capped at TREND_CHECKER_BUDGET_SECONDS (default 1200).
On 2026-07-27 an unbounded session ran 17m24s and was SIGKILLed by the CI job
timeout, taking the deploy step with it. Over budget is now a logged TIMEOUT
verdict, never a silent kill.

Run manually:
  python intelligence/trend_checker.py                 # review the real pipeline output
  python intelligence/trend_checker.py --judged PATH    # review a specific judged.json
  python intelligence/trend_checker.py --rejudge-stub    # cap test: non-converging loop
  python intelligence/trend_checker.py --budget 60       # override the wall-clock budget
"""

import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from intelligence import trend_pipeline as tp

_LOG_DIR = _ROOT / "docs" / "trend_checker_log"
# Stable, publishable copy of the newest run's log. The per-run logs live in a
# subdirectory that _deploy_dashboard does not publish, so on CI they died with the
# runner and only the Actions log carried the trail. Now that the reviewer's findings
# ARE the deliverable, that trail has to survive: this file is in the deploy list.
_LATEST_LOG = _ROOT / "docs" / "trend_checker_latest.log"

_REVIEW_MODEL = "claude-opus-5"
_REVIEW_EFFORT = "high"      # judgment quality matters more here than token spend
_REVIEW_MAX_TOKENS = 16000   # caps thinking + text together; thinking is on by default
# Turns of the tool loop. A turn is one model call plus any rejudge_items it asks for,
# and the reviewer has finished its pass in 2-3 turns every time it has been measured.
# This is a runaway backstop, not a budget: the wall-clock budget is the real bound.
_MAX_TURNS = 10
_CANDIDATE_CAP = 30          # items graded per run (surfaced + a sample of gated)

# Hard wall-clock budget for the whole reviewer pass. Kept even though the pass now
# completes in ~4 minutes: an unbounded model loop is what caused the 2026-07-27
# incident (17m24s, SIGKILLed by the CI job timeout, taking the deploy step with it),
# and the bound is what turns a slow run into a logged TIMEOUT instead of a silent
# kill. 1200 is deliberately generous relative to the measured ~4 min so a slow day
# does not trip it; the turn cap and the per-request SDK timeout are the inner
# bounds. Monday worst case is now ~1.5 min sync + ~9.5 min trend rebuild + ~4 min
# checker, comfortably inside timeout-minutes: 45.
_CHECKER_BUDGET_SECONDS = int(os.getenv("TREND_CHECKER_BUDGET_SECONDS", "1200"))
_SDK_TIMEOUT_SECONDS = 120   # per-request cap so a single hung HTTP call can't stall us

# Messages-API tool shape: name / description / input_schema. (The previous
# Managed Agents "custom" tool shape is gone along with the session.) The
# description is deliberately prescriptive about WHEN to call, not just what it
# does — recent Opus models reach for tools conservatively, and a trigger
# condition in the description measurably raises the should-call rate.
_REJUDGE_TOOL = {
    "name": "rejudge_items",
    "description": (
        "Re-run the automated relevance/fit judgment on specific flagged items, with "
        "corrective feedback appended to the judgment prompt. Call this for EVERY item "
        "whose on_topic or fit judgment you find indefensible — it is the only way to "
        "correct one; writing the correction in prose changes nothing. Returns the "
        "corrected judgment for each id."),
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

_REVIEWER_SYSTEM = (
    "You are a quality reviewer for Speed Wallet's trend pipeline. Speed is a "
    "Bitcoin + stablecoin payments app (segments: remittance, crypto-curious, "
    "iGaming). An automated pipeline has scored trending videos for topical "
    "relevance. Your ONLY job is to find judgments that are not defensible and "
    "correct them by calling the rejudge_items tool. You do not re-score anything "
    "yourself; the tool does the re-judgment.\n\n"
    "A judgment is INDEFENSIBLE if:\n"
    "  - on_topic=true for a video that only mentions crypto/fintech/money as a "
    "METAPHOR ('like finding bitcoin in 2009'), a HOMONYM (weather 'lightning', "
    "laser 'lightbridge'), or a throwaway/passing reference;\n"
    "  - on_topic=false for a video genuinely ABOUT "
    "crypto/fintech/remittance/iGaming as its subject;\n"
    "  - the fit score is far out of line with how usable the content actually is "
    "for Speed (e.g. substanceless hype scored as a strong fit).\n\n"
    "Judge only from each item's own content and its stated judgment. The tool "
    "result is the authoritative corrected value — if it still shows the old "
    "judgment, that item is NOT fixed, and you must not claim it is.\n\n"
    "Work through the whole set, then stop. When you are done, reply with one short "
    "paragraph naming what you corrected and what you deliberately left alone. Do "
    "not pad the summary, and do not re-report a correction the tool already "
    "confirmed.")


# ------------------------------------------------------------------
# Logging (plain text, framework-agnostic)
# ------------------------------------------------------------------

class _Log:
    """Plain-text log that writes every line to disk AS IT HAPPENS.

    Incremental on purpose. The previous version buffered in memory and wrote the
    file once at the end, so when the 2026-07-27 CI job was SIGKILLed at the
    30-minute timeout it left NO log file at all — the only trace of a 17-minute
    hang was the Actions runner log. Same reason print() is flushed: on CI stdout
    is a pipe, so it is block-buffered and unflushed lines die with the process.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def __call__(self, msg: str):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def flush(self):
        self._fh.flush()


# ------------------------------------------------------------------
# Wall-clock budget
# ------------------------------------------------------------------

class CheckerTimeout(BaseException):
    """Raised when the grading session exceeds its wall-clock budget.

    Inherits BaseException, NOT Exception/TimeoutError, and that is load-bearing.
    The alarm usually fires while blocked inside httpx's stream read, and the
    network stack catches broadly: as a TimeoutError subclass this was swallowed by
    httpcore and re-raised as `httpx.ReadTimeout`, so the `except CheckerTimeout`
    handlers never saw it and the run reported ERROR instead of TIMEOUT (observed
    2026-07-28). BaseException passes straight through `except Exception` while
    still running `finally` blocks and context-manager __exit__.
    """


@contextmanager
def _wall_clock_budget(seconds: int, log):
    """Hard wall-clock bound on the grading session, via SIGALRM.

    A signal is the only thing that interrupts a blocked socket read, which is
    what `for event in stream` is doing when a session goes quiet. The in-loop
    deadline in run_check cannot do it alone: that check only runs when an event
    actually arrives. Degrades to the in-loop deadline where SIGALRM is
    unavailable (non-main thread, or a platform without it).
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM") \
            or threading.current_thread() is not threading.main_thread():
        log(f"Wall-clock budget: soft only (in-loop deadline, {seconds}s) — "
            "SIGALRM unavailable here.")
        yield
        return

    def _fire(_signum, _frame):
        raise CheckerTimeout(f"wall-clock budget of {seconds}s exceeded")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    log(f"Wall-clock budget armed: {seconds}s (hard, SIGALRM).")
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


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
# Run the reviewer pass
# ------------------------------------------------------------------

def run_check(items: list[dict], log, rejudge_stub: bool = False,
              deadline: float | None = None) -> dict:
    """Review `items` and correct indefensible judgments in place.

    Two checks, deliberately separated. First the deterministic invariants, in
    plain Python — a violation there is a code bug, so it hard-fails and is never
    sent to a model. Then the REVIEWER PASS: a plain Messages-API tool loop where
    the model calls the host-side `rejudge_items` tool for each judgment it finds
    indefensible. `items` is mutated in place; the host stays the source of truth.

    There is no Outcomes grader and no Managed Agents session — see the module
    docstring for the measured reasons that was removed.

    `deadline` is a `time.monotonic()` timestamp. Past it the loop stops and the
    verdict is TIMEOUT, never a silent kill (see _wall_clock_budget).
    """
    by_id = {v["id"]: v for v in items}

    # --- 1. Deterministic invariants (hard fail, never reviewed) ---
    violations = tp.verify_fit_invariants(items)
    if violations:
        log(f"DETERMINISTIC INVARIANT VIOLATION ({len(violations)}) — hard fail, no review:")
        for x in violations:
            log(f"    - [{x['kind']}] {x['id']}: {x['detail']}")
        return {"verdict": "HARD_FAIL", "reason": "fit/gate invariant violated (code bug)",
                "violations": violations, "turns": 0, "rejudge_calls": 0}
    log(f"Deterministic invariants OK for all {len(items)} items (fit=0.4/0.3/0.3, gate enforced).")

    # --- 2. Reviewer pass (Messages API tool loop) ---
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                                 timeout=_SDK_TIMEOUT_SECONDS)
    task = (
        "Below is the current relevance/fit judgment for a set of trending videos, as "
        "JSON. Review every item. For each judgment you find indefensible, call "
        "rejudge_items(ids=[...], feedback='...') with a specific reason.\n\n"
        + json.dumps(_compact(items), ensure_ascii=False))
    messages: list[dict] = [{"role": "user", "content": task}]

    verdict = {"verdict": "UNKNOWN", "turns": 0, "rejudge_calls": 0,
               "corrections": [], "unchanged": 0, "timed_out": False, "summary": ""}
    log(f"Reviewer pass: {_REVIEW_MODEL} (effort={_REVIEW_EFFORT}) over {len(items)} items.")

    try:
        for turn in range(1, _MAX_TURNS + 1):
            if deadline is not None and time.monotonic() >= deadline:
                log("  BUDGET EXPIRED (soft, between turns) — stopping the reviewer pass.")
                verdict["timed_out"] = True
                break

            resp = client.messages.create(
                model=_REVIEW_MODEL,
                max_tokens=_REVIEW_MAX_TOKENS,
                system=_REVIEWER_SYSTEM,
                output_config={"effort": _REVIEW_EFFORT},
                tools=[_REJUDGE_TOOL],
                messages=messages,
            )
            verdict["turns"] = turn

            # Check stop_reason BEFORE reading content: on a refusal the content
            # is empty or partial, so indexing it blindly would raise.
            if resp.stop_reason == "refusal":
                cat = getattr(getattr(resp, "stop_details", None), "category", None)
                log(f"  reviewer REFUSED (category={cat}) — no review performed.")
                verdict["verdict"] = "REFUSED"
                return verdict

            messages.append({"role": "assistant", "content": resp.content})
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    verdict["summary"] = " ".join(block.text.split())
                    log(f"  reviewer: {verdict['summary'][:400]}")

            if resp.stop_reason != "tool_use":
                log(f"  reviewer finished on turn {turn} (stop_reason={resp.stop_reason}).")
                break

            # Every tool_result for this turn goes back in ONE user message —
            # splitting them across messages trains the model out of parallel calls.
            results = []
            for block in resp.content:
                if block.type != "tool_use" or block.name != "rejudge_items":
                    continue
                ids = (block.input or {}).get("ids", [])
                fb = (block.input or {}).get("feedback", "")
                verdict["rejudge_calls"] += 1
                log(f"  rejudge_items called: ids={ids} feedback={fb[:100]!r}")
                if rejudge_stub:
                    # Cap test: hand back the SAME (still-bad) judgment so the loop
                    # cannot converge, exercising the turn/budget bounds.
                    payload = [{"id": i, "on_topic": by_id.get(i, {}).get("on_topic"),
                                "fintech_involvement": by_id.get(i, {}).get("fintech_involvement"),
                                "note": "stub: unchanged"} for i in ids]
                    log("    [stub] returning unchanged judgments (forcing non-convergence)")
                else:
                    # Snapshot the judgment BEFORE re-judging: rejudge_items mutates
                    # the item dicts in place, and by_id holds the same references,
                    # so there is nothing left to compare against afterwards. Without
                    # this, a re-judgment that returned the value unchanged still got
                    # counted as a "correction" — which happened for real (t53 came
                    # back at fit 8.2 twice).
                    before = {i: {k: by_id.get(i, {}).get(k)
                                  for k in ("on_topic", "fintech_involvement", "fit_score")}
                              for i in ids}
                    corrected = tp.rejudge_items(items, ids, fb)
                    payload = [{"id": c["id"], "on_topic": c["on_topic"],
                                "fintech_involvement": c["fintech_involvement"],
                                "fit_score": c.get("fit_score"),
                                "reason": c.get("relevance_reason", "")[:160]} for c in corrected]
                    for c in corrected:
                        was = before.get(c["id"], {})
                        now = {"on_topic": c["on_topic"],
                               "fintech_involvement": c["fintech_involvement"],
                               "fit_score": c.get("fit_score")}
                        changed = was != now
                        log(f"    -> {c['id']} re-judged: on_topic={c['on_topic']} "
                            f"fintech={c['fintech_involvement']} fit={c['fit_score']}"
                            f" {'(CHANGED)' if changed else '(unchanged)'}")
                        if changed:
                            verdict["corrections"].append(
                                {"id": c["id"], "was": was, "now": now, "feedback": fb[:200]})
                        else:
                            verdict["unchanged"] += 1
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(payload, ensure_ascii=False)})
            if not results:
                log("  stop_reason was tool_use but no rejudge_items call found — stopping.")
                break
            messages.append({"role": "user", "content": results})
        else:
            log(f"  turn cap reached ({_MAX_TURNS}) — stopping the reviewer pass.")
    except CheckerTimeout as e:
        log(f"  BUDGET EXPIRED (hard, SIGALRM): {e}")
        verdict["timed_out"] = True
    except Exception as e:  # noqa: BLE001
        # Safety net for a library that swallows the alarm and re-wraps it (httpx
        # did exactly that while CheckerTimeout was a TimeoutError). Past the
        # deadline the clock is ground truth, not the exception type.
        if deadline is not None and time.monotonic() >= deadline:
            log(f"  BUDGET EXPIRED (detected via deadline; surfaced as "
                f"{type(e).__name__}: {e})")
            verdict["timed_out"] = True
        else:
            raise

    n, stale = len(verdict["corrections"]), verdict["unchanged"]
    if verdict["timed_out"]:
        verdict["verdict"] = "TIMEOUT"
    elif n:
        verdict["verdict"] = "CORRECTED"
    elif verdict["rejudge_calls"]:
        # The reviewer flagged items but every re-judgment came back identical. That
        # is NOT clean — the judgments it objected to are still in place — and it is
        # not a crash either. Distinct verdict so a human looks: either the reviewer
        # was wrong, or the judge is not responding to the feedback it was given.
        verdict["verdict"] = "FLAGGED_UNCHANGED"
    else:
        verdict["verdict"] = "CLEAN"
    log(f"Reviewer pass done: {n} correction(s) applied, {stale} re-judgment(s) "
        f"returned unchanged, across {verdict['rejudge_calls']} tool call(s) in "
        f"{verdict['turns']} turn(s).")
    return verdict


def check_pipeline_output(budget_seconds: int | None = None) -> dict:
    """Scheduled entry point (called by the Monday trend rebuild). Grades the
    latest real pipeline output, writes a plain-text log, returns the verdict.

    Best-effort: never raises, so it can't block the daily sync — and now also
    time-bounded, so it can't eat the CI job's timeout either. Worst case is a
    logged TIMEOUT verdict after `budget_seconds`.
    """
    budget = _CHECKER_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log = _Log(_LOG_DIR / f"{stamp}.log")
    log("=== Speed trend relevance checker (scheduled, Monday cron) ===")
    started = time.monotonic()
    try:
        # The budget covers load_real_judged too: it re-judges cached items via
        # Claude, so it is a network path that can hang like any other.
        with _wall_clock_budget(budget, log):
            items = load_real_judged()
            log(f"Grading {len(items)} candidate items from the latest pipeline output.")
            verdict = run_check(items, log, deadline=started + budget)
    except CheckerTimeout as e:
        # Fired outside the tool loop (e.g. during load_real_judged), so run_check
        # never got to record it.
        log(f"CHECKER TIMEOUT: {e}")
        verdict = {"verdict": "TIMEOUT", "turns": 0, "timed_out": True}
    except Exception as e:
        import traceback
        elapsed = time.monotonic() - started
        if elapsed >= budget:   # a re-wrapped timeout, not a genuine error
            log(f"CHECKER TIMEOUT (surfaced as {type(e).__name__}: {e})")
            verdict = {"verdict": "TIMEOUT", "turns": 0, "timed_out": True}
        else:
            log(f"CHECKER ERROR: {type(e).__name__}: {e}")
            log(traceback.format_exc())
            verdict = {"verdict": "ERROR", "turns": 0}
    log("")
    log(f"=== VERDICT: {verdict.get('verdict')} "
        f"(corrections={len(verdict.get('corrections') or [])}, "
        f"tool_calls={verdict.get('rejudge_calls')}, turns={verdict.get('turns')}, "
        f"elapsed={time.monotonic() - started:.0f}s of {budget}s budget) ===")
    log.flush()
    # The log IS the deliverable now that there is no graded verdict to publish, so
    # mirror it to a stable filename the deploy step can pick up (docs/*.log inside a
    # subdirectory is not in _deploy_dashboard's file list and dies with the runner).
    try:
        _LATEST_LOG.write_text(log.path.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"(log mirrored to {_LATEST_LOG.relative_to(_ROOT)} for publishing)")
    except Exception as e:  # noqa: BLE001 — mirroring must never fail the sync
        log(f"(could not mirror log: {type(e).__name__}: {e})")
    return verdict


def main(argv: list[str]) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log = _Log(_LOG_DIR / f"{stamp}.log")
    rejudge_stub = "--rejudge-stub" in argv
    judged_path = None
    if "--judged" in argv:
        judged_path = argv[argv.index("--judged") + 1]
    budget = _CHECKER_BUDGET_SECONDS
    if "--budget" in argv:
        budget = int(argv[argv.index("--budget") + 1])

    log("=== Speed trend relevance checker (reviewer pass) ===")
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
        log("MODE: --rejudge-stub (cap test — corrections will NOT converge)")

    started = time.monotonic()
    try:
        with _wall_clock_budget(budget, log):
            verdict = run_check(items, log, rejudge_stub=rejudge_stub,
                                deadline=started + budget)
    except CheckerTimeout as e:
        log(f"CHECKER TIMEOUT: {e}")
        verdict = {"verdict": "TIMEOUT", "turns": 0, "timed_out": True}
    except Exception as e:
        import traceback
        if time.monotonic() - started >= budget:   # re-wrapped timeout, not an error
            log(f"CHECKER TIMEOUT (surfaced as {type(e).__name__}: {e})")
            verdict = {"verdict": "TIMEOUT", "turns": 0, "timed_out": True}
        else:
            log(f"CHECKER ERROR: {type(e).__name__}: {e}")
            log(traceback.format_exc())
            log.flush()
            return 2

    log("")
    log("=== VERDICT ===")
    log(f"  result: {verdict['verdict']}")
    log(f"  corrections applied: {len(verdict.get('corrections') or [])}")
    log(f"  re-judgments returned unchanged: {verdict.get('unchanged')}")
    log(f"  rejudge_items calls: {verdict.get('rejudge_calls')}")
    log(f"  reviewer turns: {verdict.get('turns')}")
    log(f"  elapsed: {time.monotonic() - started:.0f}s of {budget}s budget")
    log.flush()
    log(f"(log written to {log.path.relative_to(_ROOT)})")
    # CLEAN and CORRECTED are both successful reviews — CORRECTED just means it
    # found something, which is the checker doing its job, not a failure.
    return 0 if verdict["verdict"] in ("CLEAN", "CORRECTED") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
