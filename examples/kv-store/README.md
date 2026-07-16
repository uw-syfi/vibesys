# KV Store Target for VibeSys

A non-persistent, in-memory KV store optimized by VibeSys's multi-agent loop.
The agent starts from a minimal Python RESP2 server (~10k ops/sec), gated by
correctness (candidate vs a real Redis oracle) and scored by real YCSB.

## Prerequisites

- Python 3.11+ and `uv`
- Java 8+ — YCSB (`apt install default-jre` / `brew install openjdk`)
- Redis — the accuracy oracle (`apt install redis-server` / `brew install redis`)
- Claude Code (`claude`) — drives the agent loop

## Setup

```bash
uv sync
uv pip install -r examples/kv-store/requirements.txt
cp agent.toml.example agent.toml            # no API key needed for --cli-provider claude
```

The benchmark auto-downloads YCSB 0.17.0 (Redis binding) on first run; no manual setup.

Verify the harness end-to-end against the seed: `examples/kv-store/run_test.sh`.

## Run

```bash
vibe-sys --outer-loop agent \
  --input examples/kv-store \
  --exp-name kv-store-opt \
  --backend cpu \
  --agent-backend cli --cli-provider claude \
  --max-rounds 6 \
  --modality kv_store \
  --interface service \
  --no-skills --git-tracking
```

`--interface service` judges the store only over its RESP2 socket, so the agent
may implement it in any language. Each round is a git commit in
`exp_env/<name>/workspace/`; only rounds that pass the accuracy checker advance.

## Files

```
examples/kv-store/
├── OBJECTIVE.md                   # Target spec (read by the orchestrator)
├── vibesys.input.toml             # Manifest: domain, checker, benchmark commands
├── run_test.sh                    # Standalone end-to-end test against the seed
├── reference/seed_server.py       # Seed baseline / RESP2 reference
├── accuracy_checker/checker.py    # Correctness: candidate vs Redis oracle
└── benchmark/benchmark.py         # Performance: YCSB wrapper (fetches ycsb/ on first run)
```
