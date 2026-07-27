# DEBUG_MEMORY.md format

One entry per iteration, numbered to match the patch file and the experiment directory. Append in order; never rewrite a past entry.

```markdown
# Debug log

[#1][2026-07-07 14:32:05]

- Hypothesis: the suspected root cause
- Change: the change made based on the hypothesis (source, scripts, or both)
- Experiment: claude_debug/experiments/1-<slug>/   (diff vs base/ = what this ablation changed)
- Patch: claude_debug/patches/1.<short-commit>.patch   (omit if script-only)
- Result:
- Conclusion:
```

`Experiment` is mandatory — every iteration runs from its own copy of `base/`. `Patch` is omitted only when the iteration changed no source code.
