# CVB-Skills

> **C**urated · **V**alidated · **B**attle-Tested Agent Skills

Every Skill in this repository has been **strictly curated** and **repeatedly validated in real engineering practice**. We do not collect toy scripts that "look useful" — we only keep workflows that have been used repeatedly under real codebases, real debugging pressure, and real delivery deadlines, and that still reliably produce the expected outcome.

Inclusion criteria:

- **Reproducible**: given the same input, the Agent follows the same process path every run — it does not gamble its way to the same output.
- **Bounded**: it states clearly what it does and what it does not do; it does not pretend to be universal.
- **Battle-tested**: it has run in a real project, been called repeatedly, and survived — not a one-off demo.

---

## Contributions Welcome

If you have a Skill that has been honed in real engineering practice and genuinely works well, **PRs are welcome**.

Before submitting, please self-check:

1. It solves a **recurring real problem**, not an occasional trick.
2. It has run and been used stably in at least one real project.
3. The `SKILL.md` frontmatter is complete, the description is precise, the trigger branches are clear, and there is no redundancy.
4. It does not duplicate an existing Skill's capability (check first whether it can be reused or merged).

Please attach to the PR: a brief description of the use case, the project context where it was validated (may be anonymized), and why it deserves to be included.

---

## References & Acknowledgements

The Skills in this repository were curated, organized, and locally adapted from the following open-source work:

- **[mattpocock/skills](https://github.com/mattpocock/skills)** — Matt Pocock's engineer Skills collection ("Skills for Real Engineers"). `grill-me`, `grilling`, `teach`, and `writing-great-skills` all originate from this repository.
- **[mermaid-js/mermaid](https://github.com/mermaid-js/mermaid)** — the official Mermaid.js project. The syntax documentation under `mermaid` Skill's `references/` is adapted from its official docs ([mermaid.js.org](https://mermaid.js.org)).
- **[anthropics/skills](https://github.com/anthropics/skills)** — Anthropic's official Agent Skills repository, which defines the `SKILL.md` format and the Agent Skills open standard (see [agentskills.io](https://agentskills.io)).

Thanks to the above projects and authors. The copyright of each Skill belongs to its original LICENSE; this repository as a whole is released under the MIT license.

---

## Skills Overview

| Skill | Trigger | Applicable Scenarios |
| ----- | ------- | -------------------- |
| `debug-loop` | user-invoked | Complex, hard-to-reproduce bugs needing systematic investigation; leaving a debugging archive for team retrospective; replacing "tweak-and-pray" debugging with controlled experiments. |
| `grill-me` / `grilling` | user / model-invoked | Stress-testing a plan before building; technical design review; forcing fuzzy architectural decisions into clarity one question at a time. |
| `mermaid` | model-invoked | Documentation figures, architecture/flowchart/ER/Gantt diagrams; needing directly-renderable Mermaid code rather than images; version-controlled diagrams that evolve with code. |
| `teach` | user-invoked | Systematically learning a new skill or domain across multiple sessions; wanting a traceable, resumable learning workspace; long-term retention over momentary fluency. |
| `writing-great-skills` | user-invoked | Authoring or refining a Skill; diagnosing why a Skill behaves poorly; establishing a team-wide Skill authoring standard. |

---

## License

MIT — see [LICENSE](./LICENSE).
