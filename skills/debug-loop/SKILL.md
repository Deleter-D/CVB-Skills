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
- `base/` — frozen snapshot of the user's reproduction scripts (see below).
- `experiments/` — one subdirectory per iteration, each a copy of `base/`.
- `patches/` — directory for patch files.

### Snapshot the reproduction scripts into `base/`

The user's own scripts are the experiment apparatus. Copy them **before** touching anything:

1. Walk the reproduction the user described and enumerate every file it reads or executes — launch/serve scripts, client or benchmark scripts, `env`/wrapper scripts, config and JSON files they point at, small input/prompt files. Follow one level of indirection: if a script sources or invokes another script, that one is part of the repro too.
2. `cp` each into `claude_debug/base/`, keeping the original filename. If two sources collide, prefix with the parent directory name.
3. Record the mapping in `BACKGROUND.md` under `# Repro scripts` — one line per file: `base/<name> ← <original absolute path>`. Without this, provenance is lost the moment the originals change.
4. Do **not** copy model weights, datasets, or logs. Reference large inputs by absolute path from inside the scripts.

`base/` is **frozen**. Never edit or run a modified `base/`; it is the control arm every later experiment is measured against. If the user's originals legitimately change upstream (rebase, new repro), snapshot again into `base.2/`, `base.3/`, … and note the switch in `BACKGROUND.md` — append, never overwrite.

Completion criterion: `BACKGROUND.md`, `DEBUG_MEMORY.md`, `patches/`, `experiments/` exist; `base/` holds every script the repro touches with its provenance recorded; `BACKGROUND.md` has at minimum the reproduction command and the actual (broken) result.

## Reproduction gate

Before any code change, confirm in `BACKGROUND.md`:

- The bug reproduces **stably**.
- A **minimal** reproduction exists.
- Run command, environment variables, and config are recorded.
- The bug reproduces from the `base/` copies, not just from the user's originals. A snapshot that doesn't reproduce is a broken apparatus — usually a relative path that only resolved in the original directory.

If reproduction is not stable, **stop** — the next problem to solve is reproduction, not the code. Do not enter the **loop**; do not modify code chasing a **hypothesis** you cannot observe.

Completion criterion: every gate question answered "yes". If any is "no", the iteration's work is to upgrade reproduction until it is.

## The loop

Each iteration:

1. **Read `DEBUG_MEMORY.md` first.** A **hypothesis** already tested and rejected must not be re-tested. The patches and experiment directories record what was tried; the memory records why it failed.
2. **Propose hypotheses.** Analyse the bug and offer the user distinct candidate root causes. Each must name one cause and predict one observable consequence if true. Let the user choose which path to verify.
3. **Record the chosen hypothesis** in `DEBUG_MEMORY.md` under the next iteration number `[#N][YYYY-MM-DD]`, before any code change.
4. **Fork the apparatus**: `cp -r claude_debug/base claude_debug/experiments/N-<slug>`, where `N` is the iteration number and `<slug>` is a few words naming the hypothesis. All script edits for this iteration happen **inside that directory only** — never in `base/`, never in the user's originals. One iteration, one directory; if an iteration needs a second script variant, it needs a second iteration number.
5. **Make the change** the **hypothesis** predicts will fix the bug — one change, one hypothesis, no drive-by fixes. The change may be to the source tree, to the experiment's scripts (flags, env vars, config), or both.
6. **Produce a patch** for source-tree changes: `git diff` against HEAD, saved to `claude_debug/patches/N.<short-commit>.patch`, where `<short-commit>` is HEAD when the patch was made. Script-only iterations need no patch — the experiment directory *is* the record. Record both paths in the iteration's memory entry.
7. **Verify.** Run the reproduction from `claude_debug/experiments/N-<slug>/`, and write its stdout/stderr to a log inside that same directory. Record the result.
   - If **solved**: create `SOLUTION.md` using [SOLUTION-FORMAT.md](./SOLUTION-FORMAT.md); the **loop** ends.
   - If **not solved**: revert the source change (`git apply -R` the patch), keep the patch file and the experiment directory intact, and start the next iteration. Never delete or clean up a past experiment directory — a directory that no longer exists is a dead end someone will walk into again.

Completion criterion: `SOLUTION.md` exists, or the user has explicitly ended the **loop**.

## Artefacts

Two kinds of permanent record, and neither is ever pruned:

- **`patches/`** — the source-tree half of a tried hypothesis. Rejected patches are kept; the filename ties a patch to its `DEBUG_MEMORY.md` entry by iteration number.
- **`experiments/N-<slug>/`** — the apparatus half: the exact scripts, flags, and env that produced the recorded result, plus their output logs. Diffing `experiments/N-<slug>/` against `base/` shows precisely what the ablation changed, which is the claim the iteration is actually making.

Together with `DEBUG_MEMORY.md` they answer, for any past iteration: what was believed, what was changed, what was run, and what came out.
