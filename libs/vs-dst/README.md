# vs-dst

Test support: simulation tests of the run lifecycle that are short to write and cheap to run.
It sits above `vs-sim`, which knows time, seeds and scheduling but nothing about a run, and
adds what a run is made of: the store, commits, executors and their effects.

A test builds a world, simulates it and asserts on the result:

```python
def test_a_restart_after_any_crash_finishes_the_same_run():
    world = SomeStrategyWorld(...)
    normal = simulate(world)

    for crash in normal.crash_points():           # sampled by default; all with --dst-crash-points=all
        recovered = normal.rerun_with_crash(crash)  # starts from normal's saved state, no prefix rerun
        assert recovered.result == normal.result, crash
        assert recovered.no_effect_ran_twice(), crash
```

`vs_dst.api` holds:

- `World` (a `typing.Protocol`): what a test builds. `setup` says which strategy runs
  (`RunSetup`: a fresh strategy factory, the `vs_core` `Harness` and the reply schemas) and
  `__call__(request, core)` answers every request the executors receive. A world also carries
  its position (the scripted replies it has handed out), so it must be plain data that
  `copy.deepcopy` can copy; `setup` is immutable and is shared, never copied. A world that cannot be
  copied fails `simulate` with `WorldNotCopyableError`, naming its type.
- `simulate(world, *, max_concurrent=1, controlled=False) -> SimulatedRun`: runs the whole
  run on the production shell (`vs_runtime`'s `CoreRuntime` and loop, a virtual clock, an
  in-memory store, a publication delivery, and executors that seal what each effect did).
- `SimulatedRun`: `result`, `state`, `core`, `decisions`, `journal`, `finished`, `halted`,
  `stopped`, `commits`, `executed_effects`, `no_effect_ran_twice()`, `crash_points()` and
  `rerun_with_crash(point)`.
- `CrashPoint` (`CrashKind.BEFORE_COMMIT` or `AFTER_COMMIT` of commit k; it prints as
  `before commit 17`) and the one setting that chooses which of them a suite tries
  (`CrashPointPolicy`, `SAMPLED`, `EVERY_POINT`, `set_crash_point_policy`).

## Cheap crashes

A host that dies loses its memory, so what a crash leaves behind is the outside world: the
store, the effects ledger, the publication delivery and the world's own position.
While `simulate` runs, it copies exactly those (one `copy.deepcopy`, so objects that share a
reference keep sharing it) just before and just after every commit. `rerun_with_crash(point)`
starts a new host (a new `CoreRuntime` over a new strategy instance) on a copy of that, after the
dead host's lease has expired, and runs it to the end. The prefix of the run is not run again,
so a sweep costs the sum of the suffixes instead of a whole run per crash point.
Nothing from the dead host is copied: its memory is gone by definition.

A recovered run reports the decisions of both hosts; its `journal` is the new host's.

## Which crash points a suite tries

`SimulatedRun.crash_points()` follows one setting, not a choice per test: a sample of 12
commits per crash kind, evenly spaced and always including the first and the last, or every
commit. The pytest plugin in `pytest_plugin/vs_dst_pytest.py` (outside `src`, because it imports
pytest, and loaded by the root `conftest.py`) sets it from
`--dst-crash-points=sampled|all`. Pull requests sample; the nightly job passes `all`.
