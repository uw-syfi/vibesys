# No flaky tests

A test must give the same result every run for the same code. A flaky test is
a defect in the test or the code. Fix the source of nondeterminism, or delete
the test and file an issue. Do not leave it in.

Never: add retries or reruns, skip on failure, lengthen a timeout or sleep "to
make it pass", mark it as an expected failure to hide it, or tolerate it
"because it usually passes".

## Sources and fixes

| Source | Fix |
| --- | --- |
| A timeout firing, or "finishes within N seconds" | Do not test by waiting. A Fake raises the timeout error immediately, and the test checks the handling. Never assert on elapsed time. |
| Sleeping or polling to wait for a thread, process, or event | Synchronize on the thing itself: an event, a channel or queue result, a join, an `await`. The code takes a clock or sleep port, and a Fake clock is advanced by the test. |
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

## Diagnosing a flake

1. Reproduce with repeated and parallel runs, or find the failing run's seed
   and ordering.
2. Name the nondeterminism source from the table.
3. Remove the source with an injected port, a Fake, or a synchronization point.
   Do not mask it with a sleep or retry.

A sleep that is the actual subject of an opt-in real-contract test needs a
`test-isolation: <reason>` comment. Where a language has a test-isolation
check, it counts sleeps in tests.
