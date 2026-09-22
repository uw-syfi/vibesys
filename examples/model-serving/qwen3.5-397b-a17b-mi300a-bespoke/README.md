# Qwen3.5-397B-A17B Multi-Turn, From Scratch, 4x MI300A

Bespoke-system bundle: the agent builds its own serving engine (no SGLang, vLLM,
or TensorRT-LLM) for `amd/Qwen3.5-397B-A17B-MXFP4` and is scored on a Pareto
frontier over aggregate throughput (`total_token_throughput`, max) and
multi-turn tail latency (`p95_ttft_turn2plus_ms`, min) on a multi-turn chat
workload. The workload is the v4 benchmark of the SGLang multi-turn task (PR
#421), unchanged. See `OBJECTIVE.md` for the task, the two metrics and how
throughput saturates, the disallowed-engine-code rule, and the candidate
contract.

## Run

```bash
export MODEL_PATH=/path/to/Qwen3.5-397B-A17B-MXFP4
vibesys --input examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke \
  --runs-dir /work/vibesys-runs \
  --run-environment skypilot --backend rocm --cluster-profile <profile>
```

The execution image is described in
`examples/model-serving/images/rocm-mi30x-engine-free/`.

## Layout

| Path | Role |
| --- | --- |
| `vibesys.input.toml`, `objectives.toml` | Input manifest; objectives `total_token_throughput` (max) and `p95_ttft_turn2plus_ms` (min), read over `benchmark.result_protocol = 2` |
| `OBJECTIVE.md` | Task, validity rules, candidate contract |
| `benchmark/run.py` | Workload driver; writes the result-protocol stream (`--vs-output`) and the human-readable metrics JSON (`--output-json`); hard-fails on any dropped, truncated, or errored turn |
| `benchmark/vs_protocol.py` | Writer for the evaluator result protocol v2 (`sdk/vs-evaluator/PROTOCOL.md`) |
| `benchmark/launcher.py` | Starts and stops `python3 server.py`, polls `/health` |
| `benchmark/test_run.py` | Hermetic tests (fake clock and server) |
| `accuracy_checker/checker.py` | 13 probes plus greedy-token pins; see its README |
| `accuracy_checker/make_pins.py` | Generates `reference/pins.json` from a transformers forward |
| `reference/` | Reference modeling code and `pins.json` |
| `requirements.txt` | Pure-python extras; torch, triton, AITER come from the image |

## Checks without a GPU

```bash
D=examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke
uv run pytest $D/benchmark/test_run.py -q --no-cov -p no:tach
uv run pytest $D/accuracy_checker/test_checker.py -q --no-cov -p no:tach
uv run vibesys validate $D
```

Before the first real run, generate `reference/pins.json` once on the cluster
(see `accuracy_checker/README.md`); the accuracy gate fails without it.

## Seed status

The seed (`server.py`, `model.py`, `mxfp4.py`, `weights.py`) is a plain PyTorch
implementation written from the reference modeling code: static caches, one
request at a time, layers split across the GPUs, experts kept in MXFP4 and
dequantized per call, a per-token Python loop for the DeltaNet rule. It matches
the transformers reference on a tiny random model on CPU (`seed_tests/`).
Loading the real checkpoint, the 4-GPU split, and its speed on ROCm have not
been run. Expect the first measurement to be very slow; the accuracy gate is
the first thing to confirm on the cluster.

## Baseline protocol

Success is judged against the optimized SGLang tree from the #421 campaign,
not against the seed. Compare paired runs on the same node:

1. Per node, run the SGLang tree and the candidate back to back on the same
   allocation. Alternate which goes first across nodes.
2. Each side runs 5 repetitions of `benchmark/run.py`. The first repetition is
   a warmup and is discarded.
3. Flush caches before every repetition. For SGLang that is its flush-cache
   endpoint; for a candidate, restart the server or otherwise start each
   repetition with no cross-repetition prefix state. Without this, later
   repetitions inherit prior-turn state that the workload does not offer.
4. Compare the median of the 4 measured repetitions of
   `p95_ttft_turn2plus_ms` per node.

The candidate passes when, on every paired node, it first matches the SGLang
tree within noise, then exceeds it (lower p95 TTFT). Run-to-run spread on one
node was about 6 percent for identical trees, and identical trees differed by
tens of percent across nodes, so unpaired or cross-node comparisons are not
evidence.

Compare `total_token_throughput` the same way, and report `mean_tpot_ms`
alongside. Both objectives are compared per node: a TTFT gain that costs
throughput is a frontier tradeoff, not a pass.
