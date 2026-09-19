# KV Store Target for VibeSys

A non-persistent, in-memory KV store optimized by VibeSys's multi-agent loop.
The agent starts from a minimal Python RESP2 server (~10k ops/sec), gated by
correctness (candidate vs a real Redis oracle) and scored by real YCSB.

## Prerequisites

- Python 3.12+ and `uv`
- Java 8+ — YCSB (`apt install default-jre` / `brew install openjdk`)
- Redis — the accuracy oracle (`apt install redis-server` / `brew install redis`)
- Claude Code (`claude`) — drives the agent loop

## Setup

```bash
uv sync
uv pip install -r examples/kv-store/requirements.txt
```

The benchmark auto-downloads YCSB 0.17.0 (Redis binding) on first run into
`~/.cache/vibesys/kv-store/ycsb` (override with `KV_STORE_YCSB_HOME`); no manual setup.
`agent.toml` is optional; the command below selects Claude Code explicitly.

Verify the harness end-to-end against the seed: `examples/kv-store/.vibesys/tasks/default/run_test.sh`.

## Run

```bash
vibesys --outer-loop agent \
  --headless \
  --input examples/kv-store \
  --runs-dir /work/vibesys-runs --local \
  --exp-name kv-store-opt \
  --backend cpu \
  --agent-backend cli --cli-provider claude \
  --max-rounds 6 \
  --modality kv_store \
  --interface service \
  --no-skills
```

`--interface service` judges the store only over its RESP2 socket, so the agent
may implement it in any language. The copied project, including candidate
source and `.vibesys/state/` state, is created at
`/work/vibesys-runs/<run-id>/`. Its evolution is recorded in Git. Only rounds
that pass the accuracy checker advance.

## Files

The example has one task, `default`, so `--input examples/kv-store` selects it
without `--task`. Everything the evaluator owns lives under `.vibesys/tasks/`,
which is read-only during a run.

```
examples/kv-store/
├── requirements.txt                          # Python deps for the checker (redis client)
└── .vibesys/tasks/default/
    ├── OBJECTIVE.md                          # Target spec (read by the orchestrator)
    ├── vibesys.input.toml                    # Manifest: domain, checker, benchmark commands
    ├── run_test.sh                           # Standalone end-to-end test against the seed
    ├── reference/seed_server.py              # Seed baseline / RESP2 reference
    ├── accuracy_checker/checker.py           # Correctness: candidate vs Redis oracle
    └── benchmark/benchmark.py                # Performance: YCSB wrapper (fetches YCSB on first run)
```
