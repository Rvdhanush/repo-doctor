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
      runner.py               # clone a repo into the sandbox, attempt install, capture stdout/stderr/exit
      diagnose.py             # pass captured failure to an LLM -> structured diagnosis (JSON)
      fix_loop.py             # propose fix -> apply in sandbox -> re-run -> read result -> retry (capped)
      telemetry.py            # per-attempt: fix tried, result, time, token/API cost -> dashboard data
      report.py               # final per-repo report: fixed/not, fixes applied, cost, why-if-not
      configs/
        settings.yaml         # attempt cap, base image, model, cost limits
      results/                # per-run logs + reports (committed as evidence)

Rules:
- Everything runs in the sandbox; the host is never modified.
- **Cap fix attempts** (default 5). Never loop unbounded — track token cost per run and stop at a
  configurable ceiling.
- Config-driven and reproducible.
- Stack: Python 3.11+, Docker, an LLM via API (start with a free/cheap tier), structured JSON logs.

## 5. Build order — one shippable increment at a time
- **Increment 0 — harness, NO LLM.** Clone a repo into the sandbox, attempt install, capture the
  raw failure output to a structured log. Prove isolation + capture work.
- **Increment 1 — diagnosis only.** Feed the captured error to an LLM; return a structured
  diagnosis (what failed, why, fix category). No fixing yet.
- **Increment 2 — the fix loop (core).** Propose fix -> apply in sandbox -> re-run -> read result
  -> retry up to the cap. Stop on install+import success or cap.
- **Increment 3 — telemetry.** Surface every attempt, time, and token/API cost as a dashboard.
- **Increment 4 — honest reporting.** Final report per repo, including a real diagnosis for repos
  it could not fix.

Ship 0-2 and you have a real agent. 3-4 are what make it stand out.

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
