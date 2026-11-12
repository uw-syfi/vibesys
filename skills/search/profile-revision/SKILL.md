---
name: profile-revision
description: "Use when a vibesys optimize brief asks you to answer one question about one revision with evidence, to inform the lead's next decision. Changes no code and produces no candidate: runs the profiler, trace or measurement the question needs on a read-only checkout and hands back findings with the decision they inform."
---

# Profile a revision

You answer one question about one revision. You change no code and produce no candidate. The
lead plans its next work from your answer, so a clear "unsupported" beats a guess.

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

## Start

Run the first command of your brief and confirm the worktree: a checkout of the revision. Read
the question and the decision it informs. Read `OBJECTIVE.md`. Load `vibesys:measure` and the domain skills the brief
names; they usually say which profiler fits. Details are in
[findings.md](references/findings.md).

## Work

1. Decide what evidence would answer the question, and the cheapest way to capture it.
2. Build in the checkout if the task needs it. Edit no tracked file, except as step 4 allows. Scratch output, traces and
   reports go in the scratch directory your brief names, not in the checkout.
3. Capture on the workload the benchmark uses, or one close enough to say so. A capture that
   drives load is a `vibesys-cli submit command <rev> --image harness -- <argv...>` job
   (`vibesys:measure`; `--wrap '<profiler>'` profiles the candidate's server), and
   its output is read with `vibesys-cli logs <job>`. A capture script of your own can travel as
   the job's argv (`-- python3 -c "<script>"`), so it needs no commit.
4. If the program cannot show what the question needs (it serves no profiler, its binaries carry
   no symbols, it has no hook to time), say so and stop, unless your brief names an
   instrumentation branch. A job runs only a commit, so a scratch copy outside the checkout is
   never what runs. On that branch, make one `wip:` commit that adds the instrumentation and
   nothing else: no change to what the program computes or returns. Profile that commit. Never
   measure, check or hand it back as a candidate, and say in the findings what the
   instrumentation may cost the run.
5. Answer from what you captured. Aggregate timings do not show where time goes inside a phase;
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
