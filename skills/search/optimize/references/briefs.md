# Brief templates

A subagent knows nothing of this run. A brief is the whole of what it knows, so it must stand on
its own. Fill every field; delete none. Dispatch with the Agent tool, in the background.

## Common header (every kind)

```
Goal: <what you want and why, in two or three sentences>
Worktree: <absolute path>      Branch: <branch>      Base commit: <full SHA>
First run: cd <worktree> && git rev-parse --show-toplevel && git branch --show-current
Confirm both match before you change anything. Work only in this worktree.
Task: .vibesys/tasks/<task>/ (read OBJECTIVE.md). Run directory: <abs path of $RUN>
Load these skills first: <vibesys:implement-hypothesis | judge-candidate | combine-candidates |
  profile-revision>, vibesys:measure, and <domain skills>.
Rules:
- Never edit anything under .vibesys/, the task's checker or its benchmark.
- No stub, special case or shortcut that makes a check pass or a number rise without the
  program really doing the work. Obey every rule in OBJECTIVE.md.
- Commit your work in this worktree before you report.
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
Read only. Edit nothing. Return pass or fail with reasons.
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

Read-only worktree of the revision, as for the judge. Artifacts go in `<run dir>/profiles/<id>/`.
