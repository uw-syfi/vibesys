# vs-sim

One leaf library (standard library only) that owns what makes a test depend on
timing: the clock, sleeping, seeds, signals, child processes and blocking calls.

- `vs_sim.api` holds the role-named interfaces product code takes
  (`Clock`, `Sleeper`, `SleepingClock`, `BlockingRunner`, `SignalSource`,
  `ProcessLauncher`, `RandomSource`, `Threads`, `Network`) and their real implementations
  (`SystemClock`, `MonotonicClock`, `ThreadBlockingRunner`, `LoopSignalSource`,
  `SubprocessLauncher`, `SeededRandom`, `SystemRandomSource`, `OsThreads`, `UnixNetwork`).
  `Threads` is the one interface for everything that blocks an operating-system thread:
  `spawn`, locks, reentrant locks, conditions, events, synchronous `sleep` and a monotonic
  `now`. `Network` carries bytes over local stream connections (`listen`, `accept`,
  `connect`, `send`, `recv`).
- `vs_sim.api.testing` holds the test side: the deterministic scheduler
  (`VirtualClock`, `run_virtual`), `ManualClock`, an event trace for comparing two
  runs, `Gate` and `arrival` (waits tied to the lifetime of what they wait on),
  hang-guarded joins, waits, gets and accepts, `wait_for_state`, `run_in_child`,
  crash and restart building blocks, Fakes of each interface (including
  `GatedBlockingRunner`, which keeps a blocking call in flight until released), and a contract suite
  every implementation of `Clock`, `BlockingRunner`, `SignalSource`, `ProcessLauncher`,
  `Threads` and `Network` passes.
- `SimThreads` simulates `Threads` cooperatively: every simulated thread is a real thread
  but only one runs at a time, and under a schedule seed the next one is drawn from the
  seed at every synchronization point (one seed, one interleaving). It shares the
  `VirtualClock`: time jumps only when no thread can run, and a run in which nothing can
  ever run fails with `SimDeadlockError` instead of hanging. Pass it to
  `run_virtual(..., driver=sim_threads)` and it runs whenever the loop is idle, so
  `SimBlockingRunner` turns a blocking call from a coroutine into a simulated thread.
  `SimNetwork` is the in-memory `Network` built on any `Threads`. Plain Python between two
  synchronization points is atomic, so this finds ordering bugs around locks, conditions,
  events and timers, not unsynchronized data races.

Nothing here reads a wall clock to decide a test outcome. The virtual clock jumps
only when every task waits, and refuses to idle (`VirtualDeadlockError`) instead of
hanging. Product code reaches time, threads, signals and processes only through the
interfaces, so a test can run it on the simulator.

## The pytest plugin

`pytest_plugin/vs_sim_pytest.py` (outside `src`, because it imports pytest) is loaded by
the repository's root `conftest.py`. It runs unmarked async tests on the virtual clock,
provides the `sim` fixture (`vs_sim.api.testing.Sim`), prints the seed of a failing sim
test (`--sim-seed=N` replays it) and, with `--sim-determinism-check`, runs each sim test
twice and compares their event traces. `--sim-explore=N` runs each selected sim test under
N different seeds, each also breaking scheduling ties (ready callbacks, equal-time timers)
in its own seeded order via `run_virtual(schedule_seed=...)`; a failure prints
`--sim-seed=S --sim-schedule-seed=S` to replay it. The pull-request job `seed-exploration`
uses it on the tests a PR adds or changes (`scripts/explore_changed_tests.py`). Domain fakes register with
`vs_sim.api.testing.WORLDS` and are built per test by `sim.world(name)`.
