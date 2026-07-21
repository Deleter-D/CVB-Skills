---
name: sglang-spec-verify-timing
description: Measure per-step GPU timing (duration and inter-step gap) of speculative-decoding verify steps like TARGET_VERIFY in SGLang decode profiling traces, then combine with accept length to recommend the optimal draft-step count. Use when the user wants to analyze SGLang spec-decode .trace.json(.gz) profiling files, compute the duration or interval of an annotated step, or decide how many speculative steps to run at a given concurrency.
---

Measure the average GPU **duration** and inter-step **gap** of a named step annotation (e.g. `step[TARGET_VERIFY bs=16]`) across decode profiling traces, one number per **group**.

The trap this skill exists to avoid: the same annotation appears three places with wildly different times. Only one is correct — the **main compute stream**.

## Where the annotation lives

- **CPU main thread** (`pid == tid`): only the launch time (~ms). Wrong — ignore.
- **Minor GPU streams** (`cat == gpu_user_annotation`, many `tid`s): partial/overlapped work. Wrong — ignore.
- **Main compute stream**: the `gpu_user_annotation` stream whose events have the **largest median duration**. This is the real per-step GPU time. The stream id is NOT fixed — it varies per rank (128 on some, 144 on others). Never hardcode it; detect it per file.

## Definitions

- **duration** — the annotation's `dur` on the main compute stream.
- **gap** — `next.start − prev.end` between consecutive steps on the main stream (idle between two steps, e.g. draft phase between two verifies). NOT start-to-start.
- **group** — one row of the result table: a distinct (config × concurrency) combination. Concurrency sets the batch size in the step name (`conc8` → `bs=8`, `conc16` → `bs=16`), so each group has its own exact step name. Pool all rank/TP files of a group together.

## Steps

1. **Find the traces and groups.** Glob the profiling dir for decode trace files (`*-DECODE.trace.json.gz`). Group them by config dir and concurrency. For each group determine the exact step name (substitute the concurrency's batch size). Completion: every decode file assigned to a group with its step name.

2. **Run the analyzer per group.** For each group, pass all its files at once to [analyze.py](analyze.py):
   ```
   python3 analyze.py --step-name "step[TARGET_VERIFY bs=16]" <group's *-DECODE.trace.json.gz files>
   ```
   The script auto-detects the main stream per file, drops the first & last step per file (warmup/cooldown) and >5×median outliers (profiling-window breaks), pools the files, and prints avg duration and gap in ms. Completion: every group has a duration and gap; note any file whose detected main stream looks off.

3. **Present one table**, rows = groups, columns = duration (ms) and gap (ms). Report units in ms. Point out trends (e.g. gap growing with draft steps).

4. **Ask the user for the average accept length** at each draft-step count (SGLang reports it as `accept length` / spec accept length). Timing is only the cost side; accept length is the benefit side and the skill cannot derive it from traces. Completion: an accept-length number for every step count in the table, or the user explicitly declines (then stop here).

5. **Give the throughput verdict.** Apply the **throughput model** below: build a table of tokens/s per group, name the best step count, and if throughput is still climbing at the largest measured N, recommend measuring more steps. Completion: a recommended step count per concurrency, with the marginal reasoning shown.

## Throughput model

Speculative decoding trades a longer iteration for more tokens per iteration. Maximise **throughput**, not either half alone:

- **iteration time** `iter(N)` = duration(N) + gap(N) — wall-clock of one decode step at N draft steps.
- **throughput** `tput(N)` = accept_len(N) / iter(N) — tokens/s per request (×1000 when iter is in ms). This is the quantity to maximise.
- **marginal rule** — raising N→N+1 pays iff `accept_len(N+1)/accept_len(N) > iter(N+1)/iter(N)`: the accept-length gain must beat the iteration-time growth.
- **keep climbing** — verify duration barely moves with N and gap grows only slightly, so each extra step is cheap; the optimum sits wherever accept length stops growing fast enough. If `tput` is still rising at the largest N you measured, the optimum is beyond it — recommend measuring more steps until `tput` turns over or the per-step accept-length gain saturates.


## Notes

- `.gz` originals are read directly by the script — nothing is decompressed to disk. If the user insists on `gzip -kd`, use `-k` to keep originals; the script also reads plain `.json`.
- `--trim N` changes how many steps are dropped from each end (default 1).
