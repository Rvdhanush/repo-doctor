# SPEC — repo-doctor

## 1. One-line summary
An agent that takes a broken Python ML/data repository and gets it to **install and import
successfully inside a Docker sandbox** — diagnosing dependency, version, and environment
errors, applying fixes, verifying them, and reporting honestly (including on repos it cannot fix).

## 2. Why this exists
Cloning an ML repo and getting it to actually run is a routine, painful, time-consuming task —
dependency hell, version conflicts, CPU/CUDA build mismatches, missing system packages. It is
exactly the kind of unglamorous reliability work that (a) an LLM in a chat window structurally
cannot do (it can't clone, install, hit the real error, and iterate), and (b) maps directly onto
Forward-Deployed-Engineer and AI-Engineer work: making messy real-world systems run reliably in
constrained environments, with cost and failure handled gracefully.

The project deliberately owns ONE narrow domain completely, rather than trying to run "any repo."

## 3. Scope — read this before writing anything
- **Success = the repo installs AND a basic import succeeds** inside the sandbox. That is the
  entire definition of done for v1.
- **NOT in scope for v1:** running the model/training, executing the README's example command,
  passing the repo's test suite, or a web product. Those are explicitly later phases.
- **Domain: Python ML/data repos only.** Not JavaScript, not arbitrary repositories.
- Starting at the tightest useful scope (install + import) is what makes this shippable in a few
  weekends instead of an abandoned sprawl. Extend only after v1 ships.

## 4. Architecture
The one load-bearing decision: **all target-repo code runs inside a Docker sandbox, never on the
host.** The agent clones into the container, runs shell commands against it, and reads results
back. This is what makes "the agent ran pip install and it broke something" safe — and it's the
production-isolation instinct FDE roles screen for.

    repo-doctor/
      sandbox/
        Dockerfile            # base python image the agent operates inside
      runner.py               # CLI: clone -> install/import (-> diagnose) (-> fix loop) (-> report)
      diagnose.py             # CLI: diagnose a captured run — no re-install
      telemetry.py            # CLI: aggregate every run's attempts/time/token cost into a table
      report.py               # CLI: the final honest per-repo verdict
      repo_doctor/            # logic, thin CLIs above import from here
        sandbox.py              container lifecycle, isolation, write_file/apt_install
        detect.py                deterministic install-file and import-target detection
        pipeline.py              install -> import, shared by the initial run and each fix attempt
        diagnosis.py             LLM: captured failure -> structured diagnosis (closed category enum)
        fix.py                   LLM: diagnosis -> one concrete action (closed action enum)
        fix_loop.py              orchestrates diagnose -> propose -> apply -> re-run, capped
        telemetry.py             read-only aggregation over run.json — no Docker, no LLM
        report.py                builds the verdict from run.json + telemetry — no Docker, no LLM
        llm.py                   minimal OpenAI-compatible client, provider fallback
        config.py                settings.yaml + env loading
        logstore.py              structured run logs (run.json / events.jsonl / logs/)
      configs/
        settings.yaml         # attempt cap, base image, model, cost limits
      .devcontainer/          # Codespaces: docker-in-docker + Claude Code preinstalled
      .github/workflows/      # CI: import/lint check + a real no-LLM harness smoke test
      results/                # per-run logs + reports (committed as evidence)

(This is the as-built layout — thin CLI entry points at the root, all logic under `repo_doctor/`.
The original sketch above listed `fix_loop.py`/`telemetry.py`/`report.py` as flat root files; the
`repo_doctor/` package split emerged in increment 1 to keep each CLI script small and its logic
independently importable/testable, and every later increment followed the same pattern.)

Rules:
- Everything runs in the sandbox; the host is never modified.
- **Cap fix attempts** (default 5). Never loop unbounded — track token cost per run and stop at a
  configurable ceiling.
- Config-driven and reproducible.
- Stack: Python 3.11+, Docker, an LLM via API (start with a free/cheap tier), structured JSON logs.
- Host dependencies stay minimal (PyYAML only). A project about fragile installs should
  not itself be fragile to install, so the LLM client is plain urllib rather than an SDK.
- Providers are any OpenAI-compatible endpoint, tried in order with fallback. Currently
  Groq (`llama-3.3-70b-versatile`) primary, Cerebras configured as backup. Keys live in
  `.env` locally and as Codespaces secrets in the cloud — never in the repo.

## 5. Build order — one shippable increment at a time
- **[DONE] Increment 0 — harness, NO LLM.** Clone a repo into the sandbox, attempt install, capture
  the raw failure output to a structured log. Prove isolation + capture work.
- **[DONE] Increment 1 — diagnosis only.** Feed the captured error to an LLM; return a structured
  diagnosis (what failed, why, fix category). No fixing yet.
- **[DONE] Increment 2 — the fix loop (core).** Propose fix -> apply in sandbox -> re-run -> read
  result -> retry up to the cap. Stop on install+import success or cap.
- **[DONE] Increment 3 — telemetry.** Surface every attempt, time, and token/API cost as a dashboard.
- **[DONE] Increment 4 — honest reporting.** Final report per repo, including a real diagnosis for
  repos it could not fix.

Ship 0-2 and you have a real agent. 3-4 are what make it stand out.

**Status: all 5 increments shipped and their gates cleared.** See README.md's roadmap for the
checklist and CLAUDE.md's "Increment status" for what file implements which; `results/` holds
every run committed as evidence, including live `--fix` runs against real repos for section 6's
scenarios below.

### Gate checks — each increment had to clear one before the next began
These were judged by a human, not by tests passing:
- **0:** point it at a broken repo and see the raw failure captured in a log. That's it.
- **1:** read the diagnoses against repos you already understand. Are they *right*? This is the
  calibration bar — the difference between a real diagnosis and a plausible-sounding wrong one is
  something only someone who knows these failure modes can judge. Bad diagnoses here are signal to
  refine before building the fix loop, not a reason to push on.
- **2:** watch it fix a repo you couldn't — *and* watch it fail one. The failure is data for
  increment 4, not a bug. Cleared live against real repos: `results/e2e-deoldify-fix` (CUDA/pin
  fix, then an honest give-up on a real second problem), `results/e2e-mimo-version-conflict`
  (version conflict fixed outright, then the same honest-give-up pattern on an unrelated issue),
  and `results/e2e-facerecognition-system-package` — the full "fix a repo you couldn't" case:
  `FIXED` after 3 attempts (apt_install for dlib's toolchain, then edit_dependency_file for the
  package itself), no give-up needed.
- **3:** the dashboard should let someone watch the agent think: try, fail, adjust, cost accruing.
  `telemetry.py` does this as a CLI table, deliberately not a web product (out of scope for v1).
- **4:** the "could not fix" report must be as informative as the success report. `report.py`
  renders exactly that from whatever `--diagnose`/`--fix` already captured, never calling an LLM
  of its own to fill a gap.

### Where increment 1 actually landed
Its first attempt scored 2 of 4 on real repos, and both failures were prompt-design faults rather
than model limitations. Recording them because they are easy to reintroduce:
- A **free-text category** produced `"Python Installation"` — the symptom restated. Categories must
  stay a closed enum.
- The prompt must carry **sandbox facts** (no compiler, CPU-only); no model infers them from a
  traceback.
- But facts about what the image *lacks* are not evidence of cause. Without explicit grounding
  rules the model invented a compiler failure for a repo where no install ever ran, and a CUDA
  index for a repo that has none. Categories `no_install_file` and `repo_unavailable` exist so
  non-install failures have somewhere correct to go.
- Diagnoses run against **stored** logs, so prompt iteration costs seconds instead of minutes of
  re-installing. This is what made three rounds of calibration affordable.

The fix loop (increment 2) had its own version of this: the fix-proposal prompt reliably produced
`edit_dependency_file` with an *empty* `file_content` whenever a fix touched more than one pin at
once — reproduced twice in a row against a real conflict before a worked few-shot example in the
prompt fixed it. Same lesson as increment 1's, one layer over: a closed schema stops the model from
answering the wrong question, but it does not by itself make the model fill every required field.

## 6. Worked example scenarios (the failure modes to design against)
These are representative of what the agent must handle. Use them as test cases.

**Scenario A — CPU/CUDA build mismatch.**
Repo pins a CUDA build of torch but the sandbox is CPU-only; import fails on a CUDA operator.
Expected agent behaviour: diagnose as "CPU/GPU build mismatch," fix by installing the CPU build
(or the matching pair), re-run, confirm import succeeds. (This is the exact class of bug Dhanush
hit on Kavach with torch/torchvision — a real, common failure mode.)

**Scenario B — version conflict.**
requirements.txt pins two packages whose versions are mutually incompatible (a library and a
transitive dependency that disagree). Expected: diagnose the conflict, resolve by adjusting the
offending pin, re-run.

**Scenario C — missing system package.**
pip install succeeds but import fails because a system library (an OS-level shared object) is
missing. Expected: diagnose as a missing system dependency, install it in the sandbox, re-run.

**Scenario D — unpinned / stale dependency.**
requirements.txt lists a package with no version, and the latest release broke the API the repo
uses. Expected: diagnose, pin to a compatible version, re-run.

**Scenario E — genuinely unfixable (the honesty case).**
Repo requires a proprietary dataset, a specific GPU, or a dependency that no longer exists.
Expected: after the attempt cap, produce an honest report — what was tried, what it hit, and what
a human would need to do. This scenario is NOT a failure of the agent; handling it well is a core
feature.

## 7. Honesty requirement
The agent must never fake success. A "could not fix" report must be as informative as a success:
the fixes attempted, the specific blocker, and the human next step. An agent that admits "stuck on
this CUDA conflict, here's what I tried" is more credible than one claiming 100% success — anyone
who has done this work knows 100% is a lie.

## 8. Standout rationale (why this project, for FDE / AI-Engineer)
- Lives in the most durable agent lane: reliability / "cleaning up the mess," which gains value as
  models proliferate.
- The demo *is* the job: a broken repo becomes a running one, with every diagnosis and cost visible.
- Passes the "isn't it just an LLM?" test — the agent must act in a real environment.
- Built by someone who knows these failure modes cold, so the diagnoses can be judged as real —
  which is the scarce "human-in-the-loop verification" skill the market keeps naming.

## 9. What "done" looks like for the portfolio
"Built an autonomous agent that gets broken Python ML repos to install and run inside a sandbox —
diagnoses dependency/version/CUDA errors, applies and verifies fixes, and reports honestly on what
it can't fix, with a live telemetry panel showing every step and its cost."
