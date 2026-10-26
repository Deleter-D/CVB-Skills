---
name: qa-regression
description: Gated three-tier regression of a code change — impacted tests, then the full unit suite, then end-to-end — stopping at the first red tier and reporting its failure points in a fixed format. Use when the user wants a change regressed or QA'd (回归 / 回归测试 / QA), e.g. before merge or hand-off.
---

# QA Regression

Regress the current change through three **tiers**, each one a **gate**:

1. **Impacted** — the tests covering what the change touched.
2. **Full unit** — the whole hermetic test suite.
3. **End-to-end** — the suite that exercises the running system.

A tier goes **red** on any failure, and a red tier is a closed gate: stop, report, and run no later tier — not even "to see how bad it is".

You are a **witness**, not a detective. A witness testifies to what was observed — which command ran, what failed, the error verbatim — and nothing else: no cause, no "probably", no fix, no advice, no judgement of the failure (_flaky_, _minor_, _unrelated_, _pre-existing_). The reader decides what it means.

A witness also leaves the scene untouched. Never edit source, tests, fixtures, snapshots, config, or lockfiles; never skip, deselect, or rerun a test to get green; never `checkout`, `stash`, or `pull` mid-run. One run per tier; its result is the result.

Each run writes to `./qa_regression/<YYYYMMDD-HHMMSS>/` under the project root: one `tierN.log` per tier that ran, plus `REPORT.md`. Apart from what the project's own commands generate (caches, build output), that directory is the only thing a run adds to the tree.

## 1. Pin the change

The **base** is the merge-base of `HEAD` with the ref the user names (target branch, PR base, commit), otherwise with the remote default branch. The change is everything between base and the working tree — committed, staged, unstaged, and untracked: `git diff --name-status <base>` plus `git ls-files --others --exclude-standard`, excluding `qa_regression/` itself. Record the base ref and SHA, the `HEAD` SHA, and whether the change includes anything uncommitted (staged, unstaged, or untracked).

If this is not a git repository or no base resolves, ask the user for the base.

**Completion:** the changed-file list, base SHA, and head SHA are recorded.

## 2. Find the tier commands

Find the commands the project itself uses for its full unit suite (tier 2) and its end-to-end suite (tier 3). Search in this order and take the first authoritative hit:

1. CI config (`.github/workflows/`, `.gitlab-ci.yml`, `ci.yml`, `Jenkinsfile`, …) — what the team actually gates on;
2. task runner (`Makefile`, `package.json` scripts, `tox.ini` / `noxfile.py`, `justfile`, `Taskfile.yml`);
3. docs (`README`, `CONTRIBUTING`, `CLAUDE.md`, `AGENTS.md`).

One rule sorts suites into tiers: a test that needs no running service is tier 2; a test that needs the system up — server, browser, database, cluster — is tier 3. Integration suites that need services therefore belong to tier 3. For tier 3, also find how the project brings its environment up and tears it down.

**Completion:** tiers 2 and 3 each have a command taken from one of those files, or `N/A` plus the list of places checked.

## 3. Map the change to impacted tests

Tier 1 runs the **impacted targets** with tier 2's command — or, when that is a wrapper such as `make test`, with the runner invocation inside it, every flag kept. Selecting the targets — by path, test id, or the runner's own pattern flag (`-p`, `-k`, `-run`) — is the only change tier 1 makes to the command.

- If the project has affected-test tooling (`nx affected`, `turbo --filter=...[<base>]`, `bazel query rdeps`, `jest --findRelatedTests`, `vitest related`), its answer is the target set.
- Otherwise take the union of: the changed test files; each changed file's own test unit (`foo.py` → `test_foo.py`, `Foo.java` → `FooTest.java`, a Go file → its package, a Rust file → its crate); and every test file that imports or `#include`s a changed module.

Tier 1 covers only tests that tier 2's runner runs. A changed test file that belongs to tier 3 maps to `(tier 3)`.

**Completion:** every changed file appears in the mapping with at least one test target, `(tier 3)`, or `(none)`. If no file maps to a tier 1 target, tier 1 is N/A.

## 4. Run the tiers

Run tier 1, then 2, then 3. For each tier:

- Run its command once, in the foreground, to completion — stdout and stderr into `tierN.log`, wall-clock time recorded. Never report a tier that is still running. Only if a run hangs past the time limit the project's CI sets for that job, or one the user gave, kill it.
- Run the command as found. Beyond tier 1's target selection, add no fail-fast, filter, retry, or snapshot-update flags, and drop none of the project's own. Within a tier every failure is collected; fail-fast applies only _between_ tiers.
- Setup the project's CI or docs prescribe for the command (dependency install, build, codegen) is part of the tier. When setup fails, the tier is red — do not improvise a fix.
- Tier 3 runs only against an environment the project brings up for it, or a target the user named — never against production or a shared environment on your own initiative. Tear down what you started once the tier ends, pass or fail.

Then read the tier's status:

- **PASS** — exit code 0 _and_ the runner reports zero failed or errored tests.
- **FAIL** — anything else: a failed or errored test; a setup, build, or collection failure; a non-zero exit with every test green; a killed run.
- **N/A** — nothing to run (step 2 or 3 found no command or no target). Continue to the next tier.

On FAIL, stop. Every later tier is **NOT RUN**.

**Completion:** each tier holds exactly one status, and at most one is FAIL — with every tier after it NOT RUN.

## 5. Report

Fill in the fixed format of [REPORT-FORMAT.md](REPORT-FORMAT.md), save it as `REPORT.md` in the run directory, and return it verbatim as your final output. PASS, FAIL, and INCOMPLETE runs share the one format.

Before returning it, cross-examine every line: a line that is not something you saw in a log, in git, or in a command you ran comes out.

**Completion:** the report matches the format field for field, lists every failure point of the red tier (capped as the format allows), and every line traces to a log line, a git fact, or a command you ran.
