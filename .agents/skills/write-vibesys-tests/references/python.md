# Python test harness

How the `testing` rules map onto this repository's Python suite: pytest run
through `uv`, Hypothesis, and the `vs-sim` simulator. Numbers (example counts,
shard counts, timeouts, budgets) live in the configuration that owns them; read
them there.

## Mapping the rules

| Rule | Here |
| --- | --- |
| Public API | `<pkg>.api` for a library (tach enforces it); the module API siblings already import inside `vibesys`; `vibesys.api` at the top |
| No patching | `check_test_isolation` counts the banned and allowed spellings (see the `testing` skill's Python reference for the list) |
| Fakes | Exemplar: the `FakeAgentClient` in `vs_agent.api.testing` |
| Exemption | `# test-isolation: <reason>` on the site's line or the line above |

## Contract suites

A production parameter that needs real services carries the `real_contract`
marker (registered in the pytest configuration), opt-in with
`VIBESYS_REAL_CONTRACTS=1`. CI does not set it; run it by hand when you change
an interface, a production implementation or its Fake, and report the result
in the PR.

## Depth and Hypothesis profiles

The root `conftest.py` registers the Hypothesis profiles and their example
counts; nothing else restates them. All profiles set `deadline=None`, so never
set a deadline on a property.

- `--depth=pr|nightly` (owned by the vs-sim pytest plugin) picks the profile:
  `pr`, the default, loads the derandomized `ci` profile; `nightly` loads
  `nightly`. The nightly CI workflow runs the nightly depth.
- `HYPOTHESIS_PROFILE` overrides by hand: `dev` is randomized at the PR count;
  `explore` is the larger randomized run for hunting new bugs.
- State a non-default count as `vs_sim.api.testing.examples(pr=..., nightly=...)`
  and a crash sweep as `vs_sim.api.testing.swept`. Never read an environment
  variable or define a per-file depth constant.
- Tests marked `slow` are deselected at PR depth, except one case of each under
  the smoke-slow directories the pytest configuration lists.

## Deterministic simulation (`vs-sim`)

The `vs-sim` library owns time, scheduling, seeds, signals, child processes and
blocking calls: `vs_sim.api` holds the interfaces product code takes, and
`vs_sim.api.testing` the simulator, Fakes, waits and contract suites. Its
pytest plugin, registered by the root `conftest.py`, makes the simulator the
default:

- An `async def` test outside the real-tier directories that has no
  `pytest.mark.asyncio` runs on a virtual clock: sleeping costs no wall time,
  and a test that waits on nothing raises `VirtualDeadlockError` at once. A
  test marked `asyncio` stays on pytest-asyncio; do not combine that marker
  with the `sim` fixture. Async fixtures are not supported on the virtual loop.
- The `sim` fixture gives `sim.clock`, `sim.seed`, `sim.random(label)`,
  `sim.gate()`, `sim.run(coro)` (for sync tests), `sim.run_in_child(fn)` and
  `sim.world(name)` (domain fakes registered in a conftest with
  `vs_sim.api.testing.WORLDS`).
- A failing sim test prints its seed; `--sim-seed=N` replays it exactly.
  `--sim-determinism-check` runs each sim test twice with one seed and fails if
  the event traces differ.

Ruff's `TID251` rejects real time (`time`, `datetime.now`/`utcnow`/`today`,
`date.today`, `asyncio.sleep`) outside `vs-sim` and the real tiers.
`test_deterministic_tests_use_no_real_time_threads_processes_or_sockets` and
`test_product_code_reaches_real_apis_only_through_vs_sim` fail a
deterministic-tier test, or product code outside `vs-sim`, that uses real
threads, processes, sockets or signals, against an exact-count baseline that
only shrinks. Rewrite a flagged use
as follows:

| Flagged use | Rewrite as |
| --- | --- |
| `time.sleep`, `asyncio.sleep(n)`, `time.monotonic` | An injected `vs_sim.api.Clock` or `vs_sim.api.Sleeper`; run on the virtual loop (`sim`, or an unmarked `async def` test) and advance `vs_sim.api.testing.VirtualClock` |
| `threading.Thread`, `Event`, `Lock` | `vs_sim.api.testing.Gate`, `start_thread` and `join_or_fail`, or a Fake `vs_sim.api.BlockingRunner` |
| `subprocess`, `asyncio.create_subprocess_*` | A Fake `vs_sim.api.ProcessLauncher` or the owning library's Fake command runner |
| `socket` | The owning library's in-memory Fake transport; a real socket is a real-tier test |
| `signal.signal`, `os.kill(os.getpid(), ...)` | A Fake `vs_sim.api.SignalSource`; real signal state changes go through `run_in_child` |
| bare `.wait()`, `.join()`, `.get()` | `wait_or_fail`, `join_or_fail`, `get_or_fail`, `vs_sim.api.testing.arrival`, or a `timeout=` |
| A test of a real system | Move it to a real-tier directory |

A wait on something the test does not control carries a hang guard from
`vs_sim.api.testing` (`join_or_fail`, `wait_or_fail`, `get_or_fail`,
`HANG_GUARD_S`) or a `timeout=` argument;
`test_no_test_blocks_without_a_bound_on_a_thread_process_or_socket` enforces
it. The suite-wide per-test `timeout` in the pytest configuration is only a
backstop.

## Fault injection

The `vs-faults` library (import `vs_faults.api`) owns the one fault mechanism:
a `vs_faults.api.FaultPlan` (a seed plus rules naming a boundary, a target, the
ordinal of the matching call, and the fault) drives
`vs_agent.api.testing.FaultyAgentClient` (any agent client),
`vs_faults.api.FaultyToolDispatch` (a tool dispatcher keyed by tool name), and
the fault-injecting Slurm connector wrapper, which wraps any Slurm connector
command. `vs_agent.api.testing.generated_replies` answers a Fake agent client
from each turn's declared schema. The runtime library's cluster-fault test shows a
generated property over the production Slurm executor.

The untrusted-input corpus every JSON decoder runs against is
`vs_sim.api.testing.hostile_json_frames`.

## Golden fixtures

Prompt and text goldens live beside each covering test. Byte-exact ones
compare through the shared golden helper, which rewrites the fixture instead
of comparing when `UPDATE_PROMPT_SNAPSHOTS=1` is set; the domain packs pin a
SHA-256 per rendered role instead. Run the test without the variable first and
read the diff, then regenerate, then rerun without it. The full table of
goldens is in the `write-agent-prompt` skill.

An old directory of prompt snapshots is read by no test, so regenerating it
checks nothing; do not update it.

The native replays of the run-fixtures library and the desktop-shell scenarios
are not goldens, and nothing of them is committed. They are the shipping
projector applied to a declaration, so `tests.support.derived_replays` stores
them under a hash of everything they can derive from: every file under the
roots, and the files, of the `derived_replays` component in the repoctl policy
(whole source packages, the lock and project files, the generators), and the
Python version. The key is coarse on purpose: regenerating takes seconds, so it
errs wide instead of listing what matters, and no trace of what ran can go
stale. CI selects the client jobs on a change to the same component, so a
projector change still runs the client tests that read the replays. An entry
written with `selects = false` keys the set without selecting a job; use it only
for a library the projection merely imports, whose effect on a replay the
projection snapshots and the conversion golden already show. Widen the
component, never the key alone. The `with_derived_replays` wrapper in the
clients tools makes the set before a client test run (`pnpm test` in
`run-page`, `pnpm e2e` and `e2e:linux` in the desktop shell) and needs `uv` and
a synced Python environment, which a TypeScript-only checkout did not need
before. A `.proto` or projector change names a new set, so none is stale and no
two pull requests conflict. A client test reads one through
`@vibesys/run-fixtures/derived`, which fails naming the script when it was
skipped; a Python test projects the declaration itself with
`shared_scenarios.project_shared` or `journey_fixtures.project_scenario`.
Generation is never done inside a test.

Because a projector change no longer shows as a changed file, a text snapshot
of each scenario (record kinds and span label kinds, with counts) is committed
under the run tests and rewritten by the regeneration script. It changes only
when what a scenario projects to changes, so two pull requests conflict on it
only for a real overlap.

Three sets of committed binary goldens remain, each because it pins a contract or
is read where Python cannot make it: the conversion goldens
(which the gateway and TypeScript tests read), the
gateway's Skeleton wire golden with its retained RPC replies, and the frozen
retained corpus. After a converter or gateway change run
`./scripts/regenerate_goldens.sh` and commit every file it rewrites; it is the
only regeneration command, and each stale-golden failure names it. It does not
touch the UI gallery screenshot baselines, which need Docker and depend on the
UI, not the schema (`update:screenshots`, in
[clients-architecture.md](../../../../docs/contributing/clients-architecture.md)).
These carry no hash of their inputs, because a byte comparison already fails a
stale one: `test_gateway_conversion_fixture_regenerates_identically` runs in the
proto check group, and the gateway tests compare the wire golden and the replies;
a change to any committed golden selects both jobs. A new committed golden joins
`tests.support.golden_regeneration` and the script, not a second command.

## Isolation and timeout verdicts

`check_test_isolation` counts patching, mocking, sleeps (`time.sleep`,
`asyncio.sleep` other than `asyncio.sleep(0)`), timeout verdicts, and non-`api`
imports of a library inside its own tests, against a recorded baseline; its
`--write` option only lowers counts.

A timeout verdict is a timeout or clock reading that decides the outcome
rather than only failing a hung test: `assert not ev.wait(timeout=T)`,
`return ev.wait(timeout=T)`, a timed wait inside `pytest.raises(TimeoutError)`,
and a clock reading compared inside an `assert`, `while`, or `if`.
`assert ev.wait(timeout=T)` is a deadlock guard and does not count. The
script's docstring states the criterion and what it deliberately misses.

## Sharding and parallelism

CI runs with `-n auto --dist loadgroup`; the `serial` marker is a last resort
for a host-wide resource. The Python test job splits the suite over the CI
workflow's shard matrix (`--shard=I/N` or `VIBESYS_TEST_SHARD=I/N`), balanced
by recorded seconds per file. A test must not depend on running beside another
test of its file. Every green run on main averages its measured seconds into the cached record; a
checked-in duration table is only the cold start, refreshed with
`python -m scripts.refresh_shard_durations`.

## Commands

```bash
uv run pytest <test file> -k <name>               # narrowest first
uv run pytest <test file> -n auto --no-cov -q     # parallel
uv run python -m scripts.check_test_isolation     # ratchet
uv run pytest --depth=nightly -n 16 --no-cov       # the nightly depth, all tiers
```
