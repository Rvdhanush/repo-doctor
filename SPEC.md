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
      sandbox/Dockerfile      # base python image the agent operates inside        [built]
      runner.py               # clone -> detect -> install -> import; structured log [built]
      diagnose.py             # CLI: captured failure -> structured diagnosis (JSON) [built]
      repo_doctor/
        sandbox.py            # container lifecycle + isolation flags              [built]
        detect.py             # deterministic install-file / import-target detection [built]
        logstore.py           # run.json + events.jsonl + raw untruncated logs     [built]
        llm.py                # OpenAI-compatible client with provider fallback    [built]
        diagnosis.py          # prompt, category enum, JSON validation             [built]
        config.py             # settings.yaml -> dataclasses                       [built]
      fix_loop.py             # propose fix -> apply -> re-run -> retry (capped)   [inc 2]
      telemetry.py            # per-attempt: fix tried, result, time, token cost   [inc 3]
      report.py               # final per-repo report: fixed/not, cost, why-if-not [inc 4]
      configs/settings.yaml   # attempt cap, base image, providers, timeouts
      .devcontainer/          # Codespaces: docker-in-docker + Claude Code
      results/                # per-run logs + diagnoses (committed as evidence)

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
- **[NEXT] Increment 2 — the fix loop (core).** Propose fix -> apply in sandbox -> re-run -> read
  result -> retry up to the cap. Stop on install+import success or cap.
- **Increment 3 — telemetry.** Surface every attempt, time, and token/API cost as a dashboard.
- **Increment 4 — honest reporting.** Final report per repo, including a real diagnosis for repos
  it could not fix.

Ship 0-2 and you have a real agent. 3-4 are what make it stand out.

### Gate checks — each increment must clear one before the next begins
These are judged by a human, not by tests passing:
- **0:** point it at a broken repo and see the raw failure captured in a log. That's it.
- **1:** read the diagnoses against repos you already understand. Are they *right*? This is the
  calibration bar — the difference between a real diagnosis and a plausible-sounding wrong one is
  something only someone who knows these failure modes can judge. Bad diagnoses here are signal to
  refine before building the fix loop, not a reason to push on.
- **2:** watch it fix a repo you couldn't — *and* watch it fail one. The failure is data for
  increment 4, not a bug.
- **3:** the dashboard should let someone watch the agent think: try, fail, adjust, cost accruing.
- **4:** the "could not fix" report must be as informative as the success report.

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
