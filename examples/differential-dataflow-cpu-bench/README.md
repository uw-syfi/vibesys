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

- `dest = "engine"`      — the **editable** engine (the agent's optimization surface)
- `dest = "_ref_engine"` — a **pristine** byte-identical copy (never edited; the
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

- Python 3.11+ and `uv` (the scripts use only the standard library)
- A Rust toolchain (`cargo`) with the differential-dataflow deps warm in
  `~/.cargo` — every build is `--offline`. Put cargo on PATH:
  `export PATH="$HOME/.cargo/bin:$PATH"`
- `valgrind` + `callgrind_annotate` — for `profiler/attribute_cpu.py`
- A **nightly** Rust toolchain with `rust-src` — for the ThreadSanitizer gate
  (`-Zbuild-std`); absent it, that gate reports SETUP-ERROR (see checker exit codes)

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

1. **equivalence** — candidate output byte-identical to the pristine `_ref_engine/`
   (regenerated LIVE) on every fixed workload.
2. **differential-fuzz** — matches the pristine engine on a broad corpus +
   fixed-seed random inputs (anti-memorization).
3. **determinism** — metamorphic: identical output across worker counts.
4. **crash-recovery** — SIGKILL mid-run + restart reproduces the clean-run output.
5. **sanitizer** — ThreadSanitizer build + multi-worker run, no data race.

It does **not** implement diff-discipline (the `diff -ru _ref_engine engine`
"every hunk a micro-opt" judgment) — that stays in the LLM judge prompt.

Exit codes: `0` all gates passed; `1` a gate reported a real correctness defect;
`2` (only with `--strict`) a gate could not run (SETUP-ERROR, e.g. no nightly
toolchain) and none failed. By default a SETUP-ERROR is reported but does not fail
the run, since it is an environment limitation, not a correctness defect.

```bash
uv run python accuracy_checker/checker.py
uv run python accuracy_checker/checker.py --gates equivalence,determinism
uv run python accuracy_checker/checker.py --strict
```

## Benchmark (the metric)

`benchmark/benchmark.py` measures **`cpu_seconds`** — the median child-process
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
does not score (the score stays `cpu_seconds`).

## Run

```bash
export PATH="$HOME/.cargo/bin:$PATH"
vibesys --outer-loop agent \
  --input examples/differential-dataflow-cpu-bench \
  --exp-name dd-superopt \
  --modality dataflow_opt --backend cpu \
  --max-rounds 3
```

## Files

```
examples/differential-dataflow-cpu-bench/
├── vibesys.input.toml               # manifest: domain=database, checker/benchmark commands, 2 workspace sources
├── objectives.toml                  # metric direction (cpu_seconds, min) + pareto noise
├── OBJECTIVE.md                     # target spec (read by the orchestrator)
├── README.md
├── requirements.txt
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
