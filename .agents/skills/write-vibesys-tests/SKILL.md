---
name: write-vibesys-tests
description: Write or change tests in this repository with its own harness (test tiers and where a test goes, vs-sim deterministic simulation and real-API confinement, --depth and the Hypothesis profiles, contract suites, vs-faults, goldens, the wire-protocol conformance corpus, sharding and CI budgets). Use when adding or editing a test under tests/, libs/*/tests/ or clients/, fixing a bug that needs a regression test, a test fails under vs-sim or a ratchet in tests/quality/, or a CI shard or budget overruns.
metadata:
  kind: codebase
---

# Write VibeSys tests

The `testing` skill says what makes a test valuable anywhere. This skill says
how those rules are wired here: which tier a test belongs in, the simulator
every deterministic test runs on, and the checks that hold the suite to the
rules. Open [references/python.md](references/python.md) for the Python
harness (vs-sim, profiles, faults, goldens, sharding, commands) and
[references/typescript.md](references/typescript.md) for the clients and the
conformance corpus.

## Builds on

- `testing`: every rule there applies; this skill adds only the wiring.
- `software-design`: rule 9 (I/O behind the owning library's interface) and
  rule 14 (functional core), which make the tiers below possible.

## When this applies

- Adding or changing a test under `tests/`, `libs/*/tests/`, or a `*.test.ts`
  under `clients/`.
- Fixing a bug: the regression test and its generalizing property.
- A test fails under the simulator, or a ratchet in `tests/quality/` fails.
- Changing `conftest.py`, `libs/vs-sim/pytest_plugin/`, the
  `[tool.pytest.ini_options]` settings in `pyproject.toml`, or the test jobs in `.github/workflows/test.yml`.

## Steps

1. **Place the test in the cheapest layer that can observe the guarantee.**
   The layers, what each guarantees, and the decision list for a new test are
   in [testing-map.md](../../../docs/contributing/testing-map.md). New tests go
   beside the package they test; the top-level `tests/` holds only
   cross-package layers and does not grow, and nothing test-shaped goes under
   `resources/`, which ships. Unenforced: choosing a layer needs judgment.
2. **Keep deterministic tiers on the simulator.** Everything outside the
   `sim_real_tiers` directories in `pyproject.toml` uses no real time, threads,
   processes, sockets or signals except through `vs_sim.api`;
   `tests/quality/test_real_apis_confined.py` enforces it for tests and for
   product code, against a baseline that only shrinks. Rewrites for each
   flagged use are in [references/python.md](references/python.md).
3. **Bound every wait you do not control with a hang guard** from
   `vs_sim.api.testing`, never a sleep or a timeout verdict
   (`tests/quality/test_no_unbounded_blocking_waits.py`,
   `scripts/check_test_isolation.py`).
4. **No patching or mocking; use the owning library's Fake** and register
   every implementation in its contract suite.
   `scripts/check_test_isolation.py` counts patches, mocks and non-`api`
   imports against `tests/quality/isolation_baseline.jsonl`.
5. **Scale generated checks with `--depth`, never by hand.** State a
   non-default example count with `vs_sim.api.testing.examples` and a crash
   sweep with `vs_sim.api.testing.swept`; the profiles and counts live only in
   the root `conftest.py`. Unenforced: a per-file depth constant or an
   environment read is easy to spot in review and rare enough that a check
   would cost more than it saves.
6. **Inject faults through `vs_faults.api.FaultPlan`** and the wrappers built
   on it; do not write a faulty Fake per test.
   Unenforced: a second fault mechanism is a design choice a check cannot
   tell from a legitimate Fake failure knob.
7. **Pin goldens and protocol behavior in the shared fixtures.** Prompt
   goldens go through `tests/vibesys/golden/helpers.py`; protocol changes
   extend `tests/conformance/`, which
   `clients/tools/scripts/check_conformance_corpus.mjs` gates for coverage.
8. **Stay inside the CI budgets.** The whole-run wall-time thresholds are the
   `CI_WALL_TIME_*` values in `.github/workflows/test.yml`, checked by
   `scripts/ci_wall_time.py`; the shards balance on
   `tests/support/sharding.py`. Mark a test `slow` only when it is an
   exhaustive sweep, so PR depth runs one case of it.

## Testing

- Run the narrowest target first (`uv run pytest path -k name`), then the
  package, then `./scripts/check_ci.sh` before the PR; its groups are in
  `.repoctl/checks.toml`.
- For a change to recovery, the shell, or a contract suite, also run the
  nightly depth over `libs`, `tests/system` and `tests/vibesys` (command in
  [references/python.md](references/python.md)).
  Unenforced: CI runs nightly depth only on schedule
  (`.github/workflows/nightly.yml`), after the PR has merged.
- For a sim test that failed once, replay its printed seed with
  `--sim-seed=N` and run `--sim-determinism-check`; the vs-sim plugin tests in
  `libs/vs-sim/tests/` cover both options.
- Real contract suites and real tiers need opt-in variables
  (`VIBESYS_REAL_CONTRACTS=1` and the ones in testing-map.md); report in the
  PR which ran and which environments were unavailable.
  Unenforced: CI has no real cluster, container runtime or credentials.

## Known failure modes

- A test passes alone and fails beside its neighbours because sharding moved
  its file or spread its tests: tests must not depend on file order or on
  sharing a process with another test (`tests/support/sharding.py`).
- A property passes on PR depth and fails nightly because the PR profile is
  derandomized and draws few examples: pin each nightly failure with
  `@example(...)` so PR runs replay it. Unenforced: the nightly result
  arrives after merge.
- An `async def` test marked `asyncio` silently leaves the virtual clock and
  then waits in real time; drop the marker unless the test needs
  pytest-asyncio (`tests/quality/test_real_apis_confined.py` catches the real
  sleeps it then needs).
- The per-layer CPU budgets in testing-map.md read as enforced but nothing
  sums them. Unenforced: no check measures per-layer CPU yet; a test that
  needs a real process or repository says why in a comment.
- A conformance scenario that validates structurally but that no runner
  registers has never executed against a transport
  (`clients/tools/scripts/check_conformance_corpus.mjs` reports it as a
  setup need).

## Sources of truth

- `conftest.py`: the Hypothesis profiles and their example counts.
- `pyproject.toml` (`[tool.pytest.ini_options]`): markers, `sim_real_tiers`,
  `sim_smoke_slow`, the per-test timeout.
- `libs/vs-sim/pytest_plugin/vs_sim_pytest.py`: `--depth`, the `sim`
  fixture, seeds and the determinism check.
- `.github/workflows/test.yml`: shards, the CI wall-time thresholds, and which
  jobs run.
- [testing-map.md](../../../docs/contributing/testing-map.md): the layers and
  where a new test goes.
- `tests/conformance/README.md`: the conformance corpus and its readers.
