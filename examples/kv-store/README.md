# KV Store Target for VibeServe

A non-persistent, in-memory KV store optimized by VibeServe's multi-agent loop.
The agent starts from a minimal Python RESP2 server (~12k ops/sec) and iteratively
optimizes it, gated by correctness (vs Redis oracle) and benchmarked by real YCSB.

## Prerequisites

```bash
# Python 3.11+
python3 --version

# Java 8+ (required by YCSB)
java -version
# Install: brew install openjdk (macOS) / apt install default-jre (Linux)

# Redis (used as correctness oracle by the accuracy checker)
redis-server --version
# Install: brew install redis (macOS) / apt install redis-server (Linux)

# Claude Code (drives the agent loop)
claude --version
```

## Setup

```bash
# 1. Clone and enter repo
git clone https://github.com/RockingMat/vibe-serve.git
cd vibe-serve
git checkout kv-store-target

# 2. Install Python dependencies
uv sync

# 3. Create config (no API keys needed for --cli-provider claude)
cp agent.toml.example agent.toml

# 4. Install YCSB Redis binding
cd examples/kv-store/benchmark
curl -LO https://github.com/brianfrankcooper/YCSB/releases/download/0.17.0/ycsb-redis-binding-0.17.0.tar.gz
tar -xzf ycsb-redis-binding-0.17.0.tar.gz
mv ycsb-redis-binding-0.17.0 ycsb
rm ycsb-redis-binding-0.17.0.tar.gz
cd ../../..

# 5. Verify standalone (optional)
cd examples/kv-store
python3 reference/seed_server.py 6399 &
python3 accuracy_checker/checker.py --port 6399
python3 benchmark/benchmark.py --port 6399
kill %1
cd ../..
```

## Run

```bash
vibe-serve --outer-loop agent \
  --ref examples/kv-store/reference \
  --acc-checker examples/kv-store/accuracy_checker \
  --bench examples/kv-store/benchmark \
  --exp-name kv-store-opt \
  --backend cpu \
  --agent-backend cli --cli-provider claude \
  --max-rounds 6 \
  --modality kv_store \
  --interface service \
  --domain examples/kv-store/kv-store.md \
  --no-skills \
  --git-tracking
```

`--interface service` lets the agent implement the store in any language (it is
judged only over the RESP2 wire); `--no-skills` runs with no skill library.

Each round produces a git commit in `exp_env/<name>/workspace/`. Only rounds
that pass the accuracy checker advance.

## What happens

1. **Orchestrator** reads the objective and decides what to optimize this round
2. **Implementer** (Claude Code) edits the server code in the workspace
3. **Judge** starts the server, runs `checker.py` and `benchmark.py`, passes/fails
4. **Profiler** (optional) runs py-spy to identify bottlenecks for next round

## Expected trajectory

| Round | Optimization | ~ops/sec |
|-------|-------------|----------|
| 1 | Baseline (asyncio + dict) | 12k |
| 2 | uvloop + TCP_NODELAY | 25-40k |
| 3 | Pipelining (batch parse/respond) | 50-80k |
| 4 | Protocol API instead of StreamReader | 80-120k |
| 5+ | C extension for RESP parsing | 150-200k |

## Files

```
examples/kv-store/
├── README.md              # This file
├── OBJECTIVE.md           # Target spec (read by orchestrator)
├── requirements.txt       # redis>=5.0.0
├── run_test.sh            # Standalone end-to-end test
├── reference/
│   └── seed_server.py     # Seed implementation (starting point)
├── accuracy_checker/
│   └── checker.py         # Correctness: candidate vs Redis oracle
└── benchmark/
    ├── benchmark.py       # Performance: YCSB wrapper
    ├── ycsb/              # YCSB installation (gitignored)
    └── .gitignore
```
