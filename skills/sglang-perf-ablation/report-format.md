# Report Format

Every result appends one row to the single `report.md`, under this header:

```markdown
| 数据集 | 并发 | 请求数 | 配置 | 任务总耗时(s) | QPS | OTPS | TTFT | ETE | TPOT | 输出总token | 平均输出 | 接受率(per-position) |
|-|-|-|-|-|-|-|-|-|-|-|-|-|
| throughput_1k | 64 | 128 | 静态两步 | 71.56 | 1.79 | 2212.17 | 3448.23 | 27632.01 | 20.73 | 158308 | 1236.8 | [0.84, 0.76] |
```

## Column source

Every column comes from the benchmark script output (the `sglang.bench_serving` summary block) except where noted. Match by the printed label:

| Column | Source |
|-|-|
| 数据集 | dataset name passed to the benchmark script (`--dataset-name` / scenario tag) |
| 并发 | `--max-concurrency` argument |
| 请求数 | `Successful requests:` (or `--num-prompts` if all completed) |
| 配置 | the ablated-knob values for this combo, short human label (only the varied params) |
| 任务总耗时(s) | `Benchmark duration (s):` |
| QPS | `Request throughput (req/s):` |
| OTPS | `Output token throughput (tok/s):` |
| TTFT | `Mean TTFT (ms):` |
| ETE | `Mean E2E Latency (ms):` |
| TPOT | `Mean TPOT (ms):` |
| 输出总token | `Total generated tokens:` |
| 平均输出 | 输出总token ÷ 请求数 |
| 接受率(per-position) | per-position acceptance list (see below) |

## Per-position acceptance

Only present for speculative-decoding runs. It is the list of accept rates at each draft position, e.g. `[0.84, 0.76]` for a 2-step draft. `bench_serving --output-details` reports `Accept length` (mean τ, single number); the per-position vector comes from the server side (`spec_accept_rate` in `server_info` / `internal_states`, or the scheduler's per-position accumulator in `server.log`). Pull it from `server.log` / `/server_info` when spec decoding is on; leave the cell blank (`-`) for non-spec runs.

## Rules

- Append each row the moment its benchmark finishes — never buffer rows to the end.
- A failed launch or benchmark still gets a row: fill 配置, put the failure reason across the metric cells.
- Keep the 配置 label to the varied knobs only, matching the experiment list confirmed at the launch gate.
