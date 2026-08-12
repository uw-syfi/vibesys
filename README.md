# vibe-database: Can AI Agents Superoptimize a Real Query Engine In Place?

**An agentic loop that takes a real streaming / dataflow engine's *own source*, vendors it
verbatim as round 0, and then makes correctness-preserving micro-optimizations to it round after
round — proving each win by output-equivalence against the pristine build and CPU-seconds on a
fixed workload.**

## Introduction

Most "AI writes a database" demos synthesize a bespoke engine from scratch and then claim a large
speedup — but a from-scratch engine has different architecture, different guarantees, and a
different feature surface than the mature system it's compared against, so the "win" is mostly an
apples-to-oranges artifact.

vibe-database takes the honest, harder path: **in-place superoptimization**. You name a real
reference engine; the harness copies that engine's **actual upstream source** into the run
workspace as **round 0** — which is *both* the baseline *and* the starting point — and each round
a coding agent makes **micro-optimizations to that real source in place** (allocation removal,
branch hoisting, layout, codegen knobs). The architecture stays **identical to the reference
engine by construction**, because the agent edits it and never reimplements it. Every accepted
round must:

1. produce **byte-identical output** to the pristine round-0 build on every workload (correctness
   = output-equivalence, no reimplemented oracle to game), and
2. survive a **`diff` discipline gate** — every hunk is a named micro-optimization; any
   rewrite / algorithm swap / new heavyweight dependency is an automatic fail.

The metric is `cpu_reduction_ratio = baseline_cpu_seconds / candidate_cpu_seconds`. The honest
expectation is a **modest percentage, not a multiple** — and that honesty is the point: a win here
is provably *the same code path with the same guarantees, just cheaper*.

The first end-to-end target is **[differential-dataflow](https://github.com/TimelyDataflow/differential-dataflow)**
(`examples/differential-dataflow-cpu-bench/`), superoptimized on its shipped incremental `bfs`
workload. It is a tractable, pure-Rust engine that builds from source in ~13 s — chosen to prove
the loop before scaling to larger engines (RisingWave / Flink).

## Benchmark: differential-dataflow `bfs`, 3-round run

A 3-round agent run (`--reference-engine differential-dataflow --backend cpu --max-rounds 3`)
on the incremental `bfs` example. The workload is self-generated from a fixed RNG seed and run at
`-w 1` for deterministic output; the result multiset is canonicalized by `.consolidate()`.

| Round | Output vs pristine | CPU vs round-0 (same-session) | What the agent did |
|------:|--------------------|-------------------------------|--------------------|
| 0     | — (baseline)       | 1.00×                         | vanilla vendored source |
| 2     | byte-identical     | ~1.06× (~6% less)             | `consolidation.rs` comparator spelling |
| 3     | byte-identical     | **~1.16× (~16% less)**        | `merge_batcher.rs` forward-cursor merge (removed a `reverse()` pass + hoisted loop-boundary tests) + `[profile.release]` codegen sweep |

**Round-3 result, independently reproduced by the Judge:**

- **Output-equivalence: PASS** on both the canonical and a perturbation workload (347 / 378 data
  lines byte-identical to a *live* pristine `_ref_engine` run — nothing stored to memorize).
- **CPU: ~16% less** than the pristine round-0 build measured in the same session (~8.5% less than
  the round-2 checkpoint). The headline `cpu_reduction_ratio ≈ 1.18` includes ~1.7 points of
  stale-baseline drift, which the run discloses rather than banking.
- **Discipline: PASS.** `diff -ru _ref_engine engine` touches exactly **three files**
  (`Cargo.toml [profile.release]`, `consolidation.rs`, `merge_batcher.rs`); `Cargo.lock` and the
  crate's own `Cargo.toml` are byte-identical — **no new dependency**.
- **Safety: clean.** `valgrind` reports 0 errors and `0 bytes definitely lost`; the six new
  `unsafe` sites are all length-reset / element-move with SAFETY comments and a documented
  ownership invariant; `cargo test --lib merge_batcher` passes.
- **No reward hacking.** `bfs.rs` is byte-identical, so BFS is still computed entirely by
  differential-dataflow's own `iterate`/`join_map`/`reduce`/`consolidate` dataflow; the changed
  files are generic library code with no knowledge of the workload constants.

Callgrind on the final binary: program instructions −10.5%, conditional branches −15.2%; the
hoisted loop-boundary tests now execute 4.0 M times instead of 56.3 M while doing the identical
56.3 M merge steps — the same work, cheaper per step.

## How it works

Two nested loops over a git-recorded history of validated checkpoints. Each candidate is a git
commit; the outer loop only advances on Judge-validated commits, so an incorrect or
discipline-violating candidate can never poison a later round.

- **Outer loop** — a search policy that plans the next optimization and updates persistent state
  (roadmap, long-term memory, commit graph). Three policies live under `src/vibe_database/loops/`,
  selected by `--outer-loop`: `agent` (an LLM Orchestrator plans each round — the default),
  `plain` (deterministic issue-queue drain), and `evolve` (population-based / Pareto frontier).
- **Inner loop** — three role-specialized coding-agent invocations on a shared workspace:
  - *Implementer* edits the vendored engine source in place.
  - *Accuracy Judge* rebuilds the candidate, runs the pristine engine **live** to regenerate the
    golden output, checks byte-equivalence, runs the `diff` discipline + reward-hack gates, and
    only lets correct candidates through.
  - *Performance Evaluator* runs the benchmark and reports `cpu_reduction_ratio` back to the
    policy.
- **Execution environment** — mounts the user-provided artifacts (`reference/`,
  `accuracy_checker/`, `benchmark/`) **read-only**, so the Implementer cannot edit the checker or
  the reference; only the vendored `engine/` tree is writable.

The vendoring is generic: `--reference-engine <name>` is a **preset** that fills in the five
existing target args (`--ref` / `--acc-checker` / `--bench` / `--domain` / `--modality`) from a
small registry (`src/vibe_database/reference_engines.py`). Adding a new engine is a data +
template change, not a harness change.

## Installation

Requires Python 3.11+ and a userspace Rust toolchain (for building the vendored engine).

```bash
uv sync
cp .env.example .env          # provider keys (Anthropic / OpenAI / Vertex / …)
cp agent.toml.example agent.toml
export PATH="$HOME/.cargo/bin:$PATH"   # so the run can `cargo build` the engine
```

## Quickstart

```bash
# Superoptimize differential-dataflow's own bfs source, in place, for 3 rounds.
vibe-database \
  --outer-loop agent \
  --reference-engine differential-dataflow \
  --backend cpu \
  --max-rounds 3 \
  --exp-name dd-superopt
```

`--reference-engine` is a preset that resolves `--ref` / `--acc-checker` / `--bench` /
`--domain` / `--modality` for you; any of those you pass explicitly wins over the preset.
`--outer-loop` defaults to `agent`; pass `plain` or `evolve` to switch. See
`vibe-database --outer-loop <kind> --help` for loop-specific flags.

Resume any run (defaults to the newest):

```bash
vibe-database --resume                  # newest run
vibe-database --resume 20260809-...     # a specific exp_env/ dir
```

## The target: `examples/differential-dataflow-cpu-bench/`

```
examples/differential-dataflow-cpu-bench/
├── OBJECTIVE.md                       # the superoptimization contract (read at run start)
├── ref_engine/                        # vendored, pinned, offline upstream source (→ engine/ round 0)
├── reference/
│   └── workload.py                    # single source of truth for the fixed bfs workload args
├── accuracy_checker/
│   └── equivalence_gate.py            # builds pristine live, compares candidate byte-for-byte
└── benchmark/
    ├── benchmark.py                   # process-tree CPU harness → cpu_reduction_ratio
    ├── capture_baseline.py            # one-time baseline capture (machine-specific)
    └── baseline.json                  # captured round-0 CPU-seconds
```

`OBJECTIVE.md` must be a **sibling** of `--ref`, not inside it. At run start the harness copies
`ref_engine/` into the workspace as the editable `engine/` **and** a pristine `_ref_engine/`, then
commits `engine/` as round 0 — the frozen architecture. The `bfs` workload is generated from a
fixed RNG seed (no on-disk input), uses pure-integer values (exact byte comparison, no float
tolerance), and is genuinely incremental (a batch of edges inserted/removed each round).

## Domains and modalities — pointing the loop at a problem

The prompt an agent sees is assembled from orthogonal axes, each dropping into one labelled slot:

- **domain** (`--domain`, `loops/agent/templates/_domain/*.md`) — the cross-cutting context: what
  the implementer must know, and the correctness / discipline / integrity gates the judge
  enforces. `differential-dataflow` carries the in-place superoptimization framing; `generic`
  injects nothing (copy it to start your own). A domain is one Markdown file with `## implementer`
  / `## judge` / `## single_agent` / `## orchestrator` sections — authoring guide:
  [`_domain/README.md`](src/vibe_database/loops/agent/templates/_domain/README.md).
- **modality** (`--modality`, `loops/agent/templates/_modality/<kind>/`) — the per-task
  build / measure / grade contract. `dataflow-opt` builds the engine with `cargo`, runs the
  equivalence gate + `diff` discipline gate, and reports `cpu_reduction_ratio`.

`--reference-engine differential-dataflow` selects `--domain differential-dataflow` and
`--modality dataflow-opt` for you. Adding a domain / modality is a template change, not a code
change.

## Configuration (`agent.toml`)

```toml
[model]
name = "claude-sonnet-4-6"   # auto-detected provider for claude-* / gpt-* / gemini-*
# provider = "anthropic"     # optional override

[backend]
name = "cpu"                  # compiled engines run on CPU

[agent]
backend = "cli"               # "cli" (codex/claude/gemini/opencode) or "deepagents"
cli_provider = "claude"       # which coding-agent harness to drive
# cli_model = "..."           # override the model the CLI tool uses
# cli_timeout = 1800          # per-invocation timeout (seconds)
```

Provider credentials live in `.env` (see `.env.example`). CLI flags `--agent-backend` /
`--cli-provider` / `--backend` override the file. The config is validated against a typed schema
on load (`vibe_database/config.py`) — unknown sections / keys / providers / backends are rejected,
not silently ignored.

## Outputs

Every run creates `exp_env/<timestamp>-<name>/`:

```
exp_env/<run>/
├── workspace/                # git-tracked; engine/ (editable) + _ref_engine/ (pristine), one commit per round
├── logs/
│   ├── run-*.log             # top-level run log
│   ├── run-*-roundNNN.log    # per-round agent log
│   ├── progress.md           # long-term memory the Orchestrator reads/edits
│   └── rounds.json           # per-round audit (commit, metric, pass/fail)
└── reference/                # snapshot of --ref at start
```

`exp_env/` is gitignored and excluded from the uv workspace — runs are local scratch, never
committed.

## Repository layout

```
src/vibe_database/
├── cli.py                     # single entry point: `vibe-database`
├── reference_engines.py       # --reference-engine preset registry
├── context.py                 # _RunContext: lifecycle, vendoring, ctx.invoke()
├── agent_runner.py            # invoke wrappers + structured-response extraction
├── prompts.py                 # Jinja + domain/modality/backend renderer
├── schemas.py                 # Pydantic response schemas
├── config.py / constants.py
│
├── loops/                     # the three outer-loop search policies
│   ├── agent/                 # Orchestrator-driven (default); templates/_domain, _modality here
│   ├── plain/                 # deterministic issue-queue drain
│   ├── evolve/ (+ openevolve/)# population-based
│   └── profiler.py            # shared Performance Evaluator helper
│
├── sandbox/                   # execution-environment policy (local exec is the default path)
├── agents/                    # coding-agent harness abstraction
└── backends/                  # cpu compute backend

examples/differential-dataflow-cpu-bench/   # the in-place superoptimization target
```

## Development

```bash
./scripts/format.sh                                 # format checked Python dirs
./scripts/check_format.sh                           # check formatting for CI
uv run pytest                                        # full suite
uv run pytest tests/loops/agent/test_domain_packs.py # one file
uv run pytest -k orchestrator                        # by keyword
```

CI (`.github/workflows/test.yml`) runs `check_format.sh` then `pytest` on Python 3.11 and 3.12.
