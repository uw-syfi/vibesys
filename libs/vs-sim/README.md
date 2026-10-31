# vs-sim

One leaf library (standard library only) that owns what makes a test depend on
timing: the clock, sleeping, seeds, signals, child processes and blocking calls.

- `vs_sim.api` holds the role-named interfaces product code takes
  (`Clock`, `Sleeper`, `SleepingClock`, `BlockingRunner`, `SignalSource`,
  `ProcessLauncher`, `Processes`, `StreamingCommand`, `OutputSink`, `ByteSource`,
  `RandomSource`, `Threads`, `ReservableThreads`, `Network`)
  and their real implementations (`SystemClock`, `MonotonicClock`, `ThreadBlockingRunner`,
  `LoopSignalSource`, `SubprocessLauncher`, `OsProcesses`, `SubprocessStreamingCommand`,
  `LocalFileSource`, `SeededRandom`, `SystemRandomSource`, `OsThreads`, `UnixNetwork`).
  `Threads` is the one interface for everything that blocks an operating-system thread:
  `spawn`, locks, reentrant locks, conditions, events, thread-local values, synchronous
  `sleep` and a monotonic `now`. `ReservableThreads` is the narrower extension for a
  transaction that must own a `WorkerReservation` before an interruption-prone start;
  aborting code settles that reservation before releasing its other resources. Settlement
  is idempotent and terminal even when start was never attempted. `Processes`
  runs a command to completion in a process group
  of its own; its result says whether the command exited, timed out and was cleaned up, or
  timed out and left something running. `Platform` bundles them for the few components that
  use more than three; `Platform.real()` is built at an entrypoint and `Sim.platform` in a
  test. `Network` carries bytes over local stream connections (`listen`, `accept`,
  `connect`, `send`, `recv`).
- `vs_sim.api.testing` holds the test side: the deterministic scheduler
  (`VirtualClock`, `run_virtual`), `ManualClock`, an event trace for comparing two
  runs, `Gate` and `arrival` (waits tied to the lifetime of what they wait on),
  hang-guarded joins, waits, gets and accepts, `wait_for_state`, `run_in_child`,
  crash and restart building blocks, `CrashableClock` (a virtual clock that kills the simulated
  host at its next call) and `ProbedClock` (records the waits made), `clock_from`, `pairwise_rows`,
  `file_size_limit`, Fakes of each interface (including
  `GatedBlockingRunner`, which keeps a blocking call in flight until released), and a contract suite
  every implementation of `Clock`, `BlockingRunner`, `SignalSource`, `ProcessLauncher`,
  `Processes`, `StreamingCommand` (`ScriptedStreamingCommand` is its Fake; the real one runs it in
  `tests/e2e`), `OutputSink`, `ByteSource` (`FakeByteSource` is its Fake), `Threads` and
  `Network` passes.
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

## Waiting for a child's exit

A process group's owner waits for its leader to exit without reaping it, because reaping
frees the group ID for reuse and a signal sent after it could reach an unrelated group.
`ExitWaiter` is that wait, and `host_exit_waiter()` picks the mechanism by capability:
`WaitidExitWaiter` where `os` provides `waitid` (Linux), `KqueueExitWaiter` where `select`
provides a kqueue `NOTE_EXIT` watch (macOS), and a waiter that refuses with
`ExitWaitUnsupportedError` otherwise. `SubprocessPipedProcessLauncher(exit_waiter=...)`
takes another, and a watch that fails raises from the wait instead of reading as an exit.
`ExitWaiterContract` (in `vs_sim.api.testing`) is the suite every mechanism passes.

## Command output

A command's output goes through one write interface. Every runner (`Processes.run`,
`StreamingCommand.run`) takes `output: OutputSink` and writes each chunk of stdout and
stderr to it as it reads it; its result is the same whatever the sink is. Producers only
write: they never know where the bytes are kept, chunked or shown. `DISCARD` is the
explicit opt-out (today the default, until every caller passes a sink), `tee` sends one
stream to several sinks, `Capture` keeps a bounded copy and counts what it dropped (the
runners' own results use it), and `Lines` splits each stream into decoded lines for a
parser such as the image build's step tracker. Writes for the two streams may arrive
concurrently; each call's bytes stay contiguous.

The read side is `ByteSource` (`size`, `read(offset, max_bytes)`): append-only bytes that
may grow, wherever they live. `LocalFileSource` reads a file on this host; sources for a
container's or a cluster's files belong to the libraries that own those transports, and
report a shrink or a replacement through `SourceWatch` the same way.

Process output is not read back for facts: a fact a caller needs is in the runner's typed
result or an event, and the sink is for people following the command.

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
`--sim-seed=S --sim-schedule-seed=S` to replay it. Exploration runs in rounds (every test
once, then every test again), so `--sim-explore-budget=SECONDS` or an interrupt that ends it
early leaves the tests explored about equally; `--sim-explore-salt=TEXT` varies the seeds
and the test each round starts from. The pull-request job `seed-exploration` uses it, under
a hard wall-clock cap, on the tests a PR adds or changes (`scripts/explore_changed_tests.py`). Domain fakes register with
`vs_sim.api.testing.WORLDS` and are built per test by `sim.world(name)`.
