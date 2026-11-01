---
name: state
description: "Use when you create, resume or update the state of a vibesys optimization: the plan files, measurement records, worktrees and scratch output under .vibesys-state/optimize/<task>/. Defines the layout, the plan file format and who writes which part, how to find what is live, and how to resume after a stop."
---

# Optimization state

One optimization per task, stoppable and resumable at any time. Everything it knows is in
`.vibesys-state/optimize/<task>/` at the repository root: untracked and listed in
`.git/info/exclude` (the lead creates both on first use). The committed `.vibesys/` stays read-only.

```
plans/         plan-NNN-<slug>.md   one file per idea; NNN zero-padded, ascending, never reused
measurements/  <commit-sha>.jsonl   written ONLY by measure's measurements.py
worktrees/     every git worktree: main, one per plan, judges, throwaway measurement checkouts
scratch/       disposable machine output: logs, raw JSON, profiler output, cached external results
```

Result branch (the optimization's "main"): `vibesys/<task>`. Candidates: `vibesys-work/<task>/plan-NNN`.
Pass this directory to `measurements.py --dir`.

## Plan files

One hypothesis per file. The header is three single-line fields, then three sections, each with
one writer:

```
status: proposed|chosen|returned|won|parked|rejected
claim: <one line>
gain: <one line, set by the lead when the plan closes: measured gain with record ids, or why rejected/parked>

## Proposal   (a planner subagent) claim, mechanism, expected gain and its basis, files touched, how to test, evidence
## Decision   (the lead) chosen / rejected / deferred and why; duplicates; disjoint files with other chosen plans
## Result     (the lead, from hand-backs) worker brief text, branch, commit, record ids, judge verdict, lessons
```

Workers and judges never edit plan files; they hand back evidence. The lead assigns the number.
A planner names the file itself once it has chosen its idea.

Cheap index of all plans, without reading bodies:

```sh
grep -H -m3 -E '^(status|claim|gain):' plans/*.md
grep -l '^status: \(proposed\|chosen\|returned\)' plans/*.md    # what is live
```

## Measurements

Only `measurements.py` writes records; never edit a shard. A final (fresh) measurement of a
branch is one taken after its last move, on its current head, with several samples.
`measurements.py check --dir D` verifies the result branch head has a passing `better` record.
Slow external jobs (queues, clusters) must be idempotent and cached in `scratch/` by commit plus
evaluator fingerprint, so a relaunch never repeats or duplicates work.

## Resume

1. Read the open plans (`proposed`, `chosen`, `returned`), then the last few closed ones.
2. Check git (branches, worktrees, the head of `vibesys/<task>`) and the running agents for what
   is real, not what a plan claims.
3. Relaunch or park every plan whose agent is gone, and say so in its Decision section.
4. After a long gap or an evaluator change, re-measure main as a control (re-baseline).

## Housekeeping

Remove the worktrees of closed plans; keep their branches. Put bulky output in `scratch/`.
