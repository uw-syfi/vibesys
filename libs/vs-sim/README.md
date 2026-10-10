# vs-sim

One leaf library (standard library only) that owns what makes a test depend on
timing: the clock, sleeping, seeds, signals, child processes and blocking calls.

- `vs_sim.api` holds the role-named interfaces product code takes
  (`Clock`, `Sleeper`, `SleepingClock`, `BlockingRunner`, `SignalSource`,
  `ProcessLauncher`, `RandomSource`) and their real implementations
  (`SystemClock`, `MonotonicClock`, `ThreadBlockingRunner`, `LoopSignalSource`,
  `SubprocessLauncher`, `SeededRandom`, `SystemRandomSource`).
- `vs_sim.api.testing` holds the test side: the deterministic scheduler
  (`VirtualClock`, `run_virtual`), `ManualClock`, an event trace for comparing two
  runs, `Gate` and `arrival` (waits tied to the lifetime of what they wait on),
  hang-guarded joins, waits, gets and accepts, `wait_for_state`, `run_in_child`,
  crash and restart building blocks, Fakes of each interface, and a contract suite
  every implementation of `Clock`, `BlockingRunner`, `SignalSource` and
  `ProcessLauncher` passes.

Nothing here reads a wall clock to decide a test outcome. The virtual clock jumps
only when every task waits, and refuses to idle (`VirtualDeadlockError`) instead of
hanging. Product code reaches time, threads, signals and processes only through the
interfaces, so a test can run it on the simulator.

## The pytest plugin

`pytest_plugin/vs_sim_pytest.py` (outside `src`, because it imports pytest) is loaded by
the repository's root `conftest.py`. It runs unmarked async tests on the virtual clock,
provides the `sim` fixture (`vs_sim.api.testing.Sim`), prints the seed of a failing sim
test (`--sim-seed=N` replays it) and, with `--sim-determinism-check`, runs each sim test
twice and compares their event traces. Domain fakes register with
`vs_sim.api.testing.WORLDS` and are built per test by `sim.world(name)`.
