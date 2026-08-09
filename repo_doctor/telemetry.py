"""Aggregate every recorded attempt, its time, and its LLM cost. Increment 3.

Strictly read-only: parses run.json files already on disk under results/. Never
touches Docker or an LLM, and never re-diagnoses anything -- it surfaces numbers
`logstore.py` (attempt/step timing), `diagnosis.py`, and `fix.py` (token usage,
via `LLMResult.as_dict()`) already recorded.

"Dashboard" here means a CLI table, not a web product -- CLAUDE.md's scope
discipline rules that out for v1, and every other tool in this project
(runner.py, diagnose.py) is a CLI for the same reason.

Cost is reported in tokens, not dollars: token counts are the one number this
codebase actually captures per call (repo_doctor/llm.py never records a price),
and inventing a $ figure from an unconfigured rate would be exactly the kind of
unearned precision CLAUDE.md's honesty rule warns against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMCallSummary:
    """One diagnosis or fix-proposal call, wherever in the run it happened."""

    kind: str          # "diagnosis" | "proposal"
    attempt: int | None  # None for a standalone (non-fix-loop) diagnosis
    provider: str
    model: str
    total_tokens: int
    latency_s: float

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "attempt": self.attempt, "provider": self.provider,
            "model": self.model, "total_tokens": self.total_tokens, "latency_s": self.latency_s,
        }


@dataclass
class RunTelemetry:
    run_id: str
    url: str
    status: str
    duration_s: float
    attempts_used: int          # 1 (initial) + however many the fix loop ran
    fix_stopped_reason: str | None
    llm_calls: list[LLMCallSummary] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.llm_calls)

    @property
    def total_llm_latency_s(self) -> float:
        return round(sum(c.latency_s for c in self.llm_calls), 3)

    @property
    def providers_used(self) -> list[str]:
        seen: list[str] = []
        for c in self.llm_calls:
            if c.provider not in seen:
                seen.append(c.provider)
        return seen

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "url": self.url,
            "status": self.status,
            "duration_s": self.duration_s,
            "attempts_used": self.attempts_used,
            "fix_stopped_reason": self.fix_stopped_reason,
            "llm_calls": len(self.llm_calls),
            "total_tokens": self.total_tokens,
            "total_llm_latency_s": self.total_llm_latency_s,
            "providers_used": self.providers_used,
        }


def _llm_call(llm: dict | None, kind: str, attempt: int | None) -> LLMCallSummary | None:
    if not llm:
        return None
    return LLMCallSummary(
        kind=kind, attempt=attempt,
        provider=llm.get("provider", "?"), model=llm.get("model", "?"),
        total_tokens=llm.get("total_tokens", 0), latency_s=llm.get("latency_s", 0.0),
    )


def summarize_run(run: dict) -> RunTelemetry:
    """Turn one loaded run.json document into a RunTelemetry record."""
    outcome = run.get("outcome", {})
    fix_loop = run.get("fix_loop")

    calls: list[LLMCallSummary] = []

    # A standalone `--diagnose` (no --fix, or --fix that never ran) writes a
    # top-level "diagnosis" key.
    standalone = _llm_call((run.get("diagnosis") or {}).get("llm"), "diagnosis", None)
    if standalone:
        calls.append(standalone)

    attempts_used = 1  # the initial install+import attempt always happens
    stopped_reason = None
    if fix_loop:
        stopped_reason = fix_loop.get("stopped_reason")
        attempts = fix_loop.get("attempts") or []
        attempts_used += len(attempts)
        for a in attempts:
            diag_call = _llm_call((a.get("diagnosis") or {}).get("llm"), "diagnosis", a.get("index"))
            if diag_call:
                calls.append(diag_call)
            prop_call = _llm_call((a.get("proposal") or {}).get("llm"), "proposal", a.get("index"))
            if prop_call:
                calls.append(prop_call)

    return RunTelemetry(
        run_id=run.get("run_id", "?"),
        url=(run.get("target") or {}).get("url", "?"),
        status=outcome.get("status", "?"),
        duration_s=run.get("duration_s", 0.0),
        attempts_used=attempts_used,
        fix_stopped_reason=stopped_reason,
        llm_calls=calls,
    )


def collect(results_root: Path, run_id: str | None = None) -> list[RunTelemetry]:
    """Load and summarize every run under results_root, or just one."""
    if run_id:
        candidates = [results_root / run_id]
    else:
        candidates = sorted(p for p in results_root.iterdir() if p.is_dir())

    rows: list[RunTelemetry] = []
    for run_dir in candidates:
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        try:
            run = json.loads(run_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append(summarize_run(run))
    return rows


def render_table(rows: list[RunTelemetry]) -> str:
    """A fixed-width text table -- the "dashboard". Stdlib only, no dependency."""
    if not rows:
        return "No runs found."

    headers = ("RUN ID", "STATUS", "DURATION", "ATTEMPTS", "LLM CALLS", "TOKENS", "STOPPED REASON")
    lines_data = [
        (r.run_id, r.status, f"{r.duration_s:.1f}s", str(r.attempts_used),
         str(len(r.llm_calls)), str(r.total_tokens), r.fix_stopped_reason or "")
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in lines_data)) for i, h in enumerate(headers)]

    def fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    out = [fmt(headers), fmt(tuple("-" * w for w in widths))]
    out.extend(fmt(row) for row in lines_data)

    total_runs = len(rows)
    total_tokens = sum(r.total_tokens for r in rows)
    total_calls = sum(len(r.llm_calls) for r in rows)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    status_breakdown = ", ".join(f"{count} {status}" for status, count in sorted(by_status.items()))

    out.append("")
    out.append(f"{total_runs} run(s) — {status_breakdown} — "
              f"{total_calls} LLM call(s), {total_tokens} token(s) total")
    return "\n".join(out)
