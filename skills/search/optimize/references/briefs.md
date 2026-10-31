# Brief templates

A subagent knows nothing of this run. A brief is the whole of what it knows, so it must stand on
its own. Fill every field; delete none. Dispatch with the Agent tool using a general-purpose
subagent and background execution (`run_in_background: true` where offered). Do not create agent
definition files or add a second automatic worktree: the lead already created the checkout.

## Common header (every kind)

```
Goal: <what you want and why, in two or three sentences>
Worktree: <absolute path>      Branch: <branch or detached>      Base commit: <full SHA>
First run: cd <worktree> && git rev-parse --show-toplevel && git branch --show-current
Confirm both match before you change anything (an empty branch name means detached HEAD).
Work only in this worktree.
Return by: <ISO timestamp before the run deadline>; stop work and hand back by this time.
Measurement setup: <build/start/readiness/stop commands, runtime arguments, resource allocation>.
Measurement script: <absolute installed path to measure/scripts/measurements.py; no filesystem search>
Service owner: <absolute installed path to measure/scripts/service.py>
Every benchmark, even unrecorded sanity load, uses the measurement script run -- wrapper.
Task: .vibesys/tasks/<task>/ (read OBJECTIVE.md). Run directory: <abs path of $RUN>
Load these skills first: <vibesys:implement-hypothesis | judge-candidate | combine-candidates |
  profile-revision>, vibesys:measure, and <domain skills>.
Rules:
- Never edit anything under .vibesys/, the task's checker or its benchmark.
- No stub, special case or shortcut that makes a check pass or a number rise without the
  program really doing the work. Obey every rule in OBJECTIVE.md.
- Implement and combine workers commit before reporting; judge and profile workers edit nothing.
```

Domain skills: pick from the plugin's skill list those whose description fits the task's
`[agent] domain` and `backends` in `vibesys.input.toml`. Give their full namespaced names. When
none fits, name none.

## Implement

Add:

```
Hypothesis <id>: <a claim that a measurement can falsify>
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

Add:

```
Candidate: <full SHA>      Parent: <full SHA>
Claim: <the implementer's claim>      Recorded verdict: <from vibesys:measure compare, or none>
Read only. Edit nothing. Run the task correctness command yourself in this worktree,
on your allocated port. Use the supplied service owner with JSON service/probe/client argv.
It bounds readiness and stops/verifies the whole group; never hand-write PID/sleep recipes.
Run the supplied absolute measurement script compare for this exact candidate and parent.
Read its samples, gain, gate status and pooled ranges. Missing gate or speed data is
unmeasured; a failed gate is fail.
Return pass, fail or unmeasured with the command, port, exit status and reasons.
```

Give the judge a detached worktree of the candidate: `git worktree add --detach <path> <SHA>`.

## Combine (and merge)

Add:

```
Main commit: <SHA>      Apply in this order, each as the diff from where it was built:
  1. <hypothesis id> at <SHA>, built on <parent SHA>
  2. ...
How to combine them: <which side wins a conflict; what must survive>
Watch for: <where the parts interact>
Hand back: the commit, the parts you could not apply and why, the checks you ran.
```

A merge is a combine whose result may become main. The lead decides that after measuring it.

## Profile

Add:

```
Question: <one question about one revision>      Revision: <SHA>
Decision it informs: <the choice that depends on the answer>
Change no code. Hand back findings with the evidence for each, and say which decision they
inform.
```

Read-only worktree of the revision, as for the judge.
Artifacts go in `<run dir>/profiles/<id>/`.
