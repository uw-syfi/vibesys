# Python test harness

How the `testing` rules map onto this repository's Python suite: pytest run
through `uv`, Hypothesis, and the `vs-sim` simulator. Numbers (example counts,
shard counts, timeouts, budgets) live in the files named here; read them
there.

## Mapping the rules

| Rule | Here |
| --- | --- |
| Public API | `<pkg>.api` for a `libs/` package (`tach.toml` enforces it); the module API siblings already import inside `vibesys`; `vibesys.api` at the top |
| No patching | The banned and allowed spellings are counted by `scripts/check_test_isolation.py` (see the `testing` skill's Python reference for the list) |
| Fakes | Exemplar: `vs_agent.api.testing.FakeAgentClient` |
| Exemption | `# test-isolation: <reason>` on the site's line or the line above |

## Contract suites

A production parameter that needs real services carries the `real_contract`
marker (registered in `pyproject.toml`), opt-in with `VIBESYS_REAL_CONTRACTS=1`.
CI does not set it; run it by hand when you change an interface, a production
implementation or its Fake, and report the result in the PR.

## Depth and Hypothesis profiles

The root `conftest.py` registers the Hypothesis profiles and their example
counts; nothing else restates them. All profiles set `deadline=None`, so never
set a deadline on a property.

- `--depth=pr|nightly` (owned by the vs-sim pytest plugin,
  `libs/vs-sim/pytest_plugin/vs_sim_pytest.py`) picks the profile: `pr`, the
  default, loads the derandomized `ci` profile; `nightly` loads `nightly`.
  `.github/workflows/nightly.yml` runs the nightly depth.
- `HYPOTHESIS_PROFILE` overrides by hand: `dev` is randomized at the PR count;
  `explore` is the larger randomized run for hunting new bugs.
- State a non-default count as `vs_sim.api.testing.examples(pr=..., nightly=...)`
  and a crash sweep as `vs_sim.api.testing.swept`. Never read an environment
  variable or define a per-file depth constant.
- Tests marked `slow` are deselected at PR depth, except one case of each under
  the `sim_smoke_slow` directories in `pyproject.toml`.

## Deterministic simulation (`vs-sim`)

`libs/vs-sim` owns time, scheduling, seeds, signals, child processes and
blocking calls: `vs_sim.api` holds the interfaces product code takes, and
`vs_sim.api.testing` the simulator, Fakes, waits and contract suites. Its
pytest plugin, registered by the root `conftest.py`, makes the simulator the
default:

- An `async def` test outside the `sim_real_tiers` directories that has no
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

`tests/quality/test_real_apis_confined.py` fails a deterministic-tier test, or
product code outside `libs/vs-sim`, that uses a real API, against the
exact-count baseline `tests/quality/real_api_baseline.jsonl`. Rewrite a flagged
use as follows:

| Flagged use | Rewrite as |
| --- | --- |
| `time.sleep`, `asyncio.sleep(n)`, `time.monotonic` | An injected `vs_sim.api.Clock` or `vs_sim.api.Sleeper`; run on the virtual loop (`sim`, or an unmarked `async def` test) and advance `vs_sim.api.testing.VirtualClock` |
| `threading.Thread`, `Event`, `Lock` | `vs_sim.api.testing.Gate`, `start_thread` and `join_or_fail`, or a Fake `vs_sim.api.BlockingRunner` |
| `subprocess`, `asyncio.create_subprocess_*` | A Fake `vs_sim.api.ProcessLauncher` or the owning library's Fake command runner |
| `socket` | The owning library's in-memory Fake transport; a real socket is a real-tier test |
| `signal.signal`, `os.kill(os.getpid(), ...)` | A Fake `vs_sim.api.SignalSource`; real signal state changes go through `run_in_child` |
| bare `.wait()`, `.join()`, `.get()` | `wait_or_fail`, `join_or_fail`, `get_or_fail`, `vs_sim.api.testing.arrival`, or a `timeout=` |
| A test of a real system | Move it to a `sim_real_tiers` directory |

A wait on something the test does not control carries a hang guard from
`vs_sim.api.testing` (`join_or_fail`, `wait_or_fail`, `get_or_fail`,
`HANG_GUARD_S`) or a `timeout=` argument;
`tests/quality/test_no_unbounded_blocking_waits.py` enforces it. The suite-wide
per-test `timeout` in `pyproject.toml` is only a backstop.

## Fault injection

`libs/vs-faults` (import `vs_faults.api`) owns the one fault mechanism: a
`vs_faults.api.FaultPlan` (a seed plus rules naming a boundary, a target, the
ordinal of the matching call, and the fault) drives
`vs_agent.api.testing.FaultyAgentClient` (any agent client),
`vs_faults.api.FaultyToolDispatch` (a tool dispatcher keyed by tool name), and
`python -m vs_faults.connector PLAN STATE -- INNER...` (any Slurm connector
command). `vs_agent.api.testing.generated_replies` answers a Fake agent client
from each turn's declared schema. The chaos sweep
(`tests/system/chaos/test_chaos.py`, `scripts/chaos_loop.sh N`) shows how to
compose them.

The untrusted-input corpus every JSON decoder runs against is
`vs_sim.api.testing.hostile_json_frames`.

## Golden fixtures

Prompt goldens compare through `tests/vibesys/golden/helpers.py`. Regenerate
with `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest <file>`, then read the diff.

## Isolation and timeout verdicts

`scripts/check_test_isolation.py` counts patching, mocking, sleeps
(`time.sleep`, `asyncio.sleep` other than `asyncio.sleep(0)`), timeout
verdicts, and non-`api` imports of a library inside its own tests, against
`tests/quality/isolation_baseline.jsonl`; `--write` only lowers counts.

A timeout verdict is a timeout or clock reading that decides the outcome
rather than only failing a hung test: `assert not ev.wait(timeout=T)`,
`return ev.wait(timeout=T)`, a timed wait inside `pytest.raises(TimeoutError)`,
and a clock reading compared inside an `assert`, `while`, or `if`.
`assert ev.wait(timeout=T)` is a deadlock guard and does not count. The
script's docstring states the criterion and what it deliberately misses.

## Sharding and parallelism

CI runs with `-n auto --dist loadgroup`; the `serial` marker is a last resort
for a host-wide resource. The Python test job splits the suite over its
`matrix.shard` in `.github/workflows/test.yml` (`--shard=I/N` or
`VIBESYS_TEST_SHARD=I/N`), balanced by recorded seconds per file
(`tests/support/sharding.py`). A test must not depend on running beside
another test of its file. Every green run on main caches its measured
seconds; `tests/support/shard_durations.json` is only the cold start,
refreshed with `python -m scripts.refresh_shard_durations`.

## Commands

```bash
uv run pytest path/to/test.py -k name              # narrowest first
uv run pytest path/to/test.py -n auto --no-cov -q  # parallel
uv run python scripts/check_test_isolation.py      # ratchet
uv run pytest libs tests/system tests/vibesys --depth=nightly -n 16 --no-cov
```
