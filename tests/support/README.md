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
