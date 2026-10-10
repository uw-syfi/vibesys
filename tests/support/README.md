# Suite execution

Run the core and library suite with:

```sh
uv run pytest tests/vibesys libs -n 10 -q
```

Local runs start tests from expensive files first, using the recorded per-file
durations divided by their collected item counts. xdist load scheduling keeps
worker queues short with `--maxschedchunk=1`, so long tests start before workers
finish their other work. Tests within a file retain their collection order;
serial runs keep the original collection order.
Coverage and Hypothesis example counts remain unchanged. CI explicitly uses
`--dist loadgroup` to group consumers of session-scoped native build fixtures.
Use that option when running the native example tests locally as well.

The root environment plugin installs private HOME, state and XDG directories
before application imports. Each worker owns its home and runtime directory,
and each test gets a separate lazy state path beneath its pytest basetemp.
Children inherit these defaults. Tests can still set explicit environment inputs.
Process-owned directories are removed at pytest cleanup.

Installed Rust toolchains remain available through explicit CARGO_HOME and
RUSTUP_HOME paths. Go compiler caches default to a private controller-owned
directory shared with workers; explicit compiler-cache paths are preserved.
These caches support concurrent compilation and are separate from mutable
operator state and runtime locks.

Measure a run with `--durations=60 --record-shard-durations=/tmp/durations.json`.
The JSON records additive setup, call and teardown worker-seconds per file,
not directory wall time. To measure directory wall time, run each directory
separately with the same worker count and environment.

# What lives here

`tests/support` is for what only the whole repository can own. A helper that
depends on one library's interface belongs in that library, and a generic
time, concurrency, crash or fault helper belongs in `vs_sim`. Libraries are
imported through `<package>.api` or `<package>.api.testing` only.

| Kind | Files |
| --- | --- |
| Composition worlds and scenarios (several libraries wired together) | `*_world.py`, `skeleton_*.py`, `timed_dynamic_run.py`, `crash_harness.py`, `evaluation_scenarios.py`, `loop_invariants.py`, `waiting_loop_strategy.py`, `concurrent_turns_strategy.py`, `executor_cases.py` |
| Contract suites that span libraries | `command_runner_contract.py`, `executor_cases.py` (runs `vs_runtime.api.executor_contracts` over real owners) |
| Pytest infrastructure | `sharding.py`, `shard_durations.json`, `cwd_hygiene.py`, `path_hygiene.py`, `scratch_tree.py`, `isolated_environment.py`, `shared_build.py`, `fast_cargo.py`, `posix_tools.py`, `example_registry.py`, `container_credentials.py` |
| Run-environment specs built from `vibesys.api` types | `docker_environment.py`, `host_environment.py`; `slurm_environment.py` waits for #1650 |

Where the rest went:

| Helper | Home |
| --- | --- |
| crashable and probing virtual clocks, `clock_from`, `pairwise_rows`, `file_size_limit`, fault counting, matching and seeded streams | `vs_sim.api.testing` |
| `wait_until_executor_started` | `vs_evaluation.api.testing` |
| liveness invariants | `vs_core.testing.liveness` |
| `FakeDockerConfinement` | `vs_sandbox.api.testing` |
| `WorldGit`, `GitKind`, the `GitRepository` contract scaffolding, `run_execution_record`, `scratch_state_directory` | `vs_project.api.testing` |
| daemon-backed Docker config, executor and observation contracts, runtime fixtures | `vs_runtime.api.testing`, `vs_runtime.api.*_contracts`, `vs_runtime.api.*_fixtures` |
| agent, process and conversation fault wrappers | `vs_agent.api.testing` |
