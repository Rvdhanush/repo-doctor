"""The final, honest per-repo verdict. Increment 4.

SPEC.md section 7 is the whole point of this module: "A 'could not fix' report
must be as informative as a success: the fixes attempted, the specific
blocker, and the human next step." This turns one run.json into exactly that
-- fixed or not, what was tried, what it cost, and (if it's still broken) the
specific diagnosis explaining why, reusing whatever diagnose_run already
produced rather than restating the raw error.

Strictly read-only and makes no LLM call of its own. If a run has neither a
standalone diagnosis nor a fix loop, this module does not quietly reach for an
LLM to fill the gap -- it says so, and names the command that would. That is
the same opt-in discipline `runner.py --diagnose`/`--fix` already established:
nothing here spends a token the operator did not ask for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .telemetry import RunTelemetry, summarize_run

# outcome.status values where a fix could, in principle, have been attempted.
_FIXABLE_STATUSES = {"install_failed", "import_failed"}


@dataclass
class AttemptSummary:
    """One fix-loop iteration, condensed to what a human reading the report needs."""

    index: int
    diagnosis_category: str | None
    diagnosis_confidence: str | None
    what_failed: str
    action: str | None
    action_reason: str
    applied: bool
    result_status: str | None

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "diagnosis_category": self.diagnosis_category,
            "diagnosis_confidence": self.diagnosis_confidence,
            "what_failed": self.what_failed,
            "action": self.action,
            "action_reason": self.action_reason,
            "applied": self.applied,
            "result_status": self.result_status,
        }


@dataclass
class Report:
    run_id: str
    url: str
    commit: str
    status: str
    verdict: str          # fixed | succeeded | not_fixed | unsupported | clone_failed | harness_error | unknown
    headline: str          # one-line human summary of the verdict
    summary_line: str      # outcome.summary_line, verbatim
    fix_attempts: list[AttemptSummary]
    final_diagnosis: dict | None   # Diagnosis.as_dict(), whichever is most current
    telemetry: RunTelemetry

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "url": self.url,
            "commit": self.commit,
            "status": self.status,
            "verdict": self.verdict,
            "headline": self.headline,
            "summary_line": self.summary_line,
            "fix_attempts": [a.as_dict() for a in self.fix_attempts],
            "final_diagnosis": self.final_diagnosis,
            "telemetry": self.telemetry.as_dict(),
        }


def _attempt_summaries(fix_loop: dict | None) -> list[AttemptSummary]:
    if not fix_loop:
        return []
    out = []
    for a in fix_loop.get("attempts") or []:
        diag = a.get("diagnosis") or {}
        prop = a.get("proposal") or {}
        out.append(AttemptSummary(
            index=a.get("index", 0),
            diagnosis_category=diag.get("category"),
            diagnosis_confidence=diag.get("confidence"),
            what_failed=diag.get("what_failed", ""),
            action=prop.get("action"),
            action_reason=prop.get("reason") or prop.get("give_up_note") or "",
            applied=bool(a.get("applied")),
            result_status=a.get("result_status"),
        ))
    return out


def _final_diagnosis(run: dict, fix_loop: dict | None) -> dict | None:
    """The diagnosis that best explains the run's FINAL state, if one exists.

    The fix loop's last attempt is more current than a standalone `--diagnose`
    (which, if both ran, explains the pre-fix-loop failure) -- prefer it.
    """
    if fix_loop and fix_loop.get("attempts"):
        last_diagnosis = fix_loop["attempts"][-1].get("diagnosis")
        if last_diagnosis:
            return last_diagnosis
    return run.get("diagnosis")


def build_report(run: dict) -> Report:
    """Assemble a Report from a loaded run.json document."""
    outcome = run.get("outcome", {})
    target = run.get("target", {})
    status = outcome.get("status", "?")
    fix_loop = run.get("fix_loop")
    attempts = _attempt_summaries(fix_loop)
    n = len(attempts)

    if status == "ok":
        if fix_loop and fix_loop.get("stopped_reason") == "success":
            verdict = "fixed"
            headline = f"FIXED — installed and imported after {n} fix attempt(s)."
        else:
            verdict = "succeeded"
            headline = "SUCCEEDED — installed and imported, no fixes needed."
    elif status in _FIXABLE_STATUSES:
        if fix_loop:
            verdict = "not_fixed"
            headline = (f"NOT FIXED — still {status} after {n} attempt(s) "
                       f"(stopped: {fix_loop.get('stopped_reason')}).")
        else:
            verdict = "not_fixed"
            headline = f"FAILED — {status}; no fix was attempted (re-run with --fix)."
    elif status == "unsupported":
        verdict = "unsupported"
        headline = "UNSUPPORTED — " + (outcome.get("summary_line") or "no install file found.")
    elif status == "clone_failed":
        verdict = "clone_failed"
        headline = "CLONE FAILED — " + (outcome.get("summary_line") or "could not clone the repo.")
    elif status == "harness_error":
        verdict = "harness_error"
        headline = ("HARNESS ERROR — " + (outcome.get("summary_line") or "") +
                   " — NOT a verdict about the target repo.")
    else:
        verdict = "unknown"
        headline = f"UNKNOWN outcome status: {status!r}"

    return Report(
        run_id=run.get("run_id", "?"),
        url=target.get("url", "?"),
        commit=(target.get("resolved_commit") or "")[:12],
        status=status,
        verdict=verdict,
        headline=headline,
        summary_line=outcome.get("summary_line", ""),
        fix_attempts=attempts,
        final_diagnosis=_final_diagnosis(run, fix_loop),
        telemetry=summarize_run(run),
    )


def _wrap(text: str, width: int = 72, indent: int = 4) -> str:
    import textwrap
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width) or [""]
    pad = " " * indent
    return ("\n" + pad).join(lines)


def render_human(report: Report) -> str:
    bar = "=" * 78
    out: list[str] = [bar, f"  repo-doctor report — {report.url}"]
    commit_bit = f"   commit {report.commit}" if report.commit else ""
    out.append(f"  run {report.run_id}{commit_bit}")
    out.append(bar)
    out.append("")
    out.append(report.headline)
    if report.summary_line and report.summary_line not in report.headline:
        out.append(f"  {_wrap(report.summary_line, indent=2)}")
    out.append("")

    if report.fix_attempts:
        out.append(f"ATTEMPTS ({len(report.fix_attempts)})")
        for a in report.fix_attempts:
            out.append(f"  {a.index}. diagnosed: {a.diagnosis_category} "
                      f"(confidence: {a.diagnosis_confidence}) — {_wrap(a.what_failed, indent=17)}")
            if a.action:
                out.append(f"     tried: {a.action} — {_wrap(a.action_reason, indent=13)}")
            out.append(f"     result: {a.result_status}")
        out.append("")

    # harness_error is deliberately excluded: it is not a verdict about the
    # target repo, so there is nothing about the repo to explain here.
    if report.verdict in ("not_fixed", "unsupported", "clone_failed"):
        if report.final_diagnosis:
            d = report.final_diagnosis
            out.append("WHY IT'S STILL BROKEN")
            out.append(f"  category      {d.get('category')}  (confidence: {d.get('confidence')})")
            out.append(f"  what failed   {_wrap(d.get('what_failed', ''), indent=16)}")
            out.append(f"  why           {_wrap(d.get('why', ''), indent=16)}")
            if d.get("evidence"):
                out.append(f"  evidence      {_wrap(d.get('evidence', ''), indent=16)}")
            human_note = d.get("human_note") or (
                "None recorded — see the diagnosis above for the fix class to try next."
            )
            out.append(f"  human needs   {_wrap(human_note, indent=16)}")
            out.append("")
        else:
            out.append("No diagnosis was captured for this run.")
            out.append("  Re-run with `--diagnose` to explain it, or `--fix` to also attempt a fix.")
            out.append("")

    t = report.telemetry
    out.append("COST")
    out.append(f"  {len(t.llm_calls)} LLM call(s), {t.total_tokens} token(s), "
              f"{t.total_llm_latency_s}s API latency, {t.duration_s:.1f}s wall clock total")
    out.append(bar)
    return "\n".join(out)
