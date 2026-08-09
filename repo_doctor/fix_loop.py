"""The fix loop: propose a fix, apply it, re-run install+import, retry. Increment 2.

This is the orchestrator that ties diagnosis.py (what's wrong) and fix.py (what
to try next) to the sandbox and the install/import pipeline (pipeline.py). It
never runs unless the initial attempt already failed, and it stops on the first
of: install+import succeeding, the model choosing to give up, the diagnosis
landing in a category nothing in this sandbox can fix, or the attempt cap
(SPEC.md section 4: "cap fix attempts, never loop unbounded").

Every attempt is recorded via RunLog.add_fix_attempt before the loop moves on,
so a run that exhausts the cap still leaves a complete, honest history of what
was tried -- the raw material increment 4's reporting needs.
"""

from __future__ import annotations

import sys

from . import logstore
from .config import Config
from .diagnosis import diagnose_run
from .fix import FixProposal, propose_fix
from .llm import LLMClient, LLMError
from .logstore import RunLog
from .pipeline import install_and_import
from .sandbox import Sandbox

# Diagnosis categories that mean "nothing in this sandbox can fix this" --
# calling propose_fix for these would spend a call reaching a foregone
# conclusion diagnosis.py already reached (see its CATEGORIES docstrings).
_UNFIXABLE_CATEGORIES = {"unfixable", "repo_unavailable", "no_install_file"}


def apply_fix(sandbox: Sandbox, cfg: Config, log: RunLog, repo_dir: str,
              index: int, proposal: FixProposal) -> bool:
    """Apply one proposal inside the sandbox. Returns whether it applied cleanly."""
    if proposal.action == "apt_install":
        update, install = sandbox.apt_install(proposal.packages, timeout=cfg.timeouts.install)
        log.add_step(f"attempt{index}_apt_update", update)
        log.add_step(f"attempt{index}_apt_install", install,
                    note=f"packages: {', '.join(proposal.packages)}")
        return install.ok
    if proposal.action == "edit_dependency_file":
        result = sandbox.write_file(f"{repo_dir}/{proposal.file_path}", proposal.file_content,
                                    timeout=cfg.timeouts.probe)
        log.add_step(f"attempt{index}_edit_file", result, note=f"rewrote {proposal.file_path}")
        return result.ok
    return False  # give_up never reaches here; caller stops before calling apply_fix


def run_fix_loop(sandbox: Sandbox, cfg: Config, log: RunLog, repo_dir: str, repo: str,
                 skip_import: bool, client: LLMClient, initial_status: str,
                 initial_exit_code: int) -> tuple[str, int]:
    """Diagnose -> propose -> apply -> re-run, up to cfg.limits.attempt_cap times.

    `log.doc` is read directly as the "run.json so far" that diagnose_run and
    propose_fix expect -- it has the same shape as the file that will eventually
    be written, so there is no need to serialize and reload mid-run.
    """
    cap = cfg.limits.attempt_cap
    log.start_fix_loop(cap)
    status, exit_code = initial_status, initial_exit_code
    prior_attempts: list[dict] = []

    for index in range(1, cap + 1):
        print(f"[repo-doctor] fix attempt {index}/{cap}: diagnosing ...", flush=True)
        try:
            diagnosis = diagnose_run(
                log.doc, client,
                install_file_content=(log.doc.get("declared_dependencies") or {}).get("content", ""),
                tail_lines=cfg.llm.tail_lines,
                max_chars=cfg.llm.max_chars,
            )
        except LLMError as exc:
            print(f"[repo-doctor] fix attempt {index}: diagnosis failed: {exc}", file=sys.stderr)
            log.add_fix_attempt(index, diagnosis=None, proposal=None,
                                applied=False, result_status="diagnosis_failed")
            log.finish_fix_loop(status, "diagnosis_failed")
            return status, exit_code

        print(f"[repo-doctor] fix attempt {index}: category={diagnosis.category} "
              f"(confidence: {diagnosis.confidence})")

        if diagnosis.category in _UNFIXABLE_CATEGORIES:
            log.add_fix_attempt(index, diagnosis=diagnosis.as_dict(), proposal=None,
                                applied=False, result_status="unfixable_category")
            log.finish_fix_loop(status, "unfixable_category")
            print(f"[repo-doctor] fix loop stopping: category '{diagnosis.category}' "
                  "is not fixable inside the sandbox.")
            return status, exit_code

        dependency_file_path = (log.doc.get("install_detection") or {}).get("chosen", "")
        dependency_file_content = (log.doc.get("declared_dependencies") or {}).get("content", "")

        try:
            proposal = propose_fix(
                log.doc, diagnosis, client,
                dependency_file_path=dependency_file_path,
                dependency_file_content=dependency_file_content,
                prior_attempts=prior_attempts,
            )
        except LLMError as exc:
            print(f"[repo-doctor] fix attempt {index}: proposal failed: {exc}", file=sys.stderr)
            log.add_fix_attempt(index, diagnosis=diagnosis.as_dict(), proposal=None,
                                applied=False, result_status="proposal_failed")
            log.finish_fix_loop(status, "proposal_failed")
            return status, exit_code

        print(f"[repo-doctor] fix attempt {index}: proposing {proposal.action} "
              f"— {proposal.reason or proposal.give_up_note}")

        if proposal.action == "give_up":
            log.add_fix_attempt(index, diagnosis=diagnosis.as_dict(), proposal=proposal.as_dict(),
                                applied=False, result_status="give_up")
            log.finish_fix_loop(status, "give_up")
            print(f"[repo-doctor] fix loop stopping: model gave up — {proposal.give_up_note}")
            return status, exit_code

        applied = apply_fix(sandbox, cfg, log, repo_dir, index, proposal)
        if not applied:
            log.add_fix_attempt(index, diagnosis=diagnosis.as_dict(), proposal=proposal.as_dict(),
                                applied=False, result_status="apply_failed")
            prior_attempts.append(log.doc["fix_loop"]["attempts"][-1])
            print(f"[repo-doctor] fix attempt {index}: applying the fix itself failed; trying again.")
            continue

        status, exit_code = install_and_import(
            sandbox, cfg, log, repo_dir, repo, skip_import, step_prefix=f"attempt{index}_"
        )
        log.add_fix_attempt(index, diagnosis=diagnosis.as_dict(), proposal=proposal.as_dict(),
                            applied=True, result_status=status)
        prior_attempts.append(log.doc["fix_loop"]["attempts"][-1])

        if status == logstore.STATUS_OK:
            log.finish_fix_loop(status, "success")
            print(f"[repo-doctor] fix loop: SUCCESS after {index} attempt(s).")
            return status, exit_code

    log.finish_fix_loop(status, "cap_reached")
    print(f"[repo-doctor] fix loop: cap of {cap} attempts reached, still failing.")
    return status, exit_code
