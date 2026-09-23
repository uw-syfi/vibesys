# qwen3.5-9b-mi210: agentic-coding-session serving on 1x MI210

VibeSys input bundle: maximize serving throughput of `Qwen/Qwen3.5-9B` (bf16)
on 1x AMD MI210, measured by replaying an agentic multi-turn coding-session
trace with Request Factory's `session_runner`. See
[`OBJECTIVE.md`](OBJECTIVE.md) for the objective, hardware facts, and
interface contract.

## Layout

```
qwen3.5-9b-mi210/
├── vibesys.input.toml        # manifest: request-factory-adapter entrypoint, headline metric
├── OBJECTIVE.md              # what to optimize, hardware facts, interface contract
├── pyproject.toml            # uv environment for the checker and reference engine
├── config/platforms/mi210.toml  # accelerator facts and capacity budget
├── reference/                # minimal PyTorch engine + OpenAI-compatible server
├── accuracy_checker/         # HF-golden gate (golden.json checked in)
└── benchmark/
    ├── run.py                # --mode {smoke,quick,full}, wraps session_runner
    └── vllm_baseline.sh      # tuned vLLM launch (external comparison point)
```

## Run it

VibeSys runs the accuracy checker against the candidate's server and
`benchmark/run.py --mode quick` through the pinned
`vibesys-evaluator-request-factory` package. Cargo-tool evaluator packages
require the Local run environment.

```bash
export QWEN35_BENCH_ASSETS=/path/to/assets   # see "Benchmark inputs"
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/qwen3.5-9b-mi210
```

Each piece by hand, from the bundle root on a node with the MI210:

```bash
# reference server (reads weights from the local HF cache)
HF_HUB_OFFLINE=1 uv run python -m reference.server --model Qwen/Qwen3.5-9B --port 8000

# accuracy gate against a running server (exit 0 = PASS)
uv run python accuracy_checker/checker.py --base-url http://127.0.0.1:8000

# benchmark (session_runner from request-factory rev 118da613)
python3 benchmark/run.py --mode quick \
  --request-factory-engine /path/to/session_runner \
  --base-url http://127.0.0.1:8000/v1 --output-json result.json
```

`pyproject.toml` pulls torch from the PyTorch ROCm 6.4 wheel index on Linux.
See [`reference/README.md`](reference/README.md) and
[`accuracy_checker/README.md`](accuracy_checker/README.md) for the engine
surface and the gate policy.

## Benchmark inputs

The trace and token corpus are too large to commit. `run.py` reads them from
`--trace` and `--text-file`, or from `$QWEN35_BENCH_ASSETS/coding_session_synthetic.csv`
and `$QWEN35_BENCH_ASSETS/corpus.txt`, and errors if neither is set. The
tokenizer comes from `--tokenizer` or the HF cache (`$HF_HOME`, default
`~/.cache/huggingface`). `smoke` needs only the trace.

- **Trace.** Regenerate it deterministically with Request Factory's `tracegen`
  at the pinned revision (`cargo build --release --bin tracegen --features runtime`):

  ```bash
  tracegen synthetic --out coding_session_synthetic.csv \
    --sessions 6000 --rounds 'uniform:3..9' \
    --input-len 'lognormal:545,0.69' --output-len 'lognormal:177,0.45' \
    --tool-wait-ms 'lognormal:277,0.59' --compaction-probability 0.0 \
    --arrival-rate 5.0 --arrival-pattern poisson --seed 42
  ```

  Expected: 6000 sessions, 35,949 rounds, 112,990,467 prompt tokens, planned
  prefix-hit rate 0.78.
- **Corpus.** Any plain-text corpus of at least ~1.5M tokens (the measured
  runs used several concatenated public-domain books). `session_runner` draws
  token ids from it at the lengths the trace specifies; the content does not
  need to be code, since the benchmark measures throughput at these shapes,
  not output quality.

## Workload

Each trace row is one round of one session, with `prefix_len` (tokens carried
from the previous round's context) and `input_len` (fresh tokens appended).
The distributions are fit to a real coding-agent recording:
`request-factory/examples/multi_session_large.csv` at the pinned revision (48
sessions, 304 rounds, materialized from
[TraceLab](https://github.com/uw-syfi/TraceLab) session rounds). That file is
too small to saturate an MI210 for minutes, so its statistics were fit and
resampled at scale:

| Parameter | Distribution | Real trace |
|:--|:--|:--|
| rounds per session | `uniform:3..9` | min 3, median 6.5, max 9 |
| fresh input tokens/round | `lognormal:545,0.69` | median 561 |
| output tokens/round | `lognormal:177,0.45` | median 177 |
| tool wait between rounds | `lognormal:277,0.59` ms | median 285 ms, ~16% exactly zero |
| context compaction | probability 0.0 | 0 of 255 non-first rounds reset context |

Max context reached by one session is 15,733 tokens (p50 2,773, p95 7,153),
which sets `--max-model-len 16384`. Replay uses `--arrival-mode saturated`,
so the trace's recorded arrival times are unused. `quick` and `full` replay
session-count prefixes of the same trace (`--max-items`).

**Tool waits.** `tool_wait_after_ms` is a client-side sleep between a
session's rounds, and `--max-concurrency` counts sessions, holding a slot
through those sleeps. In principle this under-fills the server by the
tool-wait share of a round's cycle time. Measured against vLLM on `quick`,
that share is ~2% (mean round 16.7 s vs mean tool wait 0.32 s), so no
concurrency correction is applied. Recompute it if round latency drops by an
order of magnitude.

## Prefix-cache preflight

For any `text-generation-session-execution-v2` trace, `session_runner` runs
an unconditional preflight: it sends one probe prompt twice and requires the
second response to report `cached_tokens > 0`. No flag disables it for
session traces. Consequences:

- `smoke` never reaches the preflight. It runs `session_runner --dry-run`
  (static trace validation, no server contact) plus direct `GET /health`,
  `GET /v1/models`, and one `POST /v1/completions` against the server.
- `quick`/`full` against a server that reports real cache hits (vLLM, or a
  candidate with prefix caching) run normally.
- `quick`/`full` against a server that always reports `cached_tokens: 0`
  (the reference engine) fail at the preflight with `session_runner`'s own
  message. This is the intended outcome, not a harness bug.

Every mode first checks that `/v1/models` lists the expected model, so a run
against an unrelated process on the same port fails fast.

## Modes

Wall clock against the tuned vLLM baseline at `--max-concurrency 128`, after
a 12-session warmup sub-run that is excluded from the metric:

| Mode | Sessions | Rounds | Wall clock |
|:--|--:|--:|:--|
| `smoke` | 2 | - | ~5 s (dry run + one completion) |
| `quick` | 60 | 334 | ~130-160 s |
| `full` | 260 | ~1,450 | ~510-570 s |

`vibesys.input.toml` runs `quick`. Confirm a candidate on `full` before
accepting it: `full` sustains enough KV pressure to expose prefix-cache
eviction that `quick` does not, and quick-mode deltas under ~5% are within
noise.

## Headline metric

`output_tokens_per_s`: `session_runner`'s
`replay.common.output_token_throughput_per_s` for the measured sub-run. It
is chosen over request throughput because requests/s rewards short outputs
without adding capacity. `run.py` also reports `total_tokens_per_s`,
`request_throughput_per_s`, TTFT and TPOT percentiles, server-reported vs
planned prefix-hit rate, and step counts. Any failed request in the measured
window makes `run.py` exit nonzero without writing a metric.

## vLLM comparison point

`benchmark/vllm_baseline.sh` launches vLLM in an apptainer image. It needs
`VLLM_SIF` (a ROCm vLLM image) and `VLLM_CACHE_DIR` (writable cache root with
the model already in `$VLLM_CACHE_DIR/hf`):

```bash
VLLM_SIF=/path/to/vllm-openai-rocm.sif VLLM_CACHE_DIR=/path/to/cache \
  benchmark/vllm_baseline.sh 8000
```

Tuned config: `--attention-backend TRITON_ATTN --gpu-memory-utilization 0.95
--max-num-seqs 256 --max-num-batched-tokens 16384 --max-model-len 16384
--async-scheduling --enable-prefix-caching --enable-prompt-tokens-details`.
On vLLM v0.3.1.dev190+g3df4ae153 (ROCm nightly) it measured ~500 output tok/s
on `quick` and 580.9 on `full` (server prefix-hit rate 0.49 vs 0.78 planned),
against 390.7 on `full` for vLLM's defaults.
