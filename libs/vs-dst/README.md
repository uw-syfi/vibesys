# vs-dst

Test support: simulation tests of the run lifecycle that are short to write and cheap to run.
It sits above `vs-sim`, which knows time, seeds and scheduling but nothing about a run, and
adds what a run is made of: the run.log, its records, executors and their effects.

A test builds a world, simulates it and asserts on the result:

```python
def test_a_restart_after_any_crash_finishes_the_same_run():
    world = SomeStrategyWorld(...)
    normal = simulate(world)

    for crash in normal.crash_points():           # sampled by default; all with --depth=nightly
        recovered = normal.rerun_with_crash(crash)  # starts from normal's saved state, no prefix rerun
        assert recovered.result == normal.result, crash
        assert recovered.no_effect_ran_twice(), crash
```

`vs_dst.api` holds:

- `World` (a `typing.Protocol`): what a test builds. `setup` says which strategy runs
  (`RunSetup`: a fresh strategy factory, the `vs_core` `Harness` (which carries the run's
  limits and lifecycle capabilities), the reply schemas, and how the loop is arranged:
  `upgrade`, the search the code has become) and
  `__call__(request, core)` answers every request the executors receive. A world also carries
  its position (the scripted replies it has handed out), so it must be plain data that
  `copy.deepcopy` can copy; `setup` is immutable and is shared, never copied. A world that cannot be
  copied fails `simulate` with `WorldNotCopyableError`, naming its type.
- `simulate(world) -> SimulatedRun`: runs the whole run on the step loop (`vs_runtime`'s
  `run_step_loop` over a run.log kept in memory, a virtual clock, and executors that seal what
  each effect did).
- `SimulatedRun`: `result`, `world` (the world as the run left it, for a test that reads the far
  side back), `state`, `core`, `decisions`, `journal`, `finished`, `halted`,
  `stopped`, `liveness_violations` (the invariants of `vs_core.testing.liveness` the run broke,
  empty when it is live; a test asserts it is empty, nothing checks it behind the scenes),
  `commits`, `executed_effects`, `no_effect_ran_twice()`, `rerun_attempts`, `crash_points()`,
  `rerun_with_crash(point)`, `interruptions()` and `interrupt(point)`.
- `CrashPoint` (`CrashKind.BEFORE_COMMIT` or `AFTER_COMMIT` of commit k; it prints as
  `before commit 17`) and the one setting that chooses which of them a suite tries
  (`CrashPointPolicy`, `SAMPLED`, `EVERY_POINT`, `crash_point_policy()`, which follows `vs_sim`'s `--depth`).

- `Interruption` (an `Action`, `KILL`, `PAUSE` or `STOP`, at a `Moment`: `BEFORE_COMMIT`,
  `AFTER_COMMIT` or `IN_FLIGHT`; it prints as `kill in flight 5`), `Interrupted` and `Outcome`.
  See "Interruptions and the never-stuck property" below.

## Cheap crashes

A host that dies loses its memory, so what a crash leaves behind is the outside world: the
run.log, the effects ledger and the world's own position.
`simulate` runs the world once with no copying (it takes one copy of the initial world up front,
which also rejects a world that cannot be copied) and counts the log's records (its commits), so a test that never
crashes the run pays nothing extra. The first `rerun_with_crash` runs the world once more from
that pristine copy and copies the outside world (one `copy.deepcopy`, so objects that share a
reference keep sharing it) just before and just after each commit the policy chose; if that run
makes a different number of commits it raises `NondeterministicWorldError`. A point inside the run
that the policy did not choose gets a run of its own; a point outside it raises `ValueError`.
`rerun_with_crash(point)` then starts a new host (a new process over a new strategy instance) on
a copy of that and runs it to the end. The prefix is not run again, so a sweep costs the sum of the suffixes. Nothing from the dead host is
copied: its memory is gone by definition.

A recovered run reports the decisions of every host so far; its `journal` is the new host's.
It offers the crash points of its own hosts, so it can be crashed again (a second death during
recovery).

## Which crash points a suite tries

`SimulatedRun.crash_points()` follows one setting, not a choice per test: a sample of 12
commits per crash kind, evenly spaced and always including the first and the last, or every
commit. It is the session's one search depth (`vs_sim.api.testing.depth`), which the vs-sim pytest
plugin sets from `--depth=pr|nightly`, so every crash sweep of the repository follows it. Pull
requests sample; the nightly job passes `--depth=nightly`.

## Interruptions and the never-stuck property

A crash point is only a commit boundary. `SimulatedRun.interruptions()` offers every way a run
can be interrupted, under the same depth setting as `crash_points()` (a sample of 12 per class on
pull requests, every one under `--depth=nightly`):

| class | what happens |
| --- | --- |
| `kill before commit`, `kill after commit` | the host dies just before or after commit k reaches the store |
| `kill in flight` | the host dies as the k-th effect begins: it has begun and sealed nothing |
| `pause before commit`, `pause after commit`, `pause in flight` | the operator pauses the run at that moment; the host winds down and exits |
| `stop before commit`, `stop after commit`, `stop in flight` | the operator stops the run (a drain) at that moment; the host winds down and ends the run |

`interrupt(point)` then does what an operator does: Resume, a new host over what the old one left,
again if that host does not end the run, or ended it only because the operator stopped it, up to
`MAX_RESUMES` times. A Resume lifts the pause or the stop the log holds, when core takes it (a
run whose ending has begun takes none, and the host finishes the ending). A resumed host is waited on
for `max(300, 2 x the uninterrupted run's duration)` virtual seconds, so a run that waits on
something that is not coming is given up on instead of waited for. It returns an `Interrupted`
(never raises for a failure of the run itself) whose `outcome` is:

- `CONTINUED`: the run ended as the uninterrupted one did;
- `ENDED_WITH_REASON`: it ended another way, and its result names why;
- `STUCK`: it did not end after the resumes, or ended with recovery blocked;
- `ERRORED`: the host that was meant to carry on raised.

A kill starts from a copy of the outside world at its point, like `rerun_with_crash`. A pause or
a stop cannot: the host that is paused or stopped is alive and winds down, so the world runs again
from its start with the operator's control planted. A stop ends the run, so a run resumed after
one ends as the uninterrupted run did only when the search's adoption is one a Resume takes back
(a verified adoption; a failed one stays failed): assert `never_stuck()` and that nothing is left
open, not `matches_uninterrupted()`, for a stop. `cores` is core's state as each host left it,
so the host that was stopped can be held to its own end (`interrupt(point, then=...)` stops the
host that resumed the run again, with a pause or a stop; `interrupt_while_draining` kills the host
that winds down after either). `Interrupted.never_stuck()` is the property: the run continued or ended
with a named reason, and never ended stuck or errored. `every_step_finished_once()` says no
request's effect sealed a result more than once, and `reruns` counts the effects that began
again for a request whose effect had begun before (`no_effect_ran_twice()` is `reruns == 0`).
`history` says how each host after the interruption ended, and `detail` the error, reason, or
the request kinds the run is stuck on.

The effects ledger seals the result of each effect that finishes, keyed by the step's tag, as a
keyed write or a job's ledger entry is: `clear` adopts a sealed result, and an effect that began
and sealed nothing runs again.
