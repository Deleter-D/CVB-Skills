# DEBUG_MEMORY.md format

One entry per iteration, numbered to match the patch file. Append in order; never rewrite a past entry.

```markdown
# Debug log

[#1][2026-07-07 14:32:05]

- Hypothesis: the suspected root cause
- Code change: the change made based on the hypothesis
- Patch: /path/to/patch
- Result:
- Conclusion:
```
