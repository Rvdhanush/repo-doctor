"""Turn a captured failure into a structured diagnosis. No fixing.

Increment 1 only: read the log the harness already produced, ask an LLM what
went wrong, return validated JSON. Nothing here modifies a repo or a sandbox.

Two design choices come straight from watching a naive first attempt fail. Asked
to diagnose a dlib build failure with only a stack-trace tail and a free-text
`category` field, the model answered `category: "Python Installation"` and
`why: "cmake returned non-zero exit status 1"` -- a restatement of the error, not
a diagnosis. The root cause (dlib ships no wheel for this platform, so pip built
from source, and the lean image has no cmake or compiler) needs two things the
first attempt lacked:

  1. A CONSTRAINED category enum. Free text invites a label that describes the
     symptom. A closed set forces a decision about what kind of problem it is.
  2. ENVIRONMENT CONTEXT. The model cannot infer "this image has no compiler"
     from a traceback. It has to be told what the sandbox is, what command ran,
     and what the repo asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient, LLMResult, extract_json

# Closed set. The first five are SPEC.md section 6's scenarios; the rest cover
# failure modes that show up constantly and would otherwise be forced into a
# wrong bucket.
CATEGORIES: dict[str, str] = {
    "cuda_cpu_mismatch":
        "The repo wants a CUDA/GPU build but the sandbox is CPU-only, or the "
        "torch/torchvision/CUDA versions disagree with each other.",
    "version_conflict":
        "Two or more pinned packages require mutually incompatible versions, so "
        "the resolver cannot satisfy them together.",
    "missing_system_package":
        "An OS-level dependency is absent: a shared library (.so) needed at "
        "import time, or a build tool (cmake, gcc/g++, pkg-config) needed to "
        "compile a package that has no prebuilt wheel.",
    "stale_dependency":
        "A dependency is unpinned or loosely pinned and a newer release broke "
        "the API the repo relies on.",
    "unfixable":
        "Cannot be resolved inside the sandbox: a proprietary dataset, real GPU "
        "hardware, a private index, or a dependency that no longer exists.",
    "missing_python_dependency":
        "A Python package the code imports is simply not declared or installed. "
        "The install itself may have succeeded.",
    "python_version_incompatible":
        "The pinned version exists, but not as a wheel/sdist usable on THIS "
        "interpreter or platform -- typically an old pin against a newer Python. "
        "Strong signal: pip lists 'from versions: ...' and every offered version "
        "is NEWER than the pin. Prefer this over network_or_index whenever pip "
        "was able to reach the index and enumerate versions.",
    "network_or_index":
        "A genuine connectivity or index problem: the index was unreachable, "
        "timed out, returned 4xx/5xx, or the package NAME does not exist at all. "
        "Do NOT use this merely because one pinned version was unavailable -- if "
        "pip successfully listed other versions, the index worked fine.",
    "no_install_file":
        "The repo ships no installable manifest at all (no requirements.txt, "
        "pyproject.toml, or setup.py), so no install was ever attempted. Common "
        "in research repos. Nothing failed to build -- there was nothing to build.",
    "repo_unavailable":
        "The repository itself could not be obtained: it does not exist, is "
        "private, or the URL is wrong. Nothing about the Python environment is "
        "implicated.",
    "other":
        "A genuine failure that none of the above describes. Use sparingly.",
}

CONFIDENCE = ("high", "medium", "low")

_SYSTEM = """You are a senior Python packaging and ML-environment engineer. You \
diagnose why a repository failed to install or import inside a Linux container.

You will be given the sandbox's exact configuration, the command that ran, the \
repo's dependency declarations, and the captured output.

Rules:
- Diagnose the ROOT CAUSE, never restate the error message. "cmake exited 1" is \
an observation; "the image has no C++ toolchain, and this package has no wheel \
for cp311 so pip fell back to a source build" is a diagnosis.
- Reason from the sandbox facts you are given. If a build tool or shared library \
is absent from the image, say so explicitly.
- Pick exactly one category from the provided list. Do not invent categories.
- Quote the specific log line(s) that justify your conclusion in "evidence".
- If the output does not actually support a confident conclusion, say so with \
confidence "low" rather than guessing.
- Reply with a single JSON object and nothing else.

Grounding rules -- these override everything else:
- Every claim must be supported by the material you were given. The sandbox facts \
describe what the image LACKS; they are not evidence that any particular lack \
caused this failure. Do not conclude "no compiler" unless the output actually \
shows a failed build.
- "secondary_issues" must reference something genuinely present in the dependency \
file or the output. If the dependency file contains no CUDA index, do not mention \
CUDA. Speculating about problems that might exist is worse than an empty list.
- If NO install command ran, then no dependency, compiler, or CUDA problem has \
been demonstrated. The failure is about the repo's packaging or availability. \
Saying "nothing was attempted, so nothing is known about the environment" is the \
correct and complete answer.

Domain knowledge that is often decisive:
- "Could not find a version that satisfies X==A (from versions: B, C, ...)" means \
the index WAS reachable. If every offered version is newer than A, the pin \
predates this interpreter -- that is a Python/platform compatibility problem, \
not a network one.
- An "--extra-index-url" pointing at download.pytorch.org/whl/cuXXX requests a \
CUDA build. On a CPU-only sandbox that is a CPU/GPU mismatch, and the fix is \
usually the /whl/cpu index or an unsuffixed release. Mention this whenever such \
an index appears in the dependency file, even if another problem masked it.
- A package that fails while building a wheel (setup.py/cmake/gcc errors) needs \
a build toolchain, which this image does not have."""


@dataclass
class Diagnosis:
    category: str
    what_failed: str
    why: str
    evidence: str = ""
    fix_category: str = ""
    confidence: str = "low"
    human_note: str = ""
    # Problems visible in the evidence that the primary failure is currently
    # masking. A repo often has several: fix the first and the next one appears.
    # Increment 2's fix loop needs to know they are coming.
    secondary_issues: list = field(default_factory=list)
    schema_valid: bool = True
    validation_notes: list[str] = field(default_factory=list)
    llm: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "what_failed": self.what_failed,
            "why": self.why,
            "evidence": self.evidence,
            "fix_category": self.fix_category,
            "confidence": self.confidence,
            "human_note": self.human_note,
            "secondary_issues": self.secondary_issues,
            "schema_valid": self.schema_valid,
            "validation_notes": self.validation_notes,
            "llm": self.llm,
        }


def _tail(text: str, lines: int, max_chars: int) -> str:
    """Last N lines, hard-capped by characters.

    Tail-weighted on purpose: pip prints the resolution log first and the actual
    failure last. The character cap is the real protection -- Groq's free tier
    allows 12k tokens per minute, and an unbounded excerpt can approach that on
    its own.
    """
    if not text:
        return ""
    kept = text.splitlines()[-lines:]
    out = "\n".join(kept)
    if len(out) > max_chars:
        out = "...[truncated]...\n" + out[-max_chars:]
    return out


def build_prompt(run: dict, install_file_content: str = "",
                 tail_lines: int = 120, max_chars: int = 6000) -> str:
    """Assemble the user prompt from a run.json document."""
    outcome = run.get("outcome", {})
    sandbox = run.get("sandbox", {})
    target = run.get("target", {})
    detection = run.get("install_detection", {})
    import_detection = run.get("import_detection", {})

    failing = outcome.get("failing_step")
    step = next((s for s in run.get("steps", []) if s.get("name") == failing), None)

    parts: list[str] = []

    parts.append(
        "## Sandbox facts (the environment the failure happened in)\n"
        f"- Base image: {sandbox.get('image', 'python:3.11-slim-bookworm')} "
        "(Debian bookworm slim)\n"
        f"- Python: {sandbox.get('python_version', 'Python 3.11')}, "
        f"pip: {sandbox.get('pip_version', 'unknown')}\n"
        "- Installed OS packages: git, ca-certificates, curl AND NOTHING ELSE.\n"
        "- There is NO C/C++ compiler, NO cmake, NO pkg-config, NO build-essential.\n"
        "- CPU-only. No GPU, no CUDA driver, no nvidia runtime.\n"
        "- Packages install into a venv at /opt/venv. Network access is available.\n"
        "- The repo is at /work/repo."
    )

    parts.append(
        "## What was attempted\n"
        f"- Repository: {target.get('url')} at commit "
        f"{(target.get('resolved_commit') or '')[:12]}\n"
        f"- Install strategy: {detection.get('strategy')} "
        f"(chose {detection.get('chosen') or 'nothing'}; "
        f"candidates seen: {', '.join(detection.get('candidates_found') or []) or 'none'})\n"
        f"- Failing step: {failing}\n"
        f"- Command: {' '.join(step.get('argv', [])) if step else 'n/a'}\n"
        f"- Exit code: {outcome.get('exit_code')}"
    )

    if import_detection.get("module"):
        parts.append(
            "## Import check\n"
            f"- Module tried: {import_detection.get('module')}\n"
            f"- How that name was chosen: {import_detection.get('source')} "
            f"(confidence: {import_detection.get('confidence')})\n"
            f"- Note: {import_detection.get('reason')}\n"
            "- If confidence is low, the module name is a GUESS. A "
            "ModuleNotFoundError on a guessed name may indicate a bad guess "
            "rather than a broken repo. Say so if that is what you see."
        )

    if install_file_content:
        parts.append(
            f"## {detection.get('chosen', 'dependency file')} (as declared by the repo)\n"
            "```\n" + install_file_content.strip()[:2500] + "\n```"
        )

    if step:
        stderr = _tail(step.get("stderr", ""), tail_lines, max_chars)
        stdout = _tail(step.get("stdout", ""), 40, 1500)
        if stderr:
            parts.append("## Captured stderr (tail)\n```\n" + stderr + "\n```")
        if stdout:
            parts.append("## Captured stdout (tail)\n```\n" + stdout + "\n```")
    else:
        parts.append(
            "## No command output — read this carefully\n"
            f"The run ended at '{failing}' with: {outcome.get('summary_line')}\n\n"
            "NO INSTALL WAS EVER ATTEMPTED. There is no build log, no pip output, "
            "and no import error, because the run stopped before that point. "
            "Therefore nothing is known about compilers, CUDA, wheels, or "
            "dependency versions for this repo, and you must not claim otherwise. "
            "Diagnose only what the summary above actually establishes, and leave "
            "secondary_issues empty."
        )

    enum_block = "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())
    parts.append(
        "## Categories (choose exactly one)\n" + enum_block
    )

    parts.append(
        "## Required JSON shape\n"
        "{\n"
        '  "category": "<one of the category names above, exactly>",\n'
        '  "what_failed": "<one sentence: the concrete thing that broke>",\n'
        '  "why": "<the root cause, reasoning from the sandbox facts>",\n'
        '  "evidence": "<the specific log line(s) that prove it>",\n'
        '  "fix_category": "<the class of fix that would address this, e.g. '
        "'install a system package', 'pin to a compatible version', "
        "'use the CPU wheel index'>\",\n"
        '  "confidence": "high | medium | low",\n'
        '  "human_note": "<only if unfixable: what a human must do; else \\"\\">",\n'
        '  "secondary_issues": ["<other real problems visible in the evidence that '
        "this failure is currently masking -- e.g. a CUDA wheel index that will "
        'bite once the version pin is fixed. Empty list if none.>"]\n'
        "}"
    )

    return "\n\n".join(parts)


def validate(raw: dict) -> tuple[dict, list[str]]:
    """Coerce the model's reply into the expected shape, recording any deviation.

    A malformed reply is itself a finding worth keeping, so nothing is silently
    corrected -- every fix-up is noted.
    """
    notes: list[str] = []
    out = dict(raw)

    category = str(out.get("category", "")).strip()
    if category not in CATEGORIES:
        normalised = category.lower().replace(" ", "_").replace("-", "_")
        if normalised in CATEGORIES:
            notes.append(f"category normalised from {category!r}")
            category = normalised
        else:
            notes.append(f"category {category!r} is not in the allowed set; using 'other'")
            category = "other"
    out["category"] = category

    confidence = str(out.get("confidence", "")).strip().lower()
    if confidence not in CONFIDENCE:
        notes.append(f"confidence {out.get('confidence')!r} invalid; using 'low'")
        confidence = "low"
    out["confidence"] = confidence

    for key in ("what_failed", "why", "evidence", "fix_category", "human_note"):
        value = out.get(key, "")
        if not isinstance(value, str):
            notes.append(f"{key} was {type(value).__name__}, coerced to string")
            value = json_safe(value)
        out[key] = value.strip()

    issues = out.get("secondary_issues", [])
    if isinstance(issues, str):
        issues = [issues] if issues.strip() else []
        notes.append("secondary_issues was a string, wrapped in a list")
    elif not isinstance(issues, list):
        notes.append(f"secondary_issues was {type(issues).__name__}, dropped")
        issues = []
    out["secondary_issues"] = [str(i).strip() for i in issues if str(i).strip()]

    if not out["what_failed"] or not out["why"]:
        notes.append("model omitted what_failed or why")

    return out, notes


def json_safe(value: Any) -> str:
    import json as _json
    try:
        return _json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def diagnose_run(run: dict, client: LLMClient, install_file_content: str = "",
                 tail_lines: int = 120, max_chars: int = 6000) -> Diagnosis:
    """Diagnose one captured run. Read-only with respect to the repo."""
    prompt = build_prompt(run, install_file_content, tail_lines, max_chars)
    result: LLMResult = client.complete_json(_SYSTEM, prompt)

    try:
        raw = extract_json(result.content)
    except ValueError as exc:
        return Diagnosis(
            category="other",
            what_failed="The model did not return usable JSON.",
            why=str(exc),
            confidence="low",
            schema_valid=False,
            validation_notes=["reply was not parseable as JSON"],
            llm=result.as_dict(),
        )

    cleaned, notes = validate(raw)
    return Diagnosis(
        category=cleaned["category"],
        what_failed=cleaned["what_failed"],
        why=cleaned["why"],
        evidence=cleaned["evidence"],
        fix_category=cleaned["fix_category"],
        confidence=cleaned["confidence"],
        human_note=cleaned["human_note"],
        secondary_issues=cleaned["secondary_issues"],
        schema_valid=not notes,
        validation_notes=notes,
        llm=result.as_dict(),
    )
