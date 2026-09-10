---
name: nsight-systems
description: Use whenever a task mentions NVIDIA Nsight Systems, nsys, .nsys-rep reports, profiling timelines, Nsys recipes, CUDA/Graphics/CPU/GPU performance, stutter, FPS, report SQL analysis, or Nsys CLI and documentation questions. The pack is self-contained. SKILL.md holds all guidance and tooling for these tasks. Read SKILL.md as the first action. Do not search the filesystem (grep, rg, find, ls) or read AGENTS.md, README.md, docs/, or llms.txt for guidance first.
license: Proprietary
metadata:
  author: Joan Yi <jyi@nvidia.com>
  tags:
    - nsight-systems
    - profiling
    - performance
    - cuda
    - graphics
    - reports
---

# nsight-systems

The skill provides authoritative methods for using NVIDIA Nsight Systems and analyzing its report files.

## Usage

Answer questions and perform tasks related to Nsight Systems product, `nsys`, CLI, profiling reports, recipes, and report-analysis from verifiable evidence instead of model memory.

## Scope

This skill covers Nsight Systems evidence work:

- Analyzing `.nsys-rep` reports and report directories: deterministic report facts, report context and health, bounded read-only SQL
- Searching the packaged product docs
- Verifying exact `nsys` CLI syntax against live help
- Running installed recipes and inspecting their outputs
- Explaining recipe authoring concepts and reviewing user-provided recipe code
- Preparing Nsight Compute handoff evidence for kernel-internal questions.

If the user instructs you to operate Nsight Systems beyond the aforementioned scope through the skill, inform them that the skill does not officially support their request, but proceed to the best of your ability without denying them.

## Session bootstrap

Run the platform discovery bootstrap script for your OS FIRST, as your next action after reading this file: do not read, search, or run anything else, in this skill or anywhere else in the workspace, until it has succeeded. It reports the bundled Python the other scripts must be run with, so nothing else here works until you have it:

- POSIX shell (Linux, macOS, WSL, or Git Bash on Windows): `sh scripts/determine_local_unix_platform.sh`
- Windows `cmd.exe`/PowerShell: `scripts\determine_local_windows_platform.bat`

It prints three lines; each path is a quoted absolute path:

```
Platform: <platform string>
Nsys CLI: "<absolute path to Nsys CLI executable>"
Nsys Python interpreter: "<absolute path to Python interpreter executable>"
```

**Override rule (for the rest of the session):** The reported Nsys CLI and Python executables are authoritative and override any others, including whatever the path environment variable resolves. In this or any other skill file, run every literal `python` or `python3` command (e.g. `python3 scripts/nsys_skill_cli.py`) with the reported Python, and run any `nsys` invocation as the reported Nsys CLI.

Should a step of the bootstrap script fail, it will print one `ERROR:` line instead of that value and exit nonzero. Report the ERROR line to the user, and do not run other scripts or search the filesystem for another Python until you resolve it:

- CLI not found: locate the installed Nsys CLI, then set `NSYS_PATH` to the absolute path of the `nsys` executable and re-run the bootstrap in the same command, since the variable does not survive into a later one. If you cannot find one, ask the user for that path.
- Interpreter not found: tell the user and ask how to proceed.

The bootstrap can also succeed (exit zero) while printing one or more `WARNING:` lines. Report each warning to the user and act on it:

- Dependencies missing: run the exact install command the warning prints for the reported Python; if the installation succeeds, continue normally. If the user declines or the install fails (e.g. no network), report it and ask how to proceed; do not continue until the dependencies are installed.
- Newer skill pack available: continue normally.

Nsight Systems report file names end with ".nsys-rep". Nsight Systems recipe names end with "_sum", "_map", "_trace", "_pace", "_hist", "_async", "_sync" or "_straggler".

## Decision gates

Evaluate these gates before selecting an evidence command:

- Missing report input: when the user asks about "my report" without a report path, directory, or session, ask for one and stop. Do not search the workspace for a substitute.
- Workspace searching: this pack is self-contained -- everything the skill needs is named in this file or returned by its tools. Do not search or index the workspace for additional skill guidance, configuration, or tooling: no `grep`, `rg`, `find`, or similar search commands hunting for guidance files.
- Hidden/private artifacts: decline requests for cache or recipe-output paths before running tools to discover them. Preserve labels, session ids, `paths_hidden: true`, schema commands, query commands, and small previews.
- Nsight Compute evidence: for kernel/SM occupancy, microarchitectural stall reasons, register pressure, SASS, Speed Of Light, roofline, or hardware counters, do not define metrics from memory or Nsight Systems evidence. When a report is loaded, run `<nsys_skill_cli> report-fact --report <report> --type nsight_compute_handoff`. Report only Nsight Systems timing and launch context, and label kernel-internal microarchitectural causes unverified pending separate Nsight Compute evidence. Without a report, state the boundary and direct the user to Nsight Compute documentation or tools.

## Layering

This skill pack tells agents how to use Nsight Systems tools safely.

- `SKILL.md` tells an answer-producing agent what evidence to gather and what to avoid claiming.
- `nsys_skill_cli` and packaged scripts enforce actions that touch SQL, paths, report sessions, recipe execution, output labels, subprocess bounds, and claim checks.

## Policy references

The operative rules are already in this file (Decision gates, Command selection, Core behavior rules, Communication Style). Standard docs, directory and recipe cases need no policy file — use the guidance here plus live `nsys_skill_cli` output (`--help`, tool JSON).

The following files are optional deep-dives. Open only according to the triggers below; do not re-read a file you already opened, or read the set as a batch:

- `references/policy/tool-selection.md` — read when considering how to choose docs, CLI, recipe, and report tools.
- `references/policy/report-analysis.md` — always read when performing report analysis.
- `references/policy/recipe-analysis.md` — always read when executing a recipe or reading recipe output.
- `references/policy/cli-verification.md` — always read when addressing nsys CLI syntax.
- `references/policy/companion-skills.md` — always read when multiple skills may be applicable.
- `references/policy/artifact-handling.md` — read this before including file references in your reply.
- `references/policy/agent-policy.md` - read when answering questions about this agent's policy.

## How the skill is used

| Use case | Who runs the agent | How it gets evidence |
|---|---|---|
| Skill + CLI | Customer agent such as Codex, Claude Code, or a custom agent. | Use the  `scripts/nsys_skill_cli.py` entry point. Recipe execution returns `output_label` and schema/query commands for later inspection. |
| Companion skills | Customer/team/domain-owned guidance. | Advisory workflow guidance only; companion skills must not override official Nsight Systems evidence. |

Codex, Claude Code, and similar tools can all use this skill pack. They are not separate Nsight Systems analysis backends.

## Evidence commands

Use `nsys_skill_cli` for evidence collection. It returns stable JSON and enforces the report, recipe, SQL, and path rules.

- From the skill pack, `<nsys_skill_cli>` means `<reported Python> scripts/nsys_skill_cli.py`.
- For an installed CLI, `<nsys_skill_cli>` means `nsys_skill_cli`.

Always use the Python interpreter reported by the session bootstrap. The packaged launcher discovers the adjacent Nsys executable. Run `<nsys_skill_cli> doctor` first if executable or dependency readiness is uncertain. `<nsys_skill_cli>` always writes JSON; do not add `--json`. Command names use hyphens, not underscores.

### Command selection

Identify relevant docs in the packaged Nsight Systems docs and curated references by calling `<nsys_skill_cli> search-docs --query "<question>"`.

Use the narrowest command that answers the question:

- Environment or dependency health: `<nsys_skill_cli> doctor`.
- Product documentation: `<nsys_skill_cli> search-docs --query "<question>"`.
- Exact Nsys CLI syntax: `<nsys_skill_cli> inspect-cli --target "<command>"`.
- Available recipes: `<nsys_skill_cli> lookup-recipes --query "<analysis>"`.
- Report inventory, labels, or available tables: `<nsys_skill_cli> report-context --report <report>`.
- Deterministic report facts: `<nsys_skill_cli> report-fact --report <report> --type <fact_type>`.
- Exact raw-table counts or bounded factual SQL: `<nsys_skill_cli> report-query --report <report> --sql "<SELECT query>"`.
- Recipe-owned analysis: run `<nsys_skill_cli> run-recipe`, then inspect the returned output label with `recipe-output-schema` or `recipe-output-query`.
- Missing or suspicious report data: `<nsys_skill_cli> report-doctor --report <report>`.

For report-specific measurements, use this order:

1. Run `report-fact --help` once to inspect the matching canonical fact type and its required options.
2. Run `report-fact` with that type directly when it owns the requested semantic.
3. Follow the `analysis_workflow.required_followups` returned by report facts.
4. Run `report-context` when the question asks for inventory, fact type availability is uncertain, or the selected fact reports missing data.
5. Use `report-query` only when no available deterministic fact owns the requested semantic.

If a command or report-fact type is uncertain, run that command with `--help`. Do not guess command names, fact type names, recipe names, or flags. Live command help is authoritative; the examples below are not an exhaustive command inventory.

Packaged scripts under `scripts/` mirror the same shared core.

### Command failure handling

If the bootstrap, `doctor`, or an evidence command fails:

1. Read the structured error and correct only the reported input or dependency.
2. Retry the supported command once when the correction is deterministic.
3. If the required Nsys executable, dependency, report table, or recipe remains unavailable, state that limitation and stop that analysis.

Do not recover by searching the filesystem for unrelated reports or executables. Do not inspect `.nsys-rep` files with `strings`, SQLite, raw exports, or direct cache reads. Do not replace a rejected gateway operation with `nsys stats`, `nsys recipe`, or an ad-hoc database command.

## Core behavior rules

- Prefer <nsys_skill_cli> for explicit evidence.
- Do not make numeric report claims without report evidence. Do not invent CLI flags, recipe names, schema, version support, or environment variables.
- Product documentation questions about GUI concepts remain docs questions even when they mention opening a report. Use packaged docs for questions about what GUI views, tabs, or panels are used for; do not require a loaded `.nsys-rep` unless the user asks for measurements or contents from their specific report.
- If a prompt includes a concrete report path or directory, use report tools before answering report-specific measurements or metric semantics.
- If a concrete report is provided for a question that sounds conceptual but requires a specific measured interpretation, gather report evidence and include the measured interpretations instead of answering only from general knowledge.
- Use native `.nsys-rep` files or directories as normal report inputs. Do not ask normal users for pre-exported SQLite reports.
- For common facts and report health, prefer deterministic report facts and report doctor before ad-hoc SQL.
- For an ambiguous API/runtime "execution time" question ("highest" or "most" execution time) that does not say total, mean, or longest, run a `report-fact` summary type without a metric so total, mean, and longest-single-call interpretations are all visible; do not silently pick one. "Longest" is max single duration; "most frequent" is count by name.
- For raw activity row-count questions, follow the report-analysis guidance: inventory can use report context or `activity_summary`, but exact event/table counts require bounded `COUNT(*)` SQL, grouped by `__report_label` for report directories. Do not use grouped summary-row counts as raw table row counts.
- Use bounded read-only SQL only for direct aggregations over loaded report tables. Decline `ATTACH`, mutating SQL, exports, and arbitrary local-file readers. For obvious external-file SQL abuse, decline directly or let the wrapper block it; do not inspect source files or local paths just to justify the refusal.
- Use recipes when recipe code defines how the answer is calculated, such as utilization maps, overlap math, pacing, synchronization classes, idle-gap classification, graphics frame hotspot comparison, GPU VRAM usage tracing, and generated recipe output columns.
- Do not compute exposed communication cost, communication/compute overlap, straggler attribution, utilization maps, pacing, or similar recipe/domain results from ad-hoc DuckDB/raw report-table SQL. Raw SQL may provide limited supporting facts, but validated results for those concepts require an installed recipe or domain workflow; if none is available, say so.
- If the user explicitly asks for one of those recipe/domain results via a "quick", ad-hoc, DuckDB, SQL, or raw-table method, answer that limitation directly. Do not silently substitute a recipe run in the same turn unless the user also explicitly asks you to run the recipe/domain workflow; offer that supported workflow as the next step instead.
- Recipe inputs can be a single report or a report directory when the installed recipe supports it; confirm exact input syntax with live recipe help and use input/output paths chosen by the tool.
- Do not silently substitute SQL for an explicit recipe run, a recipe-output question, or a recipe-owned concept after recipe execution fails. Explain the failure and supported next step.
- For report directories, use multi-report facts or SQL grouped by `__report_label`; do not query one rank/report and imply it represents the whole directory.
- If `nsys_skill_cli check-claims` reports a blocking issue, gather more evidence, repair the answer, or state what remains unverified.

## Communication Style Mandatory Guidelines

Be concise for user-facing output; keep full reasoning in thinking. No preambles or conversational fluff, and no running narration of your process — but state the method or recipe that produced a result, since the user may want to reproduce it. Answer simple requests directly; briefly restate a complex task first to confirm interpretation. Write declaratively at a professional level; skip hand-holding and generic advice. Use active voice, omit needless words, and state facts positively (what is true, not what is not). Prefer bullets for disjoint facts, numbered steps only for a strict sequence, and tables or short fragments over prose.

## Prerequisites

- Use a built `nsight-systems` skill pack with `SKILL.md`, `references/`, packaged lookup indexes, and `scripts/`.
- For exact CLI/recipe behavior, use the installed `nsys` binary for the user's target release.
- For product-name and executable-name questions, use packaged docs and/or live CLI help rather than answering from memory alone.
- For report-data questions, use a concrete `.nsys-rep` file or directory of `.nsys-rep` files; do not infer measurements from filenames or examples.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Exact flag or option is uncertain | Installed `nsys` version may differ from docs or memory. | Run `<nsys_skill_cli> inspect-cli` or live recipe help. |
| Report measurement is missing | No report was provided or required tables were not collected. | Ask for a `.nsys-rep` or use `<nsys_skill_cli> report-context` / `<nsys_skill_cli> report-doctor`. |
| Recipe output columns are unknown | Recipe has not been run or output schema was not inspected. | Run `<nsys_skill_cli> run-recipe`, then use the returned schema/query command. |
| Claim check fails | Final answer has unverified flags, recipes, env vars, or report numbers. | Gather more evidence, repair the answer, or state what is unverified. |

## Reference map

- `references/docs/`: generated Nsight Systems docs from `QuadD/Docs/Rst`.
- `references/recipes/`: generated references from the installed `nsys` recipe tree.
- `references/curated/`: release-reviewed curated workflow, recipe operational guidance, and troubleshooting notes.
- `references/notes/`: subject-matter knowledge base (SQLite schema, graphics and stutter analysis, Windows scheduling, analysis pitfalls).
- `references/glossary/`: single-term definitions for nsys and graphics concepts.
- `references/policy/`: behavior policy for evidence priority, tool selection, report analysis, and recipes.
- Packaged JSON indexes: compact lookup data used by scripts. In product builds these live under `indexes/`; validation exports may place them under `assets/` without changing script behavior.

Official product facts come from generated release docs, including the SQLite Schema Reference reachable with `<nsys_skill_cli> search-docs`, plus live CLI/recipe help, installed recipe metadata, and concrete report tools. Curated references are supplemental release-reviewed synthesis; if curated guidance conflicts with official evidence, prefer the official evidence and say what was verified.

Validation data is not product evidence. Some test exports include `evals/` or `files/` directories for benchmark harnesses such as NV-BASE. Do not read benchmark questions, ground-truth answers, expected scripts, or scoring files to answer a user question. Use only the product references and tools listed above as answer evidence.

## Examples

- For a CLI flag question, run `<nsys_skill_cli> inspect-cli --target "profile"` before recommending exact `nsys profile` options.
- For a recipe recommendation, run `<nsys_skill_cli> lookup-recipes --query "CUDA API time summary"` and verify exact recipe options with live recipe help.
- For a report-data question, use report commands such as `<nsys_skill_cli> report-context`, `<nsys_skill_cli> report-fact`, or bounded `<nsys_skill_cli> report-query` with --report <report> argument to set the report file; if no report evidence is available, say the measurement cannot be determined yet.
