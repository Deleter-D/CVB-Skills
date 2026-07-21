---
name: sglang-perf-ablation
description: Automated SGLang serving performance ablation. Sweep a matrix of config knobs, launch one server per combo (parallel across GPU shards when capacity allows), benchmark each, and collect every result into a single markdown report ending in a tuning analysis. Use when the user wants to performance-test or evaluate SGLang across several config variations (消融 / ablation / sweep) and has, or can supply, a base server-launch script and a benchmark script.
---

# SGLang Performance Ablation

Sweep an **ablation matrix** — the cartesian set of the config knobs the user wants to vary — running one SGLang server per combination and one benchmark per server, into **one** report.

The whole first half is setup behind **gates**: each numbered step below ends on a user confirmation, and nothing launches until the final gate clears. Do not run ahead of a gate to "save time" — a wrong matrix or GPU plan wastes an entire multi-hour sweep. The steps that actually launch servers and write results live in [`execute.md`](execute.md), reached only after the last gate.

## Two required scripts

This skill is useless without both:

- a **base server-launch script** — starts one SGLang server (e.g. `python -m sglang.launch_server ...`).
- a **benchmark script** — drives load at a running server (typically wraps `python -m sglang.bench_serving`).

## Steps

### 1. Confirm the two scripts

Locate the user's base launch script and benchmark script. If either is missing, stop and tell the user you need both before anything can run — name which one is missing.

**Completion:** you hold a path to a runnable launch script and a benchmark script, or you have asked for the missing one and are waiting.

### 2. Define the ablation matrix

Ask which config items to ablate and the value set for each (e.g. `speculative_num_steps ∈ {1,2,3}`, `attention_backend ∈ {fa3, flashinfer}`). These knobs, and only these, are the matrix axes; everything else stays fixed at the base script's value.

**Completion:** an explicit list of ablated knobs, each with its value set, that the user has confirmed.

### 3. Parameterize both scripts

Rewrite the launch script and benchmark script so every ablated knob is a CLI argument, then save the parameterized copies into the workspace (step 4). The launch script must also take **`--port`** (and host/base-url) as an argument so several servers coexist; the benchmark script must take the matching `--port`/`--base-url`. Port collision is the number-one cause of a corrupted parallel sweep — give every concurrent server a distinct port.

**Completion:** both scripts accept every ablated knob plus a distinct port as CLI args, and a `--help` or dry echo run shows the args wire through to the underlying command.

### 4. Build the workspace

Create one folder for the whole sweep containing:

- the two parameterized scripts;
- a nested directory tree partitioned by the ablated knobs — **ask the user which knob is the parent level and which are children**, then build that hierarchy; each leaf directory holds that combo's `server.log` and `benchmark.log`;
- one report markdown (`report.md`) seeded with the header row from [`report-format.md`](report-format.md). All results go in this one file — never spawn a second report.

**Completion:** workspace exists with the confirmed directory hierarchy and a `report.md` holding only the header table.

### 5. Plan GPU allocation

Run `nvidia-smi` to see total and free GPUs. Determine GPUs-per-service from the launch script (TP/DP/PP/EP sizes). Ask the user which GPU ids you may use. Compute how many service groups fit at once: `floor(len(usable_gpus) / gpus_per_service)`.

**Completion:** you know the usable GPU ids, GPUs-per-service, and the max number of concurrent groups.

### 6. Confirm run policy and the full experiment list — the launch gate

Two things, together:

- Ask whether to **restart the server between test groups** to avoid cache effects, and record the answer.
- List every pending experiment, showing **only the ablated params** for each (omit every constant), so the user can scan the matrix at a glance. Have the user confirm the list is correct.

**Completion:** the user has explicitly confirmed both the restart policy and the experiment list. Only now proceed to [`execute.md`](execute.md).
