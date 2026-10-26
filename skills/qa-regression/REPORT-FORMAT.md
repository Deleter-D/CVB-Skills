# Report format

One report per run, the same shape for every verdict. Every field is filled; a value that was not observed is `-`, never an estimate.

````markdown
## QA Regression Report

- **Verdict:** FAIL — stopped at tier 2
- **Change:** `origin/main@1a2b3c4` → `HEAD@5d6e7f8` (+ uncommitted), 6 files
- **Run dir:** `qa_regression/20261026-112650/`

| Tier | Status | Command | Passed | Failed | Skipped | Exit | Time |
|-|-|-|-|-|-|-|-|
| 1 Impacted | PASS | `pytest tests/test_parser.py tests/test_lexer.py` | 48 | 0 | 0 | 0 | 4s |
| 2 Full unit | FAIL | `make test` | 1203 | 2 | 5 | 2 | 1m12s |
| 3 End-to-end | NOT RUN | `make e2e` | - | - | - | - | - |

### Failure points — tier 2

1. `tests/test_cache.py::test_evict_lru`
   - Location: `tests/test_cache.py:88`
   - Error:
     ```
     AssertionError: assert 3 == 2
     ```
   - Log: `tier2.log:1532`
2. `tests/test_cache.py::test_ttl_expiry`
   - Location: `src/cache.py:142`
   - Error:
     ```
     KeyError: 'expires_at'
     ```
   - Log: `tier2.log:1587`

### Tier 1 scope

| Changed file | Test targets |
|-|-|
| `src/parser.py` | `tests/test_parser.py` |
| `src/lexer.py` | `tests/test_lexer.py`, `tests/test_parser.py` |
| `tests/e2e/test_search.py` | (tier 3) |
| `docs/usage.md` | (none) |
````

## Header

- **Verdict** — `PASS` when all three tiers PASS; `FAIL — stopped at tier N` when tier N is FAIL; otherwise `INCOMPLETE — tier N N/A`, naming every N/A tier.
- **Change** — the ref the user named (or the default branch) with the merge-base SHA → head SHA, from step 1; `(+ uncommitted)` only when the change includes staged, unstaged, or untracked files; the changed-file count.
- **Run dir** — this run's directory.

## Tier table

Always three rows, in tier order.

- **Status** — `PASS`, `FAIL`, `N/A`, or `NOT RUN`, per step 4.
- **Command** — exactly as run. N/A: `none found in: <places checked>`. NOT RUN: the command that would have run, or `-`.
- **Passed / Failed / Skipped** — from the runner's summary, copied or derived by arithmetic on the numbers it prints (unittest prints only `Ran N` and failure counts); a category the printed summary omits is 0; errored tests count as Failed. `-` when the runner printed no summary.
- **Exit / Time** — the exit code of the command as run, and its wall-clock duration (`0.4s`, `4s`, `1m12s`).

## Failure points

Only on FAIL, headed with the red tier's number. One numbered entry per failure, in log order:

- **Target** (the entry's first line) — the test id exactly as the runner prints it (`file::test`, `pkg.TestName`, `Suite › case`). When the failure is not a test, a stage tag instead: `[setup]`, `[build]`, `[collection]`, `[environment]`, `[killed]`, or `[exit N]` for a non-zero exit with no failed test behind it.
- **Location** — the `file:line` the runner prints, repo-relative; `-` when it prints none.
- **Error** — verbatim from the log: the assertion or exception line, plus at most 9 lines that belong to it (diff, message continuation). Not the runner's banner or the traceback frames above it. Never paraphrase, translate, or cut mid-line. Mask anything that looks like a credential.
- **Log** — `tierN.log:<line>` of the assertion or exception line.

Past 30 failures, list the first 30 and close with `… and N more — see tierN.log`.

## Tier 1 scope

Every changed file from step 1, each with its test targets from step 3, `(tier 3)`, or `(none)`. An empty change reads `No changed files.` in place of the table.

The report ends here. It has no other section — no summary, no analysis, no next steps.
