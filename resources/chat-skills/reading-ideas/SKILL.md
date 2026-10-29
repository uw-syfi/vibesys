---
name: reading-ideas
description: Read one idea of a run and its attempts, steps and verdicts, to answer what was tried, why it was kept or dropped, and what it cost. Use for any question about a specific idea or step.
---

# Reading an idea and its attempts

An idea is one thing the run tried: a claim about why the code could be faster, and the change
that tests it. The run works an idea in steps; a step is one pass of implementing, evaluating
and judging. An attempt is one try at an idea: a step that fails or is judged not better can be
followed by another attempt at the same idea, so an idea can hold several.

1. `list_ideas` shows every idea with its step range and outcome. `[active]` marks the idea
   being worked now. An outcome of `pending` means no verdict yet.
2. `get_idea <id>` gives the claim, the action taken, the judge's verdict, and each step's
   result: whether it passed, was reviewed, its commit and its measured value.
3. `list_steps` is the same steps across all ideas in order. Use it for trends: which steps
   moved the result, and where it stalled.
4. For detail the tools omit, `list_state_files` then `read_state_file`. The commit of a step
   is in the workspace's git history: `git show <commit>` shows what changed.

Report what the evidence says and what it does not: a step with no measured value was not
measured, which is different from no change. Name ideas and steps by id so the operator can
find them on the run screen.
