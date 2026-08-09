#!/usr/bin/env python3
"""repo-doctor telemetry — increment 3. Surfaces every attempt, its time, and
its LLM token cost, aggregated across every run under results/.

Strictly read-only: parses run.json files already on disk. Touches no Docker
container and no LLM API — the numbers it reports were all captured by
runner.py / diagnose.py / the fix loop at the time each attempt actually ran.

    python telemetry.py                 # a table across every run under results/
    python telemetry.py <run_id>        # just one run
    python telemetry.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repo_doctor.config import load_config
from repo_doctor.telemetry import collect, render_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telemetry.py",
        description="Surface every attempt, its time, and its LLM token cost, "
                    "aggregated across every run under results/. Read-only.",
    )
    parser.add_argument("run_id", nargs="?",
                        help="a single results/<run_id> to report on (default: every run)")
    parser.add_argument("--config", default="configs/settings.yaml")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    # Same rationale as runner.py/diagnose.py: captured output and repo URLs
    # routinely contain characters the Windows console codepage can't encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parent
    cfg = load_config(args.config, project_root=project_root)

    rows = collect(cfg.results_root, args.run_id)
    if not rows:
        print(f"[repo-doctor] no runs found under {cfg.results_root}"
             + (f" matching {args.run_id!r}" if args.run_id else ""), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([r.as_dict() for r in rows], indent=2, ensure_ascii=False))
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
