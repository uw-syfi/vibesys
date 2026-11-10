---
name: allocations
description: "Use when jobs of a vibesys optimization wait long in a cluster queue, or before a long stretch of jobs on a placement that queues. Says when to hold a standing allocation with vibesys-cli, how to start one, how to judge renewal from `placement capacity`, how to end it, and how to read `alloc show` honestly. The CLI provides the commands; you decide when to ask, renew and stop."
---

# Allocations

A standing allocation is one slot of a placement that your run holds over time. Jobs submitted into
it run one at a time and never queue for capacity again. `vibesys-cli` gives you the primitives;
whether and when to renew is your judgment, made fresh at every check-in. Commands:
`vibesys-cli alloc --help`; the full reference is in the CLI's README.

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`. If it is unset or empty, stop and say `VIBESYS_CLI is not set; export it as the absolute
path of the vibesys-cli to use`. Pass the context flags your brief names (`--run`, `--task`).

## Is one worth it

- Yes: the placement queues (a cluster), jobs wait for hours, and many jobs are ahead of you.
  Compare the waits in `vibesys-cli placement capacity <placement> --json` (`recent_waits`) with how
  long a job runs: if a wait is several times a job's length, pay it once.
- No: a `local` placement. A request there only reserves one of its job slots and never ends, so it
  is harmless but gains nothing; skip it unless a run should keep a slot from other runs. No work
  in sight: hold nothing and let it lapse.
- Refused with an error naming `placements.<name>.allocations`: the operator has not enabled it
  there. Do not work around it; submit as usual.

## Start

```
vibesys-cli alloc request <placement> --for 6h        # returns at once, usually PENDING
vibesys-cli submit measure <sha> --samples 5 --plan <n> --alloc any
```

`--alloc any` is the default choice: the job goes to a running allocation of your run if it fits,
else it takes the normal path, so it is never stuck. Use `--alloc <id>` only when the job must run
there; it is refused, with both durations, if it does not fit. A shorter `--for` can start sooner
(backfill) than a long one; ask for what the next jobs need, not the maximum. A job without
`--alloc` is unchanged.

## Renewal

The successor must start before the current one ends, or the agent idles for the whole wait.
Overlap costs a little capacity; a gap costs hours. Set a standing order and revisit it:

```
vibesys-cli alloc renew <id> --lead 4h --for 6h --until run-end
```

- Lead: about the p90 of `recent_waits[].waited` plus a margin. With fewer than about five samples,
  use 2x the longest wait seen. Samples with a very different `requested_for` say less.
- The lead must be shorter than `--for` (the CLI refuses it otherwise). If the wait you expect is
  longer than an allocation lasts, one allocation cannot cover it: request another one now as well.
- Read `placement capacity` at every check-in and call `alloc renew` again with the adjusted
  lead; the order is replaced, not stacked. Waits grow when `pending_jobs` grows or `nodes_idle`
  is 0.
- Slurm reservations (`start`, `end`, `node_count`, `usable`) can take the partition. If one starts
  before your allocation would end, or before a successor could run, renew earlier or ask for a
  length that ends before it starts.
- `--until run-end` makes the order stop when the run ends. Without it, say when it should stop.
- The old allocation is never handed over: it runs to its end, and `--alloc any` jobs go to
  whichever fits. If renewal was skipped (`renewal_skipped_reason`, for example `max_held`), end one
  you no longer need, or wait.

## End

When work runs out, `vibesys-cli alloc end <id> --after-jobs` (finish the queued jobs, then
release). Never leave an allocation held without work: it takes a slot from everyone. At the end of
the run, `alloc end-run`.

## Read `alloc show` honestly

- PENDING is not running. Nothing runs until it is RUNNING; do not plan around an unstarted slot.
- Slurm's estimated start is a guess that moves, often by hours. Plan from `recent_waits`.
- A job running when its allocation ends is `lost` with reason TIME_LIMIT. Size `--for` to the jobs
  you will put in, and check the time left (`alloc wait <id> --until ending-in <dur>`) before
  queueing a long one.

## Example

Placement `gpu-a`; recent waits 1h, 3h, 2h, 5h, 2.5h; jobs run about 40 minutes.

1. Waits are 3 to 8 times a job: worth it. `alloc request gpu-a --for 8h` (a 24h request would
   likely wait longer). Submit jobs with `--alloc any`.
2. Lead: p90 is about 5h, margin 1h, so `alloc renew a1 --lead 6h --for 8h --until run-end`. The
   successor is requested when 6h are left, so it should start about when a1 ends. With only two
   samples (longest 5h), 2x would be 10h, more than the 8h allocation allows as a lead: keep the
   order at `--lead 6h` and request a second allocation now so one is queued early.
3. At the next check-in `placement capacity` shows `pending_jobs` doubled and a reservation of the
   partition starting in 7h. a1 ends before then and a successor would wait behind it: lengthen
   the lead (`alloc renew a1 --lead 7h --for 8h --until run-end`) so it is queued first.
4. Two jobs left: `alloc renew a1 --off`, then `alloc end a1 --after-jobs`.
