"""Deterministic detection of how to install a repo, and what to import afterwards.

There is no LLM here and there must not be one in this increment. This is plain
file-shape inspection. Every candidate found is recorded alongside the one chosen
and the reason — that trail is what increment 1's diagnosis step reads when it has
to explain why an install was attempted the way it was.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

from .sandbox import Sandbox

# Directories that look like packages but are not the thing a user imports.
_NON_PACKAGE_DIRS = {
    "test", "tests", "testing", "docs", "doc", "examples", "example",
    "scripts", "script", "benchmarks", "notebooks", "tools", "build", "dist",
    # ML-repo furniture: data and asset directories, not code.
    "assets", "configs", "config", "data", "checkpoints", "weights",
    "images", "outputs", "output", "logs", "results",
}


@dataclass
class InstallPlan:
    """How (and whether) we can install this repo."""

    supported: bool
    strategy: str                      # e.g. "requirements.txt", "pyproject", "none"
    argv: list[str] = field(default_factory=list)
    chosen: str = ""                   # the file we act on, relative to the repo root
    reason: str = ""
    candidates_found: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "supported": self.supported,
            "strategy": self.strategy,
            "chosen": self.chosen,
            "argv": self.argv,
            "reason": self.reason,
            "candidates_found": self.candidates_found,
        }


_PIP_BASE = ["pip", "install", "--no-input", "--no-color", "--disable-pip-version-check"]
# Note: no -q. The full output IS the product of this increment.


def detect_install(sandbox: Sandbox, repo_dir: str) -> InstallPlan:
    """Choose an install strategy by inspecting the cloned tree. First match wins."""
    entries = sandbox.list_dir(repo_dir)
    entry_set = set(entries)
    candidates: list[str] = []

    # Collect everything that looks installable, for the record, before choosing.
    for name in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
                 "environment.yml", "environment.yaml", "Pipfile", "poetry.lock"):
        if name in entry_set:
            candidates.append(name)

    # Secondary requirements files: requirements-dev.txt, requirements_gpu.txt, ...
    secondary = sorted(
        n for n in entries
        if n != "requirements.txt" and re.fullmatch(r"requirements[-_].+\.txt", n)
    )
    candidates.extend(secondary)

    # A requirements/ directory is a common layout (requirements/base.txt).
    if "requirements" in entry_set:
        for n in sorted(sandbox.list_dir(f"{repo_dir}/requirements")):
            if n.endswith(".txt"):
                candidates.append(f"requirements/{n}")

    # --- choose, in priority order ---

    if "requirements.txt" in entry_set:
        return InstallPlan(
            supported=True,
            strategy="requirements.txt",
            argv=_PIP_BASE + ["-r", "requirements.txt"],
            chosen="requirements.txt",
            reason="Found requirements.txt at the repo root.",
            candidates_found=candidates,
        )

    if "pyproject.toml" in entry_set:
        content = sandbox.read_file(f"{repo_dir}/pyproject.toml") or ""
        is_poetry = "[tool.poetry]" in content
        return InstallPlan(
            supported=True,
            strategy="pyproject",
            argv=_PIP_BASE + ["."],
            chosen="pyproject.toml",
            reason=(
                "Found pyproject.toml; installing the project itself with `pip install .`."
                + (" It declares [tool.poetry]; pip can still build it via PEP 517."
                   if is_poetry else "")
            ),
            candidates_found=candidates,
        )

    if "setup.py" in entry_set or "setup.cfg" in entry_set:
        chosen = "setup.py" if "setup.py" in entry_set else "setup.cfg"
        return InstallPlan(
            supported=True,
            strategy="setuptools",
            argv=_PIP_BASE + ["."],
            chosen=chosen,
            reason=f"Found {chosen}; installing the project itself with `pip install .`.",
            candidates_found=candidates,
        )

    # No root-level install file. Fall back to a secondary requirements file if
    # there is exactly one obvious choice.
    req_like = [c for c in candidates if c.endswith(".txt")]
    if req_like:
        chosen = req_like[0]
        return InstallPlan(
            supported=True,
            strategy="requirements-secondary",
            argv=_PIP_BASE + ["-r", chosen],
            chosen=chosen,
            reason=(
                f"No root requirements.txt/pyproject.toml/setup.py; falling back to {chosen} "
                f"(candidates: {', '.join(req_like)})."
            ),
            candidates_found=candidates,
        )

    # Conda-only repos are out of scope for this increment. Say so honestly
    # instead of crashing or pretending to try — SPEC.md section 7 applies to the
    # harness itself, not just to the agent's reports.
    if {"environment.yml", "environment.yaml"} & entry_set:
        return InstallPlan(
            supported=False,
            strategy="conda",
            chosen=sorted({"environment.yml", "environment.yaml"} & entry_set)[0],
            reason=(
                "This repo installs via a conda environment file. The sandbox is pip-only, "
                "so no install was attempted. Conda support is not part of increment 0."
            ),
            candidates_found=candidates,
        )

    if "Pipfile" in entry_set:
        return InstallPlan(
            supported=False,
            strategy="pipenv",
            chosen="Pipfile",
            reason="This repo installs via Pipenv, which the sandbox does not provide.",
            candidates_found=candidates,
        )

    return InstallPlan(
        supported=False,
        strategy="none",
        reason=(
            "No install file found at the repo root (looked for requirements.txt, "
            "pyproject.toml, setup.py, setup.cfg, environment.yml, Pipfile)."
        ),
        candidates_found=candidates,
    )


@dataclass
class ImportTarget:
    module: str | None
    reason: str
    # high   = the installed distribution told us (authoritative)
    # medium = a real package directory in the source tree
    # low    = guessed from a name; a ModuleNotFoundError here means little
    confidence: str = "low"
    candidates: list[str] = field(default_factory=list)
    installed_top_level: list[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
            "candidates": self.candidates,
            "installed_top_level": self.installed_top_level,
        }


# Asks the installed distribution what it actually provides. pip writes
# direct_url.json for anything installed from a local path, so we can find the
# distribution built from the repo without needing to know its name first.
_TOP_LEVEL_PROBE = r"""
import importlib.metadata as md, json, posixpath
repo = "%s"
found = {"dist": None, "top_level": []}
for dist in md.distributions():
    try:
        direct = dist.read_text("direct_url.json")
    except Exception:
        direct = None
    if not direct or repo not in direct:
        continue
    found["dist"] = dist.metadata["Name"]
    names = []
    top = None
    try:
        top = dist.read_text("top_level.txt")
    except Exception:
        top = None
    if top:
        names = [ln.strip() for ln in top.splitlines() if ln.strip()]
    else:
        # No top_level.txt (common for modern wheels): derive from RECORD.
        for f in (dist.files or []):
            parts = str(f).split(posixpath.sep if posixpath.sep in str(f) else "/")
            head = parts[0]
            if head.endswith(".dist-info") or head.endswith(".data"):
                continue
            if head.endswith(".py"):
                head = head[:-3]
            if head and head not in names:
                names.append(head)
    found["top_level"] = names
    break
print(json.dumps(found))
"""


def _installed_top_level(sandbox: Sandbox, repo_dir: str, timeout: int) -> tuple[str | None, list[str]]:
    """Ask the environment what the freshly installed project distribution provides."""
    result = sandbox.exec(["python", "-c", _TOP_LEVEL_PROBE % repo_dir], cwd="/", timeout=timeout)
    if result.exit_code != 0:
        return None, []
    try:
        import json as _json
        data = _json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, []
    return data.get("dist"), list(data.get("top_level") or [])


def _dirs_with_python_files(sandbox: Sandbox, repo_dir: str, entries: list[str]) -> list[str]:
    """Top-level directories that hold .py files but have no __init__.py.

    Plenty of research repos are laid out this way: the code is importable only
    because you run from the repo root, never because it was installed.
    """
    scored: list[tuple[int, str]] = []
    for name in sorted(entries):
        if name.startswith(".") or name.lower() in _NON_PACKAGE_DIRS or not name.isidentifier():
            continue
        listing = sandbox.list_dir(f"{repo_dir}/{name}")
        if not listing:
            continue
        count = sum(1 for n in listing if n.endswith(".py"))
        if count:
            scored.append((count, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored]


def detect_import_target(sandbox: Sandbox, repo_dir: str, repo_name: str,
                         installed_project: bool = False,
                         timeout: int = 60) -> ImportTarget:
    """Work out which module a user would import after installing this repo.

    Ordered by how much the answer can be trusted. The confidence field matters
    downstream: a ModuleNotFoundError on a *guessed* name says nothing about the
    repo, while one on a name the installed distribution itself declared is a
    genuine finding.
    """
    entries = sandbox.list_dir(repo_dir)

    # 1. Authoritative: the installed distribution's own top-level names. Only
    #    meaningful when we installed the project itself (not a requirements file).
    dist_name = None
    installed: list[str] = []
    if installed_project:
        dist_name, installed = _installed_top_level(sandbox, repo_dir, timeout)
        usable = [n for n in installed if n.isidentifier() and n.lower() not in _NON_PACKAGE_DIRS]
        if usable:
            return ImportTarget(
                module=usable[0],
                confidence="high",
                source="installed-distribution",
                reason=(
                    f"The installed distribution '{dist_name}' declares top-level module(s): "
                    f"{', '.join(installed)}."
                ),
                candidates=usable,
                installed_top_level=installed,
            )

    # 2. A real package directory in the source tree.
    pkg_dirs: list[str] = []
    for name in sorted(entries):
        if name.startswith(".") or name.lower() in _NON_PACKAGE_DIRS or not name.isidentifier():
            continue
        if sandbox.path_exists(f"{repo_dir}/{name}/__init__.py"):
            pkg_dirs.append(name)
    if "src" in entries:
        for name in sorted(sandbox.list_dir(f"{repo_dir}/src")):
            if name.isidentifier() and sandbox.path_exists(f"{repo_dir}/src/{name}/__init__.py"):
                pkg_dirs.append(name)

    if pkg_dirs:
        note = ""
        if installed_project and dist_name and not installed:
            note = (f" Note: the installed distribution '{dist_name}' declares no top-level "
                    f"modules, so this package was probably not packaged by the install.")
        return ImportTarget(
            module=pkg_dirs[0],
            confidence="medium",
            source="source-tree-package",
            reason=(f"Top-level package directory with __init__.py "
                    f"(candidates: {', '.join(pkg_dirs)})." + note),
            candidates=pkg_dirs,
            installed_top_level=installed,
        )

    # 3. A source directory with .py files but no __init__.py.
    loose = _dirs_with_python_files(sandbox, repo_dir, entries)
    if loose:
        note = ""
        if installed_project and dist_name and not installed:
            note = (f" The installed distribution '{dist_name}' declares no top-level modules, "
                    f"so nothing importable was installed even though pip reported success.")
        return ImportTarget(
            module=loose[0],
            confidence="medium",
            source="source-tree-loose",
            reason=(
                f"'{loose[0]}' holds .py files but has no __init__.py, so it is importable only "
                f"from the repository root (candidates: {', '.join(loose)})." + note
            ),
            candidates=loose,
            installed_top_level=installed,
        )

    # 4. The project name declared in pyproject.toml.
    content = sandbox.read_file(f"{repo_dir}/pyproject.toml")
    if content:
        try:
            data = tomllib.loads(content)
            name = (data.get("project", {}).get("name")
                    or data.get("tool", {}).get("poetry", {}).get("name"))
            if name:
                module = name.replace("-", "_")
                return ImportTarget(
                    module=module,
                    confidence="low",
                    source="pyproject-name",
                    reason=f"Derived from the project name '{name}' in pyproject.toml.",
                    candidates=[module],
                    installed_top_level=installed,
                )
        except tomllib.TOMLDecodeError:
            pass

    # 5. Last resort: the repository name. Explicitly low confidence.
    module = repo_name.replace("-", "_")
    if module.isidentifier():
        return ImportTarget(
            module=module,
            confidence="low",
            source="repo-name",
            reason=(
                f"No package directory and no declared project name; guessed from the repository "
                f"name '{repo_name}'. An ImportError on this name may say more about the guess "
                f"than about the repo."
            ),
            candidates=[module],
            installed_top_level=installed,
        )

    return ImportTarget(
        module=None,
        confidence="low",
        source="none",
        reason=(
            "Could not determine a module to import: no package directory, no project name in "
            "pyproject.toml, and the repository name is not a valid identifier."
        ),
        installed_top_level=installed,
    )
