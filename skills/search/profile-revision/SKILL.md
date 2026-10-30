---
name: profile-revision
description: "Use when a vibesys optimize brief asks you to answer one question about one revision with evidence, to inform the lead's next decision. Changes no code and produces no candidate: runs the profiler, trace or measurement the question needs on a read-only checkout and hands back findings with the decision they inform."
---

# Profile a revision

You answer one question about one revision. You change no code and produce no candidate. The
lead plans its next work from your answer, so a clear "unsupported" beats a guess.

## Start

Run the first command of your brief and confirm the worktree: a checkout of the revision. Read
the question and the decision it informs. Read `OBJECTIVE.md`. Load the domain skills the brief
names; they usually say which profiler fits. Details are in
[findings.md](references/findings.md).

## Work

1. Decide what evidence would answer the question, and the cheapest way to capture it.
2. Build in the checkout if the task needs it. Edit no tracked file. Scratch output, traces and
   reports go in the artifacts directory the brief gives, not in the checkout.
3. Capture on the workload the benchmark uses, or one close enough to say so.
4. Answer from what you captured. Aggregate timings do not show where time goes inside a phase;
   if the evidence does not cover part of the question, say which part and what is missing.
   Repeating the same capture cannot fill a field it does not collect.

## Hand back

```
Question: <as asked>      Revision: <SHA>
Answer: <direct, with the numbers>
Evidence: <paths of the captures, and the command that made each>
Unsupported: <parts of the question the evidence does not answer, and what would>
Informs: <the decision from the brief, and which way the answer points>
```

Do not propose and implement changes. Name the hypotheses the findings suggest if the brief asks
for them.
