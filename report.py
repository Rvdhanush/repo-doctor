#!/usr/bin/env python3
"""repo-doctor report — increment 4. The final, honest per-repo verdict.

Reads a run already captured by runner.py (and, if run, diagnosed and/or
fixed) and renders one report: fixed or not, every fix attempted, what it
cost, and — if it's still broken — the specific diagnosis and what a human
needs to do next. Strictly read-only, makes no LLM call of its own; a run
with no captured diagnosis gets an honest "none captured" rather than a
report.py-triggered API call.

    python report.py results/<run_id>
    python report.py --all
    python report.py results/<run_id> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repo_doctor.config import load_config
from repo_doctor.report import build_report, render_human


def load_run(run_dir: Path) -> dict:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise FileNotFoundError(f"No run.json in {run_dir}")
    return json.loads(run_json.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="report.py",
        description="Render the final, honest report for a captured repo-doctor run. "
                    "Read-only — never calls an LLM itself.",
    )
    parser.add_argument("run_dir", nargs="?", help="results/<run_id> directory")
    parser.add_argument("--all", action="store_true", help="report on every run under results/")
    parser.add_argument("--config", default="configs/settings.yaml")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the human report")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parent
    cfg = load_config(args.config, project_root=project_root)

    if args.all:
        run_dirs = [p for p in sorted(cfg.results_root.iterdir())
                   if p.is_dir() and (p / "run.json").exists()]
    elif args.run_dir:
        run_dirs = [Path(args.run_dir)]
    else:
        parser.error("give a run directory or --all")

    reports = []
    exit_code = 0
    for run_dir in run_dirs:
        try:
            run = load_run(run_dir)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"[repo-doctor] skipping {run_dir}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        report = build_report(run)
        reports.append(report)
        if not args.json:
            print(render_human(report))
            print()

    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2, ensure_ascii=False))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
