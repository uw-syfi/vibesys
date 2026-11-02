---
name: state
description: "Use when you create, resume or update the state of a vibesys optimization: what vibesys-cli stores (jobs, results, plans and their comments), what lives in git (the result branch, candidate branches, worktrees), who writes which part, how to find what is live, and how to resume after a stop."
---

# Optimization state

One optimization per task, stoppable and resumable at any time. Its state is split by owner. Read
and write the CLI's part through its commands only; never open its files.

## What `vibesys-cli` stores

All worktrees of the repository share one store per task, so any agent sees the same state.

| Kept by the CLI | Ask with |
| --- | --- |
| Jobs, their states, logs and notes | `vibesys-cli jobs --json`, `logs <job>`, `show <sha>` |
| Saved results: check passes and samples, per commit | `vibesys-cli show <sha> --json` |
| The head of the result branch and its fresh number | `vibesys-cli result --json` |
| Plans, their status and comments, and the jobs linked to them | `vibesys-cli plan list\|view` |
| The daemon that runs it all | `vibesys-cli daemon status` |

## What lives in git and on disk

- Result branch (the optimization's "main"): `vibesys/result/<task>`. The lead creates it once, at
  the base commit, and never checks it out. Only `vibesys-cli promote` moves it.
- Candidate branches: `vibesys-work/<task>/plan-N`, where N is the plan number.
- Worktrees: the lead picks one directory outside the repository's working tree and puts every
  worktree there (`git worktree add`), one per plan, one per judge or profile question. Each brief
  names its worktree. `git worktree list` is the truth about which exist.
- Scratch: disposable machine output (profiles, raw logs). Any directory the lead names in the
  briefs; nothing depends on it.

Commit rules: `vibesys:commit`. After each `git worktree add` where anyone will commit, the lead
runs `commit_schema.py install-hook --worktree <path>` (the `commit` skill's script). The hook is
per-worktree and chains to the repository's own hooks, so the user's checkout is untouched.

## Plans

One hypothesis per plan. The title is the claim; the body is the planner's proposal; every later
word is a comment. The CLI numbers plans and stores them as given.

Statuses: `open` (proposed, undecided), `chosen` (a worker is on it), `won` (promoted),
`parked` (worth revisiting; say what would change the answer), `rejected` (ruled out; say why).

| Writer | Writes |
| --- | --- |
| Planner | `plan create`, and `plan edit` of its own body |
| Lead | status (`plan edit <n> --status`, `plan close <n> --status s --comment ...`) and comments: the decision (chosen, rejected, deferred and why; duplicates; disjoint files), the worker brief text, the result (branch, commit, job ids, judge verdict, lessons), and on close the gain with its `compare` verdict or the reason |

Workers and judges never write plans; they link jobs with `--plan <n>` and hand back evidence.
Write each comment when the thing happens, not at the end.

Cheap index of all plans, without reading bodies: `vibesys-cli plan list --json`. What is live:
`plan list --status open,chosen`. A plan's linked jobs: `plan view <n>`.

## Resume

1. `plan list --status open,chosen`, then the last few closed plans.
2. Check what is real, not what a plan claims: `git branch --list 'vibesys-work/<task>/*'`,
   `git worktree list`, `vibesys-cli jobs --state queued,running`, `vibesys-cli result`, and the
   running agents.
3. Relaunch or park every plan whose agent is gone, and comment why. A job that is still queued
   or running needs no resubmission.
4. After a long gap or a change of environment, re-measure the head as a control
   (`submit measure <head> --no-cache`).

## Housekeeping

Remove the worktrees of closed plans (`git worktree remove`); keep their branches, and never
rewrite measured commits.
