# Brief templates

A subagent knows nothing of this optimization. A brief is the whole of what it knows, so it must
stand on its own. Dispatch a general-purpose subagent with a fresh context, running in the
background. Do not create agent definition files or add a second automatic worktree: the lead
already created the checkout. Where things live: `vibesys:state`. Workers and judges never write
plans; they submit jobs linked to the plan and hand back evidence.

## Common header (worker, judge, combine, profile)

```
Goal: <what you want and why, in two or three sentences>
Worktree: <absolute path>      Branch: <branch or detached>      Base commit: <full SHA>
First run: cd <worktree> && git rev-parse --show-toplevel && git branch --show-current
Confirm both match before you change anything (an empty branch name means detached HEAD).
Work only in this worktree.
CLI context: add `--task <task> --run <run id> --harness <harness> --agent <your role>` to every
  vibesys-cli call. Every agent sees your jobs through jobs, show and compare.
Task: .vibesys/tasks/<task>/ (read OBJECTIVE.md).
Plan: <n>      Budget: <at most this many measure jobs, samples and command jobs>
Load these skills first: <the matching vibesys skill>, vibesys:commit, vibesys:measure, and
<domain skills>.
Rules:
- Never edit anything under .vibesys/, the task's checker or its benchmark.
- No stub, special case or shortcut that makes a check pass or a number rise without the
  program really doing the work. Obey every rule in OBJECTIVE.md.
- All measurement and all load go through vibesys-cli jobs (vibesys:measure); report only numbers
  it printed. Link every measure you submit to your plan with `--plan <n>`: a measure that names
  neither `--plan` nor `--no-plan` is refused, and `--no-plan` is for the baseline or a head
  re-measure the lead asked for, never for a candidate.
- Implement and combine workers commit as vibesys:commit says before reporting (the candidate is a
  full commit with `Checks: pass`; the branch name and `Plan: N` come from the brief); judge and
  profile workers edit nothing.
- Finish every turn by measuring (within the budget), then delivering the handoff with the
  harness's handoff tool (`post_handoff` in a run that has it) as your last act. A prose summary
  is not a handoff; never end a turn without one.
```

Domain skills: pick from the plugin's skill list those whose description fits the task's
`[agent] domain` and `backends` in `vibesys.input.toml`. Give their full names, which carry the
plugin name (`vibesys-kernels-cuda:kernel-performance`).

## Implement

Add:

```
Plan: <n> <a claim that a measurement can falsify>   (read it: vibesys-cli plan view <n>)
Why: <the evidence: a profile, a measurement, a code reading>
Pass criteria: <what you will observe if it is right, in the task's metric>
Scope: <files or components you may change; what to leave alone>
Specialize to: <what the task fixes, from OBJECTIVE.md: model, hardware, workload class>. Where the task fixes them, generality is not a cost, but keep a specialization only if it shows a measured benefit (performance, memory footprint, ...). Never specialize to the benchmark's own content; note any dependence on the workload's shape in the commit body.
First step: <the smallest change to the claim that you can finish, commit and measure in this
  turn>. Do it and measure it before anything larger.
Parent: <base commit, or the candidate commit you build on and its measured value>
Measure: <whether you submit the measure yourself, or return `ready for measurement`>
Hand back: claim, commit SHA, job ids and the compare output, what you learned, the next step
  you propose.
```

## Judge

Give the judge a detached worktree of the candidate: `git worktree add --detach <path> <SHA>`,
with `<path>` in `<name>.worktrees/` beside the repository (`vibesys:state`), never inside it.
Remove it when the verdict is in.
When the repository has submodules (`.gitmodules`), run `git -C <path> submodule update --init --recursive`
too, so the judge can read their source.

```
Candidate: <full SHA>      Parent: <full SHA>
Claim: <the implementer's claim>      Job ids: <the candidate's measure job, or none>
Read only. Edit nothing. Read what vibesys-cli saved for the candidate (show), and its compare
against the parent. Missing speed evidence is unmeasured.
Return pass, fail or unmeasured with the commands run, their output and reasons.
```

## Combine

```
Main commit: <SHA>      Apply in this order, each as the diff from where it was built:
  1. plan <n> at <SHA>, built on <parent SHA>
  2. ...
How to combine them: <which side wins a conflict; what must survive>
Watch for: <where the parts interact>
Hand back: the commit, the parts you could not apply and why, the checks you ran.
```

## Profile

Read-only worktree of the revision, placed and removed as for the judge. When the question needs the program to
expose something it does not (a profiler endpoint, symbols), create a branch and worktree for
it at the revision and name it as the instrumentation branch; a profile of an earlier revision
can be reused on a later one by cherry-picking its instrumentation commit onto a new branch. Profiles that drive load run as
`vibesys-cli submit command` jobs; their output is read with `vibesys-cli logs`.

```
Question: <one question about one revision>      Revision: <SHA>
Decision it informs: <the choice that depends on the answer>
Instrumentation: none | branch <vibesys-work/<task>/profile-<topic>> at <SHA>, worktree <path>
Change no code, except one instrumentation commit on the branch named above. Hand back findings with the evidence for each, and say which decision they
inform.
```

## Plan

A planner gets ONLY this, in a fresh context. No idea, no area. It still gets the task's domain
skills, chosen as for the common header, because they carry how to bound and plan in that domain.

```
Task: <task>      Nudge: <optional, one short phrase, or none>
CLI context: <as above, with --agent planner>
Load vibesys:plan-hypotheses, vibesys:state and <domain skills>. Create only your own plan.
```
