# KV Store Target for VibeServe

A non-persistent, in-memory KV store optimized by VibeServe's multi-agent loop.
The agent starts from a minimal Python RESP2 server (~10k ops/sec), gated by
correctness (candidate vs a real Redis oracle) and scored by real YCSB.

## Prerequisites

- Linux with readable `/proc` (required for trusted process CPU accounting)
- Python 3.11+ and `uv`
- Java 8+ — YCSB (`sudo apt install default-jre`)
- Redis — the accuracy oracle (`sudo apt install redis-server`)
- `iproute2`; optional `linux-tools`/`perf` for manual profiling
- Claude Code (`claude`) — drives the agent loop

Scored evaluation is Linux-only. Candidate processes must share the evaluator's
PID namespace. Manual `perf`/`py-spy`/`strace` capture may additionally require
adjusting `perf_event_paranoid` or ptrace policy.

## Setup

```bash
uv sync
uv pip install -r examples/kv-store/requirements.txt
cp agent.toml.example agent.toml            # no API key needed for --cli-provider claude
```

The benchmark downloads a checksum-pinned YCSB 0.17.0 Redis binding into the
workspace `.cache/` on first run.

Verify the harness end-to-end against the seed: `examples/kv-store/run_test.sh`.

## Run

```bash
vibe-serve --outer-loop agent \
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
The trusted checker and benchmark each launch `./run.sh <port>` in an isolated
process group and clean it up afterward.

Rounds are scored by `ops_per_cpu_sec` (operations per second of server CPU,
sampled externally from `/proc`) — a client-saturation-immune efficiency metric,
with `throughput_ops_per_sec` and READ/UPDATE p99 as gates. The benchmark drives
the server from several client JVMs (`--client-procs`) to reach saturation.
Optional diagnostics (`--probe-per-op`, `--field-count`, `--field-length`) live
on the same CLI but are not part of the scored path; see
`evaluator_support/` and `benchmark/benchmark.py`. Generic Linux
`--profiler auto` currently resolves to no separate framework profiler; the
benchmark's CPU evidence remains available, and `perf` can be used manually when
host policy permits.

## Files

```
examples/kv-store/
├── OBJECTIVE.md                   # Target spec (read by the orchestrator)
├── CANDIDATE_CONTRACT.md          # Normative protocol and lifecycle contract
├── vibeserve.input.toml           # Manifest: domain, checker, benchmark commands
├── run_test.sh                    # Standalone end-to-end test against the seed
├── evaluator_support/             # Trusted helpers (lifecycle, procfs CPU, YCSB, validity)
├── reference/seed_server.py       # Seed baseline / RESP2 reference
├── accuracy_checker/checker.py    # Correctness: candidate vs Redis oracle
└── benchmark/benchmark.py         # Scored YCSB orchestration (fetches YCSB on first run)
```
