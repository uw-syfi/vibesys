# No flaky tests

A test must give the same result every run for the same code. A flaky test is
a defect in the test or the code. Fix the source of nondeterminism, or delete
the test and file an issue. Do not leave it in.

Never: add retries or reruns, skip on failure, lengthen a timeout or sleep "to
make it pass", mark it as an expected failure to hide it, or tolerate it
"because it usually passes".

A test's result must never depend on timing, and every wait needs a bound that
only guards against hangs. In the deterministic tiers this is enforced, not
advised: `tests/quality/test_real_apis_confined.py` fails a test that uses
`time`, `threading`, `subprocess`, `socket`, `signal` (or a process launch, executor,
network, file-lock or wall-clock spelling of the same), a nonzero
`asyncio.sleep`, a bare wait, join or get, or a signal to its own process
(`vs_sim` interfaces, the virtual loop and Fakes cover each). Rewrite a flagged
test as follows.

| Flagged use | Rewrite as |
| --- | --- |
| `time.sleep`, `asyncio.sleep(n)`, `time.monotonic` | An injected `vs_sim.api.Clock`/`Sleeper`; run on the virtual loop (`sim`, or an unmarked `async def` test) and advance `VirtualClock`. |
| `threading.Thread`, `Event`, `Lock` | A `Gate`, `start_thread` and `join_or_fail`, or run the code through a Fake `BlockingRunner`. |
| `subprocess`, `asyncio.create_subprocess_*` | A Fake `ProcessLauncher` or command runner from the owning library. |
| `socket` | The library's in-memory Fake transport; a real socket is a real-tier test. |
| `signal.signal`, `os.kill(os.getpid(), ...)` | A Fake `SignalSource`; anything that must change real signal state goes through `run_in_child`. |
| bare `.wait()`, `.join()`, `.get()` | `wait_or_fail`, `join_or_fail`, `get_or_fail`, `arrival(awaitable, task)` or a `timeout=`. |
| A test of a real system | Move it to a real tier (`sim_real_tiers`). |

The baseline of existing uses has exact counts and only shrinks.

The suite has a hard per-test bound (`timeout` in `pyproject.toml`, 300 s) so a
hung test fails alone instead of stalling its shard. It is a backstop, not a
synchronization tool: a test must pass without ever reaching it.

A wait on a thread, process or socket the test does not control carries a hang
guard from `vs_sim.api.testing` (`join_or_fail`, `wait_or_fail`,
`get_or_fail`, `accept_or_fail`, `stop_process`, `HANG_GUARD_S`) or a `timeout=` argument, so a stuck peer fails that test with a
message. The guard sits far above any passing run and is never what a test
synchronizes on. `tests/quality/test_no_unbounded_blocking_waits.py` enforces it.

## Sources and fixes

Clock and async injection below apply to the shell and I/O implementations.
[Pure core tests](properties-and-goldens.md#functional-core-event-sequences)
supply time as event data and use no clocks or Fakes.

| Source | Fix |
| --- | --- |
| A timeout firing, or "finishes within N seconds" | Do not test by waiting. A Fake raises the timeout error immediately, and the test checks the handling. Never assert on elapsed time. |
| Sleeping or polling to wait for a thread, process, or event | Synchronize on the thing itself: an event, a channel or queue result, a join, an `await`. The code takes a clock or sleep interface, and a Fake clock is advanced by the test. |
| Wall-clock values (current time, dates) | Inject a clock. Compare against the injected value. |
| Thread or async interleaving | Do not assert on an order the code does not guarantee. Assert on an order-independent property (a set, a sorted list), or drive a single-threaded Fake executor. |
| Randomness | Inject a seeded RNG. Property-based libraries manage their own; see [properties-and-goldens.md](properties-and-goldens.md). |
| Real network, ports, services | Use a Fake. Real dependencies belong only in opt-in real-contract or end-to-end tests. |
| Shared state: working directory, environment, global singletons, static fields, files outside a per-test temp directory | Own the state per test. Tests must pass in any order and in parallel. Serializing tests is a last resort for a truly host-wide resource. |
| Map, set, or filesystem iteration order, object identity, temp paths in output | Sort or normalize before comparing. |
| Leaked threads, subprocesses, or files from an earlier test | The component that creates a resource owns its cleanup. Use scoped-cleanup constructs or fixtures. |

## Verify before handing back

If a test involves threads, processes, subprocesses, async work, or time, run
it 20 times in a row and once in parallel with other tests. The commands are in
the per-language reference.

## Zero tolerance: every flake gets fixed

Seeing a flake, even in a test you did not write or in a CI rerun that "passed
the second time", obliges you to fix it. Rerunning the job is not a fix.

Put the fix in its own PR, not in the change you were working on, so it lands
fast and unblocks everyone. Then rebase your current PR on top of it (or, if
the fix has already merged, on the updated main). Do not carry the fix inside
an unrelated PR, and do not wait on your PR to ship it. Root-cause it, then redesign the test so the
source of nondeterminism is gone, not merely less likely. Leave the test and
its neighbors better than you found them: sweep for the same pattern elsewhere
and cover it with a property test where practical.

Techniques that remove flakiness by construction: injected clock, sleeper, and
RNG; Fakes and fault injection instead of real timeouts, networks, and
processes; deterministic simulation (single-threaded Fake executor, virtual
time); seeded property-based tests with a recorded seed; synchronizing on
events, channels, or joins instead of sleeping or polling; order-independent
assertions (sets, sorted lists); per-test temp directories, ports, and
environment; scoped fixtures that own cleanup; pure functional-core tests over
event sequences, with no clocks at all.

## Diagnosing a flake

1. Reproduce with repeated and parallel runs, or find the failing run's seed
   and ordering. Under CI load, also try `-n auto` and shuffled order.
2. Name the nondeterminism source from the table.
3. Remove the source with an injected interface, a Fake, or a synchronization
   point. Do not mask it with a sleep or retry.
4. Prove it: the repeated and parallel runs from the previous section pass,
   and the failure reproduces deterministically on the old code (a pinned
   seed or ordering, or the bad condition forced through the test's seams)
   and does not on the new code.
5. Decide where the root cause lives. If the flake exposed a product defect
   (a race, a missed drain, a lost update), add a regression test of that
   product behavior, as for any bug fix. If the defect is in the test itself
   (it read the wrong stream, polled a clock it then advanced, shared state
   with a neighbor, depended on what the runner has installed), fix the test
   and stop: do not add a test that tests the test. Put the deterministic
   reproduction from step 4 in the PR description instead.

A sleep that is the actual subject of an opt-in real-contract test needs a
`test-isolation: <reason>` comment. Where a language has a test-isolation
check, it counts sleeps in tests.
