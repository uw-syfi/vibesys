# Brief templates

A subagent knows nothing of this optimization. A brief is the whole of what it knows, so it must
stand on its own. Dispatch a general-purpose subagent with a fresh context, running in the background
(see [harness/](harness/README.md)). Do not create agent definition files or add
a second automatic worktree: the lead already created the checkout. Layout and plan files:
`vibesys:state`. Workers and judges never edit plan files.

## Common header (worker, judge, combine, profile)

```
Goal: <what you want and why, in two or three sentences>
Worktree: <absolute path>      Branch: <branch or detached>      Base commit: <full SHA>
First run: cd <worktree> && git rev-parse --show-toplevel && git branch --show-current
Confirm both match before you change anything (an empty branch name means detached HEAD).
Work only in this worktree.
Measurement setup: <build/start/readiness/stop commands, runtime arguments, resource allocation>.
Measurement script: <absolute installed path to measure/scripts/measurements.py>
Service owner: <absolute installed path to measure/scripts/service.py>
Every benchmark, even unrecorded sanity load, uses the measurement script run -- wrapper.
Task: .vibesys/tasks/<task>/ (read OBJECTIVE.md). State dir (--dir): <abs path>
Load these skills first: <the matching vibesys skill>, vibesys:measure, and <domain skills>.
Rules:
- Never edit anything under .vibesys/, the task's checker or its benchmark.
- No stub, special case or shortcut that makes a check pass or a number rise without the
  program really doing the work. Obey every rule in OBJECTIVE.md.
- Implement and combine workers commit before reporting; judge and profile workers edit nothing.
```

Domain skills: pick from the plugin's skill list those whose description fits the task's
`[agent] domain` and `backends` in `vibesys.input.toml`. Give their full namespaced names.

## Implement

Add:

```
Plan: plan-NNN-<slug>: <a claim that a measurement can falsify>
Why: <the evidence: a profile, a measurement, a code reading>
Pass criteria: <what you will observe if it is right, in the task's metric>
Scope: <files or components you may change; what to leave alone>
First step: <the smallest change to the claim that you can finish, commit and measure in this
  turn>. Do it and measure it before anything larger.
Parent: <base commit, or the candidate commit you build on and its measured value>
Hand back: claim, commit SHA, the verdict from vibesys:measure, what you learned, the next step
  you propose.
```

## Judge

Give the judge a detached worktree of the candidate: `git worktree add --detach <path> <SHA>`.

```
Candidate: <full SHA>      Parent: <full SHA>
Claim: <the implementer's claim>      Recorded verdict: <from vibesys:measure compare, or none>
Read only. Edit nothing. Run the task correctness command yourself in this worktree,
on your allocated port, with the supplied service owner (JSON service/probe/client argv).
Run the supplied measurement script compare for this exact candidate and parent.
Missing speed evidence is unmeasured.
Return pass, fail or unmeasured with the command, port, exit status and reasons.
```

## Combine

```
Main commit: <SHA>      Apply in this order, each as the diff from where it was built:
  1. plan-NNN at <SHA>, built on <parent SHA>
  2. ...
How to combine them: <which side wins a conflict; what must survive>
Watch for: <where the parts interact>
Hand back: the commit, the parts you could not apply and why, the checks you ran.
```

## Profile

Read-only worktree of the revision, as for the judge. Artifacts go in `scratch/`.

```
Question: <one question about one revision>      Revision: <SHA>
Decision it informs: <the choice that depends on the answer>
Change no code. Hand back findings with the evidence for each, and say which decision they
inform.
```

## Plan

A planner gets ONLY this, in a fresh context. No idea, no area.

```
Task: <task>      Plan number: NNN      Nudge: <optional, one short phrase, or none>
State dir: <abs path>
Load vibesys:plan-hypotheses and vibesys:state. Write only your own plan file.
```
