---
name: write-vibesys-tests
description: Write or change tests in this repository with its own harness (test tiers and where a test goes, vs-sim deterministic simulation and real-API confinement, --depth and the Hypothesis profiles, contract suites, vs-faults, goldens, the wire-protocol conformance corpus, sharding and CI budgets). Use when adding or editing a Python or TypeScript client test, fixing a bug that needs a regression test, a test fails under vs-sim or a test-quality ratchet, or a CI shard or time budget overruns.
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

- Adding or changing a Python test in a package, in the cross-package layers,
  or a TypeScript test in a client package.
- Fixing a bug: the regression test and its generalizing property.
- A test fails under the simulator, or a test-quality ratchet fails.
- Changing the pytest configuration, the root `conftest.py`, the vs-sim pytest
  plugin, or the CI test jobs.

## Steps

1. **Place the test in the cheapest layer that can observe the guarantee.**
   The layers, what each guarantees, and the decision list for a new test are
   in [testing-map.md](../../../docs/contributing/testing-map.md). New tests go
   beside the package they test; the cross-package test layers do not grow,
   and nothing test-shaped goes into the shipped resources.
   Unenforced: choosing a layer needs judgment.
2. **Keep deterministic tiers on the simulator.** Every test outside the real
   tiers (the directories the pytest configuration lists as `sim_real_tiers`)
   uses no real time, threads, processes, sockets or signals except through
   `vs_sim.api`. Ruff's `TID251` bans real time;
   `test_deterministic_tests_use_no_real_time_threads_processes_or_sockets` and
   `test_product_code_reaches_real_apis_only_through_vs_sim` fail on any other
   real API outside a baseline that only shrinks. Rewrites for each flagged use are in
   [references/python.md](references/python.md).
3. **Bound every wait you do not control with a hang guard** from the vs-sim
   testing API, never a sleep or a timeout verdict.
   `test_no_test_blocks_without_a_bound_on_a_thread_process_or_socket` fails
   on an unbounded wait, and `check_test_isolation` counts sleeps and timeout
   verdicts.
4. **No patching or mocking; use the owning library's Fake** and register
   every implementation in its contract suite. `check_test_isolation` fails
   when the count of patches, mocks and non-`api` imports rises above its
   recorded baseline.
5. **Scale generated checks with `--depth`, never by hand.** State a
   non-default example count with the vs-sim testing helper `examples` and a
   crash sweep with `swept`; the profiles and counts live only in the root
   `conftest.py`. Unenforced: a per-file depth constant or an environment
   read is easy to spot in review and rare enough that a check would cost more
   than it saves.
6. **Inject faults through the vs-faults `FaultPlan`** (reached through
   `vs_faults.api`) and the wrappers built on it; do not write a faulty Fake
   per test. Unenforced: a second fault mechanism is a design choice a check
   cannot tell from a legitimate Fake failure knob.
7. **Pin goldens and protocol behavior in the shared fixtures.** Each prompt
   golden lives beside the test that covers it and compares byte for byte
   through the shared golden helper. Which golden covers which prompt, and how
   to update one, is in the `write-agent-prompt` skill's goldens reference.
   Protocol changes extend the shared conformance corpus, and
   `pnpm check:conformance` fails when an event or scenario lacks coverage or
   no runner executes it.
8. **Never print a full diff of a large golden.** pytest's diff of two stale
   300 KB replays took 223 to 803 s under `-v` and cancelled a shard. The
   bounded-comparison plugin explains a failed `==` over a large operand by
   sizes, digests and the first difference
   (`test_a_failed_comparison_prints_no_diff_at_any_verbosity`).
9. **Do not commit what a projector or schema change rewrites.** The clients'
   native replays are generated under a coarse hash of their inputs, the same
   list that also selects the client jobs (`test_a_changed_byte_of_any_input_names_a_different_set`).
   A committed golden pins a contract or is read where no Python runs
   (`test_gateway_conversion_fixture_regenerates_identically`;
   `./scripts/regenerate_goldens.sh` rewrites it); a text snapshot per scenario
   (`test_a_scenario_projects_to_the_shape_its_snapshot_records`) shows a projector change.
10. **Keep a parametrized test id short.** A long value in the id cut a
    shard's CI log; give it `pytest.param(value, id="short-name")`.
    `test_a_long_parameter_value_stops_the_session_naming_the_fix` covers it.
11. **Stay inside the CI budgets.** The whole-run wall-time thresholds are set
   in the CI test workflow, and the shards balance on recorded per-file
   seconds. Mark a test `slow` only when it is an exhaustive sweep, so PR depth
   runs one case of it.
   `test_every_test_runs_in_exactly_one_shard_whether_or_not_its_file_is_heavy`
   fails if sharding drops or duplicates a test; the wall-time thresholds fail
   the CI job itself.

## Testing

- Run the narrowest target first (`uv run pytest <test file> -k <name>`), then
  the package. Run only the checks the change can affect; CI runs the rest.
  Unenforced: which checks a change can affect is judgment.
- For a change to recovery, the shell, or a contract suite, also run the
  nightly depth over the libraries and the system and product test layers
  (command in [references/python.md](references/python.md)).
  Unenforced: CI runs nightly depth only on schedule, after the PR has merged.
- For a sim test that failed once, replay its printed seed with
  `--sim-seed=N` and run `--sim-determinism-check`.
  Unenforced: the vs-sim plugin's own tests cover both options; a test that
  needs them has no check beyond running them by hand.
- Real contract suites and real tiers need opt-in variables
  (`VIBESYS_REAL_CONTRACTS=1` and the ones in testing-map.md); report in the
  PR which ran and which environments were unavailable.
  Unenforced: CI has no real cluster, container runtime or credentials.

## Known failure modes

- A test passes alone and fails beside its neighbours because sharding moved
  its file or spread its tests: tests must not depend on file order or on
  sharing a process with another test. Unenforced: a hidden ordering
  dependency shows up only in some shard layouts.
- A property passes on PR depth and fails nightly because the PR profile is
  derandomized and draws few examples: pin each nightly failure with
  `@example(...)` so PR runs replay it. Enforced for regression tests by
  `test_every_property_regression_test_pins_its_input`. Unenforced for other
  nightly finds: the result arrives after merge.
- An `async def` test marked `asyncio` silently leaves the virtual clock and
  then waits in real time; drop the marker unless the test needs
  pytest-asyncio.
  `test_deterministic_tests_use_no_real_time_threads_processes_or_sockets`
  catches the real sleeps it then needs.
- The per-layer CPU budgets in testing-map.md read as enforced but nothing
  sums them. Unenforced: no check measures per-layer CPU yet; a test that
  needs a real process or repository says why in a comment.
- A conformance scenario that validates structurally but that no runner
  registers has never executed against a transport; `pnpm check:conformance`
  reports it as a setup need.

## Sources of truth

- The Hypothesis profiles and their example counts, in the root `conftest.py`.
- The pytest configuration: markers, the real-tier and smoke-slow directory
  lists, and the per-test timeout.
- The vs-sim pytest plugin: `--depth`, the `sim` fixture, seeds and the
  determinism check.
- The CI test workflow: shards, wall-time thresholds, and which jobs run.
- [testing-map.md](../../../docs/contributing/testing-map.md): the layers and
  where a new test goes.
- The wire-protocol conformance corpus and its readers, described in
  [wire-protocol.md](../../../docs/contributing/wire-protocol.md).
