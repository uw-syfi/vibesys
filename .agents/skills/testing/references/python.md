# Python

Runner: pytest, run through `uv`. Property testing: Hypothesis.

## Mapping the rules

| Rule | Python |
| --- | --- |
| Public API | `<pkg>.api` for a `libs/` package (tach enforces it); the module API siblings already import inside `vibesys`; `vibesys.api` at the top |
| No patching | Banned: `monkeypatch.setattr`, `setitem`, `delattr`, `delitem`, and `unittest.mock` / `pytest-mock` (`patch`, `MagicMock`, `mocker`). Allowed: `monkeypatch.setenv`, `delenv`, `chdir`, `tmp_path` |
| Fakes | Exemplar: `libs/vs-agent/src/vs_agent/fake_client.py` (`FakeAgentClient`) |
| Exemption | `# test-isolation: <reason>` on the site's line or the line above |

## Contract suite wiring

Parametrize the interface's suite over every Fake and production implementation.
Register every implementation factory. A production parameter requiring real
services carries the `real_contract` marker, which is opt-in with
`VIBESYS_REAL_CONTRACTS=1` (same shape as the `e2e` marker in `pyproject.toml`).

```python
IMPLS = [
    pytest.param(make_fake, id="fake"),
    pytest.param(make_real, id="real", marks=pytest.mark.real_contract),
]

@pytest.mark.parametrize("make", IMPLS)
def test_missing_key_raises_the_documented_error(make):
    store = make()
    with pytest.raises(KeyMissing):
        store.get("absent")
```

For a differential test, use a Hypothesis `RuleBasedStateMachine`.

## Properties

- The root `conftest.py` registers four profiles (`nightly` is in
  [properties-and-goldens.md](properties-and-goldens.md)), all with `deadline=None`
  (Hypothesis's deadline is a wall-clock dependence), so do not set a deadline
  on a property. `ci` is derandomized and is selected automatically when `CI`
  is set, so CI runs are a pure function of the code. `dev` is the local
  default. `explore` is randomized with 500 examples; run it by hand to hunt
  for new bugs: `HYPOTHESIS_PROFILE=explore uv run pytest path/to/test.py`.
- Pin a found failure with `@example(...)`, so it reproduces under the
  derandomized `ci` profile too.
- Build strategies from the public Pydantic models and enums.

## Deterministic simulation (`vs-sim`)

`libs/vs-sim` owns time, scheduling, seeds, signals, child processes and
blocking calls (`vs_sim.api` for the interfaces product code takes,
`vs_sim.api.testing` for the simulator, Fakes, waits and contract suites). Its
pytest plugin (`libs/vs-sim/pytest_plugin`, registered by the root
`conftest.py`) makes the simulator the default:

- An `async def` test outside `tests/e2e`, `tests/slurm_cluster` and
  `tests/minimal_container` that has no `pytest.mark.asyncio` runs on a virtual
  clock: sleeping costs no wall time, and a test that waits on nothing raises
  `VirtualDeadlockError` at once. A test marked `asyncio` stays on
  pytest-asyncio; do not combine the marker with the `sim` fixture. Async
  fixtures are not supported on the virtual loop.
- The `sim` fixture gives `sim.clock`, `sim.seed`, `sim.random(label)`,
  `sim.gate()`, `sim.run(coro)` (for sync tests), `sim.run_in_child(fn)` and
  `sim.world(name)` (domain fakes registered in a conftest with
  `vs_sim.api.testing.WORLDS`).
- Use `Gate`/`arrival`, `wait_for_state` with `Changes`, and the `*_or_fail`
  waits instead of polling or bare waits; run anything that changes signals,
  environment, working directory or timers in `run_in_child`.
- A failing sim test prints its seed; `--sim-seed=N` replays it exactly.
  `--sim-determinism-check` runs each sim test twice with one seed and fails if
  the event traces (clock advances, task steps, input from outside the
  simulation) differ.

## Golden fixtures

Prompt snapshots live under `tests/vibesys/loops/*/fixtures/prompt_snapshots`.
Regenerate with `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest <file>`, then read the
diff.

## Commands

```bash
uv run pytest path/to/test.py -k name              # narrowest first
uv run python scripts/check_test_isolation.py      # ratchet; --write only lowers counts
uv run pytest path/to/test.py -n auto --no-cov -q  # parallel
```

`tests/quality/test_real_apis_confined.py` fails a deterministic-tier test (or product
code outside `libs/vs-sim`) that uses real time, threads, processes, sockets or signals,
against the exact-count baseline `tests/quality/real_api_baseline.jsonl`; counts only go
down.

`scripts/check_test_isolation.py` counts patching, mocking, sleeps
(`time.sleep`, `asyncio.sleep` other than `asyncio.sleep(0)`), timeout
verdicts, and non-`api` imports of a library inside its own tests. The baseline
is `tests/quality/isolation_baseline.jsonl`. CI runs with `-n auto --dist
loadgroup`; the `serial` marker is a last resort for a host-wide resource.

CI splits the suite across sixteen runners (`VIBESYS_TEST_SHARD=I/16` or
`--shard=I/16`, balanced by `tests/support/shard_durations.json`). Whole files
are placed by recorded seconds; a file over 1.1x the mean shard load has its tests spread over the
shards. A test must not depend on running beside another test of its file or
module. Each shard warns when it overruns its budget or runs a file the record
underestimates. Every green run on main caches its measured seconds and later
runs balance on that cache (`$VIBESYS_SHARD_DURATIONS`), so the record
maintains itself; the checked-in file is only the cold start, refreshed with
`python -m scripts.refresh_shard_durations` on a run's `shard-durations-*`
artifacts. A stale file only unbalances the shards. Slow generated checks (Hypothesis examples, chaos seeds)
run reduced in pull requests and at full strength in
`.github/workflows/nightly.yml`.

A timeout verdict is a timeout or clock reading that decides the outcome rather
than only failing a hung test: `assert not ev.wait(timeout=T)`,
`return ev.wait(timeout=T)`, a timed wait inside `pytest.raises(TimeoutError)`,
and a clock reading compared inside an `assert`, `while`, or `if`.
`assert ev.wait(timeout=T)` is a deadlock guard and does not count, because
raising T cannot turn its pass into a failure. The script's docstring states
the criterion and what it deliberately misses.
