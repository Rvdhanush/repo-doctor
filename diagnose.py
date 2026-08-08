#!/usr/bin/env python3
"""repo-doctor diagnosis — increment 1. Diagnose only, never fix.

Reads a run already captured by runner.py and asks an LLM what actually went
wrong. Nothing here touches a repo, a sandbox, or a dependency: the input is a
JSON file on disk and the output is JSON on stdout.

    python diagnose.py results/20260808T164042Z
    python diagnose.py --all            # every failed run under results/
    python diagnose.py <run_dir> --json # machine-readable, for piping

Diagnosing a stored run rather than re-running the repo is deliberate: install
failures take minutes to reproduce and the captured log is identical every time,
so prompt iteration should not pay that cost.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repo_doctor.config import build_llm_client, load_config
from repo_doctor.diagnosis import diagnose_run
from repo_doctor.llm import LLMError

FAILED_STATUSES = {"install_failed", "import_failed", "clone_failed", "unsupported"}


def load_run(run_dir: Path) -> dict:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise FileNotFoundError(f"No run.json in {run_dir}")
    return json.loads(run_json.read_text(encoding="utf-8"))


def read_install_file(run: dict, run_dir: Path) -> str:
    """The dependency file the repo declared, as captured at clone time.

    Runs produced before the harness captured this fall back to the raw log
    directory; absent is fine, the prompt adapts.
    """
    declared = run.get("declared_dependencies") or {}
    if declared.get("content"):
        return declared["content"]
    fallback = run_dir / "logs" / "declared_dependencies.txt"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8", errors="replace")
    return ""


def print_human(run: dict, diag) -> None:
    outcome = run.get("outcome", {})
    target = run.get("target", {})
    bar = "-" * 72

    print(bar)
    print(f"  {target.get('url', '?')}")
    print(f"  run {run.get('run_id')}   status: {outcome.get('status')}"
          f"   failing step: {outcome.get('failing_step')}")
    print(bar)
    print(f"  CATEGORY     {diag.category}   (confidence: {diag.confidence})")
    print(f"  WHAT FAILED  {_wrap(diag.what_failed)}")
    print(f"  WHY          {_wrap(diag.why)}")
    if diag.evidence:
        print(f"  EVIDENCE     {_wrap(diag.evidence)}")
    if diag.fix_category:
        print(f"  FIX CLASS    {_wrap(diag.fix_category)}")
    if diag.secondary_issues:
        for i, issue in enumerate(diag.secondary_issues):
            label = "ALSO" if i == 0 else "    "
            print(f"  {label:<12} {_wrap(issue)}")
    if diag.human_note:
        print(f"  HUMAN NEEDS  {_wrap(diag.human_note)}")
    if diag.validation_notes:
        print(f"  ! schema     {'; '.join(diag.validation_notes)}")
    llm = diag.llm
    print(f"  cost         {llm.get('provider')}/{llm.get('model')}  "
          f"{llm.get('total_tokens')} tokens  {llm.get('latency_s')}s")
    print()


def _wrap(text: str, width: int = 58, indent: int = 15) -> str:
    import textwrap
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width) or [""]
    pad = " " * indent
    return ("\n" + pad).join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnose.py",
        description="Diagnose a captured repo-doctor run with an LLM. Does not fix anything.",
    )
    parser.add_argument("run_dir", nargs="?", help="results/<run_id> directory")
    parser.add_argument("--all", action="store_true",
                        help="diagnose every failed run under results/")
    parser.add_argument("--config", default="configs/settings.yaml")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--save", action="store_true",
                        help="write the diagnosis back into the run's run.json")
    args = parser.parse_args(argv)

    # Model replies routinely contain characters the Windows console codepage
    # (cp1252) cannot encode. Without this, printing a valid diagnosis crashes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parent
    cfg = load_config(args.config, project_root=project_root)
    client = build_llm_client(cfg)

    if not client.configured_providers:
        print("[repo-doctor] No LLM provider configured. Add a key to .env "
              "(see .env.example).", file=sys.stderr)
        return 2

    if args.all:
        run_dirs = []
        for candidate in sorted(cfg.results_root.iterdir()):
            run_json = candidate / "run.json"
            if not run_json.exists():
                continue
            status = json.loads(run_json.read_text(encoding="utf-8"))["outcome"]["status"]
            if status in FAILED_STATUSES:
                run_dirs.append(candidate)
    elif args.run_dir:
        run_dirs = [Path(args.run_dir)]
    else:
        parser.error("give a run directory or --all")

    if not args.json:
        providers = ", ".join(p.name for p in client.configured_providers)
        print(f"[repo-doctor] diagnosing {len(run_dirs)} run(s) via: {providers}\n")

    results = []
    exit_code = 0
    for run_dir in run_dirs:
        try:
            run = load_run(run_dir)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"[repo-doctor] skipping {run_dir}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        try:
            diag = diagnose_run(
                run, client,
                install_file_content=read_install_file(run, run_dir),
                tail_lines=cfg.llm.tail_lines,
                max_chars=cfg.llm.max_chars,
            )
        except LLMError as exc:
            print(f"[repo-doctor] {run_dir.name}: diagnosis failed: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        results.append({"run_id": run.get("run_id"),
                        "url": run.get("target", {}).get("url"),
                        "status": run.get("outcome", {}).get("status"),
                        "diagnosis": diag.as_dict()})

        if args.json:
            pass
        else:
            print_human(run, diag)

        if args.save:
            run["diagnosis"] = diag.as_dict()
            (run_dir / "run.json").write_text(
                json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
