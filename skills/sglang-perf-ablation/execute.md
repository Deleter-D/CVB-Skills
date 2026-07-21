# Execute the Sweep

Reached only after the launch gate in `SKILL.md` step 6 has cleared. Run one experiment at a time per group; parallelize groups only up to the GPU-fit number from step 5.

## Parallelism

If GPUs allow N concurrent groups (N > 1), spawn one **SubAgent per group** so servers run in parallel. Give each SubAgent: its combo's params, a distinct port, its distinct GPU shard (`CUDA_VISIBLE_DEVICES`), its leaf directory, and this file. Each SubAgent runs the per-experiment loop below for its assigned combos and reports the extracted metrics back; the parent appends every returned row to the single `report.md`. If N == 1, run serially in the parent.

Never let two concurrent servers share a port or a GPU id.

## Per-experiment loop

For each combo:

1. **Launch the server.** Run the parameterized launch script with this combo's knob values and its port/GPU shard, redirecting output to the leaf dir's `server.log`.
2. **Wait until ready.** Poll the server (e.g. `/health` or `/get_model_info`) until it answers, or the log shows a fatal error. Do not start the benchmark against a server that is not up — a connection-refused benchmark is a silent zero. If it never comes up, record the failure in the report row and move on.
3. **Benchmark.** Only after ready, run the parameterized benchmark script against this combo's port, redirecting to `benchmark.log`. Pass `--output-details` to `bench_serving` so per-request stats and acceptance are emitted.
4. **Extract and append.** Pull every column from `benchmark.log` (and `server.log` for acceptance) per [`report-format.md`](report-format.md) and append one row to `report.md` immediately — do not batch rows to the end, so a crash mid-sweep still leaves the completed rows.
5. **Restart policy.** If the user chose restart-between-groups, kill this server before the next combo on the same shard. Otherwise reuse it only if the next combo shares the identical server config (rare in an ablation — usually every combo needs a fresh server).

**Completion:** every combo in the matrix has a row in `report.md` (a launch/benchmark failure counts as a row, with the reason in place of numbers).

## Final analysis

After the last row, add an analysis section to the bottom of `report.md`:

- name the best config per dataset/concurrency and by which metric (QPS, OTPS, TPOT);
- read the trends across the ablation axes (e.g. "OTPS climbs with `num_steps` until acceptance per-position drops below X, then TPOT regresses");
- give concrete tuning recommendations grounded in the table rows, citing the row that justifies each.

Do not recommend from intuition — every suggestion names the rows that support it.

**Completion:** `report.md` ends with an analysis section whose every recommendation cites specific result rows.
