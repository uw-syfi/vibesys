# No flaky tests

A test must give the same result every run for the same code. A flaky test is
a defect in the test or the code. Fix the source of nondeterminism, or delete
the test and file an issue. Do not leave it in.

Never: add retries or reruns, skip on failure, lengthen a timeout or sleep "to
make it pass", mark it as an expected failure to hide it, or tolerate it
"because it usually passes".

A test's result must never depend on timing, and every wait needs a bound that
only guards against hangs. In the deterministic tiers, enforce this with a
check rather than advice: fail a test that uses real time, threads,
processes, sockets or signals (under any spelling, including process
launches, executors, file locks and wall-clock reads), a nonzero sleep, an
unbounded wait, join or get, or a signal to its own process. Each has a
deterministic replacement:

| Real use | Rewrite as |
| --- | --- |
| Sleep, monotonic or wall-clock reads | An injected clock and sleeper; run on a virtual event loop and advance the virtual clock. |
| Threads, events, locks | A gate the test opens, a joined thread with a hang guard, or a Fake blocking runner. |
| Subprocesses | A Fake process launcher or command runner from the owning library. |
| Sockets | The owning library's in-memory Fake transport; a real socket belongs in a real tier. |
| Installing signal handlers, signalling yourself | A Fake signal source; code that must change real signal state runs in a child the simulator manages. |
| Bare wait, join, get | A bounded wait that fails the test on timeout, or an explicit `timeout=`. |
| A test of a real system | A real tier. |

Give the suite a hard per-test bound so a hung test fails alone instead of
stalling its run. It is a backstop, not a synchronization tool: a test must
pass without ever reaching it.

A wait on a thread, process or socket the test does not control carries a hang
guard (a bounded wait helper or a `timeout=` argument), so a stuck peer fails
that test with a message. The guard sits far above any passing run and is
never what a test synchronizes on.

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
| A browser locator that names a row by position (`first()`, `nth()`) in a list the page windows or reorders, kept across an action | A locator re-resolves on every assertion, so after the action it can name another row: a scroll moves the window. Name the row by its text. |
| Page state that depends on how many times the page drew between two inputs (a block kept open because it was drawn while it was being written) | Send the input, then wait for the state that shows the page drew it (the live marker) before the next input; never rely on the inputs' speed. |
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
