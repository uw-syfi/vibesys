# differential-dataflow CPU Target for VibeSys

In-place **superoptimization** of a real upstream engine: the vanilla
[differential-dataflow](https://github.com/TimelyDataflow/differential-dataflow)
crate, micro-optimized on its incremental `bfs` example to shave **CPU-seconds**
at byte-identical output. Round 0 is the unmodified crate; every later round is
the same source with correctness-preserving micro-optimizations, so the win is a
same-code, same-guarantees CPU reduction. Expect a **modest %, not a multiple**.

## How the workspace is materialized

There is no engine vendored in this bundle. `vibesys.input.toml` declares **two
`[[workspace.sources]]`, the same repo at the same pinned commit**, that the
harness clones into the candidate workspace at run time:

- `dest = "engine"`: the **editable** engine (the agent's optimization surface)
- `dest = "_ref_engine"`: a **pristine** byte-identical copy (never edited; the
  correctness diff/golden source)

```
repo   = https://github.com/HQingXuan/differential-dataflow-pinned
commit = 4f05cbb61775a45844a0905de9dacfee1e91dd80
strip_git = true   (both)
```

Both are the trimmed single-member differential-dataflow 0.25.1 cargo workspace.
The bundle's own scripts (`reference/`, `accuracy_checker/`, `benchmark/`,
`profiler/`) are copied into the same workspace root, so they reference `engine/`
and `_ref_engine/` as siblings.

## Prerequisites

- Python 3.12+ and `uv` (the scripts use only the standard library)
- A Rust toolchain (`cargo`) with the differential-dataflow deps warm in
  `~/.cargo`. Every build is `--offline`. Put cargo on PATH:
  `export PATH="$HOME/.cargo/bin:$PATH"`
- `valgrind` + `callgrind_annotate`, for `profiler/attribute_cpu.py`
- A **nightly** Rust toolchain with `rust-src`, for the ThreadSanitizer gate
  (`-Zbuild-std`). The manifest runs the checker with `--strict`, so a missing
  nightly toolchain fails evaluation rather than silently weakening the gate.

## Build

```bash
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release --example bfs -p differential-dataflow \
    --offline --manifest-path engine/Cargo.toml
# binary: engine/target/release/examples/bfs
```

The accuracy checker and the benchmark both build `engine/` automatically if the
binary is missing.

## Accuracy

`accuracy_checker/checker.py` is a single wrapper that builds `engine/` once, then
runs five mechanical behavioral gates and aggregates them:

1. **equivalence**: candidate output byte-identical to the pristine `_ref_engine/`
   (regenerated LIVE) on every fixed workload.
2. **differential-fuzz**: matches the pristine engine on a broad corpus +
   fixed-seed random inputs (anti-memorization).
3. **determinism**: metamorphic, identical output across worker counts.
4. **crash-recovery**: SIGKILL mid-run + restart reproduces the clean-run output.
5. **sanitizer**: ThreadSanitizer build + multi-worker run, no data race.

It does **not** implement diff-discipline (the `diff -ru _ref_engine engine`
"every hunk a micro-opt" judgment). That stays in the LLM judge prompt.

Exit codes: `0` all gates passed; `1` a gate reported a real correctness defect
or a required reference-backed gate could not run; `2` (with `--strict`) an
optional environmental gate could not run (SETUP-ERROR, e.g. no nightly
toolchain) and none failed. The manifest uses `--strict`, so an official VibeSys
evaluation accepts a round only when every configured gate ran and passed.

```bash
uv run python accuracy_checker/checker.py --strict
uv run python accuracy_checker/checker.py --gates equivalence,determinism
```

## Benchmark (the metric)

`benchmark/benchmark.py` measures **`cpu_seconds`**: the median child-process
`getrusage(RUSAGE_CHILDREN)` user+sys CPU-seconds on the fixed metric workload
(median of N timed runs after warmups). It writes JSON with a top-level numeric
`cpu_seconds` field to the `--output-json` path; that scalar is the
`[benchmark.result] metric = "cpu_seconds"` the harness scrapes, and
`objectives.toml` declares `direction = "min"` (lower is better). When
`benchmark/baseline.json` is present it also emits a display-only
`cpu_reduction_ratio = baseline / candidate`.

```bash
uv run python benchmark/benchmark.py \
    --engine-cmd 'engine/target/release/examples/bfs' --output-json /tmp/perf.json
# regenerate the round-0 baseline for this box:
uv run python benchmark/capture_baseline.py
```

## Profiler

`profiler/attribute_cpu.py` runs the built `bfs` binary under
`valgrind --tool=callgrind --cache-sim=no`, parses `callgrind_annotate`, and folds
self-`Ir` into a fixed component vocabulary to rank which engine component to
attack next. It writes a ranked JSON attribution to `--output-json`; it ranks, it
does not score (the score stays `cpu_seconds`). This task-owned profiler is a
manual diagnostic; the ordinary `database` domain does not invoke it
automatically:

```bash
uv run python profiler/attribute_cpu.py \
  --engine-cmd engine/target/release/examples/bfs \
  --output-json /tmp/attribution.json
```

## Run

```bash
export PATH="$HOME/.cargo/bin:$PATH"
vibesys --headless --outer-loop agent \
  --input examples/database/differential-dataflow \
  --runs-dir /work/vibesys-runs --local \
  --exp-name dd-superopt \
  --backend cpu \
  --max-rounds 3
```

The example runs through the existing `database` domain declared in
`vibesys.input.toml`; it does not require a `dataflow_opt` modality or any other
core integration. `--runs-dir` is required because VibeSys must provision the
two pinned workspace sources before starting the loop. Replace
`/work/vibesys-runs` with any writable absolute path.

The agent may edit only the paths listed in `OBJECTIVE.md`. In particular,
`_ref_engine/`, `reference/`, `accuracy_checker/`, `benchmark/`, and `profiler/`
are evaluator-owned. The checker enforces behavioral equivalence; the independent
judge reviews the Git diff for this path discipline and the no-rearchitecture
contract.

After materialization, validate the unmodified seed and the benchmark result
contract from the copied project root:

```bash
./run_test.sh
```

## Files

```
examples/database/differential-dataflow/
├── vibesys.input.toml               # manifest: database domain, strict checker/benchmark commands, 2 workspace sources
├── objectives.toml                  # metric direction (cpu_seconds, min) + pareto noise
├── OBJECTIVE.md                     # target spec (read by the orchestrator)
├── README.md
├── requirements.txt
├── run_test.sh                       # strict accuracy + benchmark contract smoke test
├── reference/
│   └── workload.py                  # single source of truth: workloads, normalize(), build_cmd()
├── accuracy_checker/
│   ├── checker.py                   # wrapper: builds engine/, runs the 5 gates, aggregates
│   ├── equivalence_gate.py
│   ├── differential_fuzz_gate.py
│   ├── determinism_gate.py
│   ├── crash_recovery_gate.py
│   └── sanitizer_gate.py
├── benchmark/
│   ├── benchmark.py                 # getrusage CPU; emits top-level cpu_seconds JSON
│   ├── capture_baseline.py          # regenerates baseline.json from pristine _ref_engine/
│   └── baseline.json
└── profiler/
    └── attribute_cpu.py             # callgrind attribution → ranked component JSON
```
