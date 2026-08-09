"""Turn a diagnosis into an actionable fix. Increment 2, first half.

`diagnosis.py` explains what went wrong; this module asks the LLM a narrower,
second question: given that diagnosis, what is the ONE concrete, closed-form
action to take next? The action space is deliberately small -- three choices,
never free text -- for the same reason `diagnosis.py`'s category field is a
closed enum (see its module docstring): a model asked for an open-ended "fix
command" will happily propose a shell pipeline, a package that doesn't exist,
or a rewrite of a file it was never shown. Constraining the shape constrains
the failure modes.

The three actions cover SPEC.md section 6's fixable scenarios completely:
  - apt_install            -- Scenario C (missing system package / build tool).
  - edit_dependency_file   -- Scenarios A, B, D (CPU/CUDA index, version pin,
                               stale/unpinned dependency). The model rewrites
                               the WHOLE chosen dependency file rather than
                               producing a diff/patch: small text files, and a
                               full rewrite is something a model reproduces
                               reliably where a unified diff is not.
  - give_up                -- Scenario E. Stopping is a valid, expected output,
                               not a failure of this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .diagnosis import Diagnosis, sandbox_facts_block
from .llm import LLMClient, LLMResult, extract_json

ACTIONS = ("apt_install", "edit_dependency_file", "give_up")

# Debian package names: lowercase alphanumerics plus +.-, never starting with
# a symbol. A proposed package that fails this is dropped, not executed --
# apt-get is never given a name we haven't shape-checked ourselves.
_VALID_APT_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")

_SYSTEM = """You are a senior Python packaging and ML-environment engineer. You \
have already diagnosed why a repository failed to install or import inside a \
Linux container, and now must choose exactly ONE concrete action to try next.

You may choose exactly one of three actions:
- "apt_install": the sandbox is missing an OS-level package (a build toolchain, \
a shared library). List the Debian (bookworm) package names to install.
- "edit_dependency_file": the repo's dependency file needs a different pin, a \
different (or additional) package index, or an added version constraint. \
Provide the COMPLETE new content of the file -- not a diff, not a snippet. \
Preserve everything in the original file that is not part of the fix.
- "give_up": the failure cannot be resolved inside this sandbox (proprietary \
data, real GPU hardware, a dependency that no longer exists, or you have \
already tried the available fixes in prior attempts without progress).

Rules:
- Reason from the diagnosis and sandbox facts you are given. Do not invent a \
cause the diagnosis did not establish.
- You may only edit the ONE dependency file named in the prompt. Do not \
propose editing any other path.
- Look at prior attempts in this run before proposing anything: if an action \
was already tried and did not fix the problem, do not propose it again -- \
either try something different or give up.
- apt_install package names must be real Debian bookworm package names you are \
confident exist (e.g. "build-essential", "cmake", "libgl1", "libglib2.0-0", \
"pkg-config", "g++"). Do not guess an unfamiliar name.
- If action is "edit_dependency_file", file_content is REQUIRED and must never \
be empty -- an edit_dependency_file reply with no file_content is treated as a \
malformed reply and discarded, wasting the attempt. Write out the ENTIRE file \
exactly as it should look afterward, including every unchanged line, even when \
the fix is as small as removing one version pin. Do not describe the edit in \
prose instead of making it, and do not omit file_content because the change \
feels small.
- Reply with a single JSON object and nothing else.

Worked example -- same shape of problem, a smaller file:

Given the file is requirements.txt with content:
    flask==2.0.1
    werkzeug==3.0.0
    requests==2.31.0
and the diagnosis says werkzeug==3.0.0 conflicts with flask==2.0.1 (flask 2.0.1
requires werkzeug<2.1), the correct reply unpins ONLY the conflicting package
and reproduces every other line unchanged:
{
  "action": "edit_dependency_file",
  "packages": [],
  "file_path": "requirements.txt",
  "file_content": "flask==2.0.1\\nwerkzeug\\nrequests==2.31.0",
  "reason": "Unpinned werkzeug so pip can select a version flask 2.0.1 accepts.",
  "give_up_note": ""
}
Note file_content is the FULL file as one string with embedded newlines, not a
description of the change -- reproduce this shape exactly, adapted to the real
file you were given."""


@dataclass
class FixProposal:
    action: str
    packages: list[str] = field(default_factory=list)     # apt_install
    file_path: str = ""                                     # edit_dependency_file
    file_content: str = ""                                  # edit_dependency_file
    reason: str = ""
    give_up_note: str = ""                                   # give_up
    schema_valid: bool = True
    validation_notes: list[str] = field(default_factory=list)
    llm: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "packages": self.packages,
            "file_path": self.file_path,
            "file_content": self.file_content,
            "reason": self.reason,
            "give_up_note": self.give_up_note,
            "schema_valid": self.schema_valid,
            "validation_notes": self.validation_notes,
            "llm": self.llm,
        }


def _format_prior_attempts(prior_attempts: list[dict]) -> str:
    if not prior_attempts:
        return "## Prior attempts this run\nNone -- this is the first attempt."
    lines = []
    for a in prior_attempts:
        proposal = a.get("proposal") or {}
        action = proposal.get("action", "?")
        detail = (
            f"packages={proposal.get('packages')}" if action == "apt_install"
            else f"file={proposal.get('file_path')!r}" if action == "edit_dependency_file"
            else proposal.get("give_up_note", "")
        )
        lines.append(
            f"- attempt {a.get('index')}: action={action} ({detail}) "
            f"-> result: {a.get('result_status')}"
        )
    return "## Prior attempts this run (do not blindly repeat these)\n" + "\n".join(lines)


def build_fix_prompt(run: dict, diagnosis: Diagnosis, dependency_file_path: str,
                     dependency_file_content: str, prior_attempts: list[dict]) -> str:
    """Assemble the fix-proposal prompt from the run, its diagnosis, and history."""
    parts: list[str] = [sandbox_facts_block(run)]

    parts.append(
        "## Diagnosis already established\n"
        f"- category: {diagnosis.category}\n"
        f"- what failed: {diagnosis.what_failed}\n"
        f"- why: {diagnosis.why}\n"
        f"- evidence: {diagnosis.evidence}\n"
        f"- suggested fix class: {diagnosis.fix_category}\n"
        f"- confidence: {diagnosis.confidence}"
    )

    if dependency_file_path:
        parts.append(
            f"## The ONE file you may edit: {dependency_file_path}\n"
            "```\n" + (dependency_file_content.strip()[:3000] or "(empty)") + "\n```"
        )
    else:
        parts.append(
            "## No editable dependency file\n"
            "No dependency file was identified for this repo, so "
            "\"edit_dependency_file\" is not a valid choice this attempt."
        )

    parts.append(_format_prior_attempts(prior_attempts))

    parts.append(
        "## Required JSON shape\n"
        "{\n"
        '  "action": "apt_install | edit_dependency_file | give_up",\n'
        '  "packages": ["<debian package name>", "..."],  // apt_install only, else []\n'
        '  "file_path": "<must exactly match the file path given above>",  '
        "// edit_dependency_file only, else \"\"\n"
        '  "file_content": "<the COMPLETE new file content>",  '
        "// edit_dependency_file only, else \"\"\n"
        '  "reason": "<one sentence: why this action fixes the diagnosed problem>",\n'
        '  "give_up_note": "<only if action is give_up: what a human must do; else \\"\\">"\n'
        "}"
    )

    return "\n\n".join(parts)


def validate(raw: dict, allowed_file_path: str) -> tuple[dict, list[str]]:
    """Coerce the model's reply into the closed action schema.

    Every correction is recorded, never silently applied -- a proposal that
    needed fixing up is itself informative, same as `diagnosis.validate`.
    """
    notes: list[str] = []
    out = dict(raw)

    action = str(out.get("action", "")).strip()
    if action not in ACTIONS:
        notes.append(f"action {action!r} is not in {ACTIONS}; forcing give_up")
        action = "give_up"
    out["action"] = action

    packages = out.get("packages", [])
    if not isinstance(packages, list):
        notes.append(f"packages was {type(packages).__name__}, dropped")
        packages = []
    cleaned_packages = []
    for p in packages:
        name = str(p).strip()
        if _VALID_APT_PACKAGE.match(name):
            cleaned_packages.append(name)
        else:
            notes.append(f"package {name!r} failed the name check; dropped")
    out["packages"] = cleaned_packages

    for key in ("file_path", "file_content", "reason", "give_up_note"):
        value = out.get(key, "")
        if not isinstance(value, str):
            notes.append(f"{key} was {type(value).__name__}, coerced to string")
            value = str(value)
        out[key] = value.strip()

    if action == "apt_install" and not out["packages"]:
        notes.append("apt_install proposed with no valid packages; forcing give_up")
        out["action"] = action = "give_up"
        out["give_up_note"] = out["give_up_note"] or "No valid system package was proposed."

    if action == "edit_dependency_file":
        # The single hard boundary: an LLM-controlled write path is never
        # trusted to name its own target. It may only touch the file it was
        # shown, never anything it invents (e.g. a path outside the repo).
        if not allowed_file_path or out["file_path"] != allowed_file_path:
            notes.append(
                f"edit_dependency_file targeted {out['file_path']!r}, "
                f"not the allowed {allowed_file_path!r}; forcing give_up"
            )
            out["action"] = action = "give_up"
            out["give_up_note"] = out["give_up_note"] or "Proposed file path was not the allowed one."
        elif not out["file_content"]:
            notes.append("edit_dependency_file proposed with empty file_content; forcing give_up")
            out["action"] = action = "give_up"
            out["give_up_note"] = out["give_up_note"] or "No file content was proposed."

    return out, notes


def propose_fix(run: dict, diagnosis: Diagnosis, client: LLMClient,
                dependency_file_path: str, dependency_file_content: str,
                prior_attempts: list[dict]) -> FixProposal:
    """Ask the LLM for the next concrete action. Never applies anything itself."""
    prompt = build_fix_prompt(run, diagnosis, dependency_file_path,
                              dependency_file_content, prior_attempts)
    result: LLMResult = client.complete_json(_SYSTEM, prompt)

    try:
        raw = extract_json(result.content)
    except ValueError as exc:
        return FixProposal(
            action="give_up",
            give_up_note=f"The model did not return usable JSON: {exc}",
            schema_valid=False,
            validation_notes=["reply was not parseable as JSON"],
            llm=result.as_dict(),
        )

    cleaned, notes = validate(raw, dependency_file_path)
    return FixProposal(
        action=cleaned["action"],
        packages=cleaned["packages"],
        file_path=cleaned["file_path"],
        file_content=cleaned["file_content"],
        reason=cleaned["reason"],
        give_up_note=cleaned["give_up_note"],
        schema_valid=not notes,
        validation_notes=notes,
        llm=result.as_dict(),
    )
