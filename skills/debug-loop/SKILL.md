---
name: debug-loop
description: Debug a bug as a series of controlled experiments, each verifying one hypothesis.
argument-hint: "What's the bug?"
disable-model-invocation: true
---

A bug is debugged as a series of **controlled experiments**, not a string of code changes. Each iteration of the **loop** verifies exactly one **hypothesis**, produces **reproducible evidence**, and leaves a permanent record so no future iteration repeats the same investigation.

All artifacts live in `./claude_debug/` under the project root.

## Setup

If the bug isn't described in the invocation, ask the user for: the symptom, expected vs actual behaviour, and how they trigger it. Then create `claude_debug/` with:

- `BACKGROUND.md` — bug background, code version, reproduction, error. Use [BACKGROUND-FORMAT.md](./BACKGROUND-FORMAT.md). **Append-only**: supplement, never overwrite history.
- `DEBUG_MEMORY.md` — running log of the **loop**. Use [DEBUG-MEMORY-FORMAT.md](./DEBUG-MEMORY-FORMAT.md).
- `patches/` — directory for patch files.

Completion criterion: directory exists with all three above; `BACKGROUND.md` populated with at minimum the reproduction command and the actual (broken) result.

## Reproduction gate

Before any code change, confirm in `BACKGROUND.md`:

- The bug reproduces **stably**.
- A **minimal** reproduction exists.
- Run command, environment variables, and config are recorded.

If reproduction is not stable, **stop** — the next problem to solve is reproduction, not the code. Do not enter the **loop**; do not modify code chasing a **hypothesis** you cannot observe.

Completion criterion: every gate question answered "yes". If any is "no", the iteration's work is to upgrade reproduction until it is.

## The loop

Each iteration:

1. **Read `DEBUG_MEMORY.md` first.** A **hypothesis** already tested and rejected must not be re-tested. The patches record what was tried; the memory records why it failed.
2. **Propose hypotheses.** Analyse the bug and offer the user distinct candidate root causes. Each must name one cause and predict one observable consequence if true. Let the user choose which path to verify.
3. **Record the chosen hypothesis** in `DEBUG_MEMORY.md` under the next iteration number `[#N][YYYY-MM-DD]`, before any code change.
4. **Make the code change** the **hypothesis** predicts will fix the bug — one change, one hypothesis, no drive-by fixes.
5. **Produce a patch**: `git diff` against HEAD, saved to `claude_debug/patches/N.<short-commit>.patch`, where `N` is the iteration number and `<short-commit>` is HEAD when the patch was made. Record the path in the iteration's memory entry.
6. **Verify.** Run the reproduction from `BACKGROUND.md`. Record the result.
   - If **solved**: create `SOLUTION.md` using [SOLUTION-FORMAT.md](./SOLUTION-FORMAT.md); the **loop** ends.
   - If **not solved**: revert the code change (`git apply -R` the patch), keep the patch file, and start the next iteration.

Completion criterion: `SOLUTION.md` exists, or the user has explicitly ended the **loop**.

## Patches

Patches are the permanent artefacts of tried hypotheses. Rejected patches are kept — they document dead ends so the same path is never taken twice. The filename ties a patch to its `DEBUG_MEMORY.md` entry by iteration number.
