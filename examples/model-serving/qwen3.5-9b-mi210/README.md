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
    ├── run.py                # --mode {smoke,quick,full,holdout}, wraps session_runner
    ├── test_run.py           # unit tests: mode/trace resolution, digest + disjointness checks
    ├── traces/               # checked-in session slices + full-trace manifest
    ├── slice_trace.py        # cuts traces/ out of the full tracegen output
    ├── fetch_corpus.py       # downloads and verifies the token corpus
    └── vllm_baseline.sh      # tuned vLLM launch (external comparison point)
```

## Run it

VibeSys runs the accuracy checker against the candidate's server and
`benchmark/run.py --mode quick` through the pinned
`vibesys-evaluator-request-factory` package. Cargo-tool evaluator packages
require the Local run environment.

```bash
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

`run.py` needs no setup: by default it replays the checked-in
`benchmark/traces/coding_session_0000-0259.csv` (checked against a pinned
sha256 and row count) and reads the corpus that `fetch_corpus.py` builds.
`--trace`/`--text-file`, or `$QWEN35_BENCH_ASSETS` naming a directory with
`coding_session_synthetic.csv` and `corpus.txt`, override both. The tokenizer
comes from `--tokenizer` or the HF cache (`$HF_HOME`, default
`~/.cache/huggingface`). `smoke` needs only the trace.

- **Trace.** The committed slices are sessions 0-259 (every session `quick`
  and `full` measure; `--max-items` takes a prefix), 3000-3299 (a disjoint
  range held out from tuning -- `holdout` measures the first 260 of these,
  see "Held-out evaluation" below), and 5000-5011 (the warmup pool shared by
  every mode, see "Warmup" below). `slice_trace.py` cuts them from the
  full 6000-session trace, shifting arrivals so each slice starts at 0 (unused
  under saturated replay). The full trace (manifest:
  `traces/coding_session_synthetic.manifest.json`; sha256
  `769f85b9c834f94b637086862a40882ea96e5d70aec8646b9f69a1315eaa1b1a`) regenerates
  deterministically with Request Factory's `tracegen` at the pinned revision
  (`cargo build --release --bin tracegen --features runtime`):

  ```bash
  tracegen synthetic --out coding_session_synthetic.csv \
    --sessions 6000 --rounds 'uniform:3..9' \
    --input-len 'lognormal:545,0.69' --output-len 'lognormal:177,0.45' \
    --tool-wait-ms 'lognormal:277,0.59' --compaction-probability 0.0 \
    --arrival-rate 5.0 --arrival-pattern poisson --seed 42
  ```

  Expected: 6000 sessions, 35,949 rounds, 112,990,467 prompt tokens, planned
  prefix-hit rate 0.78.
- **Corpus.** Eight Project Gutenberg ebooks concatenated in a fixed order,
  plus a second copy of Pride and Prejudice (5,979,406 bytes, ~1.46M tokens).
  `fetch_corpus.py` downloads them from pinned URLs, checks each file and the
  result against pinned sha256 digests, and caches the output under
  `~/.cache/vibesys/` (`$XDG_CACHE_HOME`). Run it ahead of time on nodes
  without internet access. `session_runner` draws token ids from the corpus
  at the lengths the trace specifies; the content only needs to be plausible
  text, since the benchmark measures throughput, not output quality.

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
| `holdout` | 260 | ~1,450 (of 3000-3299) | ~510-570 s (same shape as `full`) |

`vibesys.input.toml` runs `quick`. Confirm a candidate on `full` before
accepting it: `full` sustains enough KV pressure to expose prefix-cache
eviction that `quick` does not, and quick-mode deltas under ~5% are within
noise.

## Warmup

Every mode's warmup sub-run (12 sessions, kernel compile, HIP/CUDA graph
capture, allocator warm-up -- excluded from the reported metric) replays the
checked-in `traces/coding_session_5000-5011.csv`, disjoint from every mode's
own measured range (0-259 and 3000-3259).

**This was not always true.** Before this file's warmup/measured split,
`quick`/`full` warmed up on sessions 0-11 of their *own* measured trace, so
those 12 sessions started the measured sub-run already cache-warm (prefix
cache, any per-session engine state) in a way the other 48-248 measured
sessions never were. Numbers recorded before this change are not directly
comparable to numbers recorded after it, in either direction: a candidate
whose advantage was concentrated in the pre-cached first 12 sessions would
look relatively worse post-fix, and vice versa. The task ledger notes which
side of this change a given entry falls on.

## Held-out evaluation

`quick` (sessions 0-59) and `full` (sessions 0-259) are prefixes of the same
trace, and every optimization on this task is tuned or gate-checked against
those sessions. `--mode holdout` replays a disjoint, fixed 260-session slice
of the same seed-42 trace instead: sessions 3000-3259 (the first 260 sessions
of the checked-in `traces/coding_session_3000-3299.csv`; the file's remaining
sessions, 3260-3299, are unused headroom, not part of any measured or warmup
range).

**Binding usage rule:**

- Holdout is **never** used to tune a knob, choose between candidate designs,
  or decide whether an experiment worked. That stays on `quick`/`full`.
- Holdout is run only at milestones (e.g. a parity or win claim vs. tuned
  vLLM), always paired with tuned vLLM (`benchmark/vllm_baseline.sh`) on the
  same node in the same job.
- A claimed win or parity result must hold on holdout too. A `full`-mode win
  that does not reproduce on holdout is a real overfitting signal, not noise
  to explain away.
- Report holdout's ratio vs. tuned vLLM and its server-vs-planned prefix-hit
  rate alongside the same numbers on `full` mode, to flag any divergence
  between the two.

An explicit `--trace` (or `$QWEN35_BENCH_ASSETS`) override is taken as-is for
every mode, including `holdout`: `session_runner` only truncates a *prefix*,
so handing `holdout` the raw, unsliced 6000-session trace this way would
silently replay sessions 0-259 again (`quick`/`full`'s own range), not a held
out set. Pass a trace already cut to start at session 3000 (e.g. with
`slice_trace.py`) if overriding holdout's input.

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
On vLLM v0.3.1.dev190+g3df4ae153 (ROCm nightly), on the current harness
(disjoint warmup, same launch config as `benchmark/vllm_baseline.sh`):

| Mode | Output tok/s | Server prefix-hit rate | Planned |
|:--|--:|--:|--:|
| `quick` | ~500 (495.6-505.2) | 0.648 | 0.769 |
| `full` | ~576 | 0.492 | 0.773 |
| `holdout` | ~532 | 0.442 | 0.783 |
