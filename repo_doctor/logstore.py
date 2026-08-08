"""Structured logging for a run.

This is the real product of increment 0. Increment 1 (LLM diagnosis) consumes
run.json as its input and increment 3 (telemetry) consumes the same records, so
the schema matters more than anything else built here.

Three artifacts per run, under results/<run_id>/:

  logs/<step>.stdout.txt  raw, untruncated, exactly what the command emitted
  events.jsonl            append-only, flushed as it happens, so a ten-minute
                          install is observable while it is still running
  run.json                the single structured document describing the run
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Terminal outcomes. Anything that is not "ok" must carry a failing_step and a
# human-readable summary_line.
STATUS_OK = "ok"
STATUS_CLONE_FAILED = "clone_failed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_INSTALL_FAILED = "install_failed"
STATUS_IMPORT_FAILED = "import_failed"
STATUS_HARNESS_ERROR = "harness_error"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truncate_middle(text: str, head_lines: int, tail_lines: int) -> tuple[str, bool]:
    """Keep the first `head_lines` and last `tail_lines`, dropping the middle.

    Truncating the middle rather than the tail is deliberate: pip prints the
    invocation context at the top and the actual error at the bottom, and the
    middle is download-progress noise. Cutting the tail would throw away the one
    part of the output that matters.
    """
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return text, False
    omitted = len(lines) - head_lines - tail_lines
    kept = (
        lines[:head_lines]
        + [f"... [repo-doctor] {omitted} lines omitted; full output in logs/ ..."]
        + lines[-tail_lines:]
    )
    return "\n".join(kept), True


class RunLog:
    """Accumulates the record of one run and writes it to disk."""

    def __init__(self, run_id: str, url: str, results_root: Path,
                 head_lines: int = 200, tail_lines: int = 400):
        self.run_id = run_id
        self.head_lines = head_lines
        self.tail_lines = tail_lines

        self.dir = Path(results_root) / run_id
        self.logs_dir = self.dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.run_path = self.dir / "run.json"

        self._started_at = datetime.now(timezone.utc)
        self.doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": _utcnow(),
            "finished_at": None,
            "duration_s": 0.0,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "target": {"url": url, "owner": None, "repo": None,
                       "resolved_commit": None, "default_branch": None},
            "sandbox": {},
            "install_detection": {},
            "import_detection": {},
            "steps": [],
            "outcome": {"status": None, "failing_step": None,
                        "exit_code": None, "summary_line": None},
        }

    # -- events ------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        """Append one line to events.jsonl and flush it immediately."""
        record = {"ts": _utcnow(), "run_id": self.run_id, "kind": kind, **fields}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    # -- steps -------------------------------------------------------------

    def add_step(self, name: str, result, *, note: str = "") -> dict:
        """Record an executed command: raw output to disk, truncated view inline."""
        self._write_raw(name, "stdout", result.stdout)
        self._write_raw(name, "stderr", result.stderr)

        out, out_trunc = truncate_middle(result.stdout, self.head_lines, self.tail_lines)
        err, err_trunc = truncate_middle(result.stderr, self.head_lines, self.tail_lines)

        step = {
            "name": name,
            "argv": result.argv,
            "cwd": result.cwd,
            "exit_code": result.exit_code,
            "duration_s": round(result.duration_s, 3),
            "timed_out": result.timed_out,
            "stdout": out,
            "stderr": err,
            "truncated": out_trunc or err_trunc,
            "raw_logs": {
                "stdout": f"logs/{name}.stdout.txt",
                "stderr": f"logs/{name}.stderr.txt",
            },
            "note": note,
        }
        self.doc["steps"].append(step)
        self.event(
            "step",
            name=name,
            exit_code=result.exit_code,
            duration_s=round(result.duration_s, 3),
            timed_out=result.timed_out,
        )
        return step

    def add_skipped_step(self, name: str, reason: str) -> dict:
        """Record a step that was never reached, so the pipeline is legible end to end."""
        step = {"name": name, "skipped": True, "reason": reason}
        self.doc["steps"].append(step)
        self.event("step_skipped", name=name, reason=reason)
        return step

    def _write_raw(self, name: str, stream: str, text: str) -> None:
        (self.logs_dir / f"{name}.{stream}.txt").write_text(text or "", encoding="utf-8")

    # -- fields ------------------------------------------------------------

    def set_target(self, **fields: Any) -> None:
        self.doc["target"].update(fields)

    def set_sandbox(self, info) -> None:
        self.doc["sandbox"] = {
            "image": info.image,
            "image_id": info.image_id,
            "image_digest": info.image_digest,
            "container_id": info.container_id,
            "container_name": info.container_name,
            "python_version": info.python_version,
            "pip_version": info.pip_version,
            "limits": info.limits,
        }

    def set_install_detection(self, plan) -> None:
        self.doc["install_detection"] = plan.as_dict()
        self.event("install_detected", strategy=plan.strategy, chosen=plan.chosen)

    def set_import_detection(self, target) -> None:
        self.doc["import_detection"] = target.as_dict()

    def set_outcome(self, status: str, *, failing_step: str | None = None,
                    exit_code: int | None = None, summary_line: str = "") -> None:
        self.doc["outcome"] = {
            "status": status,
            "failing_step": failing_step,
            "exit_code": exit_code,
            "summary_line": summary_line,
        }

    # -- finalisation ------------------------------------------------------

    def write(self) -> Path:
        """Write run.json. Called from a finally block, so it must not raise."""
        now = datetime.now(timezone.utc)
        self.doc["finished_at"] = now.isoformat(timespec="seconds")
        self.doc["duration_s"] = round((now - self._started_at).total_seconds(), 3)
        if self.doc["outcome"]["status"] is None:
            # A run that reached here without an outcome ended abnormally. Say so
            # rather than leaving a record that implies nothing went wrong.
            self.doc["outcome"] = {
                "status": STATUS_HARNESS_ERROR,
                "failing_step": None,
                "exit_code": None,
                "summary_line": "Run ended without setting an outcome.",
            }
        self.run_path.write_text(
            json.dumps(self.doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.event("run_finished", status=self.doc["outcome"]["status"],
                   duration_s=self.doc["duration_s"])
        return self.run_path
