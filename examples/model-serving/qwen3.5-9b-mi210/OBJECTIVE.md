# Objective: Qwen3.5-9B agentic-coding-session serving on 1x MI210

Maximize **serving throughput** for `Qwen/Qwen3.5-9B` (bf16) on **1x AMD
MI210** (gfx90a, 64 GB) on an agentic multi-turn coding-session workload,
while passing the accuracy checker. Expose an OpenAI-compatible
`/v1/completions` server (see "Interface contract").

`reference/` is a minimal, correct, unoptimized PyTorch engine and server to
start from. The ROCm optimization floor in
`resources/skills/serving-systems/references/platforms/rocm/floor.md`
(continuous batching, paged KV, prefix caching, chunked prefill) applies in
design. Its kernel-library specifics (AITER, CK, FP8) target MI300-class
gfx942; treat them as hypotheses to re-verify on gfx90a, per
`config/platforms/mi210.toml`.

## Candidate rules

- The candidate is a serving engine built from `reference/`. It lives in the
  top-level package `engine/`; trusted evaluation launches
  `python -m engine.server --model Qwen/Qwen3.5-9B --host 127.0.0.1 --port <port>`
  from the candidate root when `engine/server.py` exists, and the reference
  server otherwise.
- The engine may use the kernel and tensor libraries installed in the
  evaluation environment (for example torch, Triton, and the fla kernels), but
  it must not delegate serving to an existing serving framework (vLLM, SGLang,
  TGI, or similar), as a library or as a subprocess. Tuned vLLM
  (`benchmark/vllm_baseline.sh`) is the comparison baseline, so a wrapper
  would measure vLLM against itself.
- `reference/`, `accuracy_checker/`, and `benchmark/` are read-only trusted
  inputs.
- The `quick` and `full` benchmarks fail at their preflight unless the server
  reports a prefix-cache hit (`cached_tokens > 0`; see "Interface
  contract"), and their warmup sub-run must finish within 180 s.

## Hardware and model facts

- bf16 weights: 19.3 GB, leaving ~44.7 GB nominal for KV cache, Gated-DeltaNet
  (GDN) recurrent state, activations, and runtime overhead.
- 32 layers: 8 full-attention layers (KV cache 32 KiB/token) and 24 GDN
  linear-attention layers.
- GDN state: ~49 MiB/sequence, fixed regardless of context length. It limits
  concurrency at short contexts; KV limits it at long contexts.
- vLLM forces a 528-token attention KV block on this model/GPU (hybrid mamba
  page alignment: the attention page must be >= the mamba page). This is an
  observed constraint of vLLM's allocator, not a property of the model.
- Single-stream decode: ~66 tok/s on vLLM. The objective is aggregate
  throughput under concurrency.
- vLLM attention backends on this build/model/GPU: `ROCM_ATTN` (vLLM's
  default) and `TRITON_ATTN`. `ROCM_ATTN` cannot use its custom
  paged-attention kernel here and falls back to Triton through a slower
  dispatch path; `TRITON_ATTN` measured +21-33% output tok/s over it.
- A vLLM boot at `--gpu-memory-utilization 0.85 --max-model-len 4096`
  reported a GPU KV cache of 547,401 tokens; see `config/platforms/mi210.toml`
  for the capacity budget.

## Workload

An agentic multi-turn coding-session trace
(`text-generation-session-execution-v2`) replayed by Request Factory's
`session_runner`. Each session is one coding-agent conversation: round *k*'s
prompt is round *k-1*'s full context (a cache-eligible prefix) plus a fresh
chunk of input (the next user or tool message), and the server generates a
fresh completion. Sessions are replayed saturated at `--max-concurrency 128`.
See `README.md` for provenance, distributions, and how tool-wait time is
handled.

## Interface contract

- The candidate launches the server; it listens on `http://127.0.0.1:8000/v1`
  and serves the model name `Qwen/Qwen3.5-9B`.
- Endpoints: `GET /health`, `GET /v1/models`, `POST /v1/completions`.
  - `prompt`: string or list of token ids.
  - `max_tokens`, `temperature`, `ignore_eos`, `stream` (SSE), and
    `stream_options.include_usage`.
  - The final streamed usage block must include
    `prompt_tokens_details.cached_tokens` (vLLM:
    `--enable-prompt-tokens-details`). Report `0` honestly if the engine has
    no prefix caching; never fabricate a nonzero value. `quick`/`full` modes
    fail at `session_runner`'s prefix-cache preflight until the engine reports
    real cache hits (see `README.md` "Prefix-cache preflight").
  - The accuracy checker additionally needs the vLLM extensions
    `return_token_ids` and `echo` + `logprobs` + `return_tokens_as_token_ids`
    (see `accuracy_checker/README.md`).
  - Response shapes match vLLM's OpenAI-compatible server.

## Headline metric

`output_tokens_per_s`: output tokens generated in the measured window divided
by that window's wall-clock duration (`session_runner`'s
`output_token_throughput_per_s`, excluding the warmup sub-run). Any request
failure in the measured window fails the run. See `README.md` "Headline
metric" for the secondary metrics.

## Correctness

`accuracy_checker/` gates the candidate against HF transformers greedy output
and teacher-forced logprobs. It includes a cache-resume check: each gate
prompt's continuation is also generated as chained rounds, so a server with
prefix caching must resume correctly from the previous round's cached KV and
GDN state (see `accuracy_checker/README.md` "Cache-resume check").
Throughput counts only if the gate passes; do not trade correctness for
throughput, and do not retune its thresholds.

### Fast CPU check before submitting

A trusted accuracy evaluation takes about 2 minutes before its first result
(staging, model load, server start). Catch engine lifecycle bugs in 10 to 20 s
on the editor host first, which has no GPU. From the candidate root:

```bash
cpu_check/run.sh                      # any engine
cpu_check/run.sh --expect-cache-hits  # once the engine caches prefixes
```

It writes a randomly initialized tiny checkpoint of this architecture (8
layers: 6 GDN, 2 full attention; nothing to download), starts
`python -m engine.server --model <tiny dir> --device cpu` (the reference
server if `engine/server.py` does not exist), and replays two interleaved
3-round sessions whose prompts and outputs grow every round, comparing each
round with the in-process reference engine. It catches crashes on chained
rounds (for example "sequence length exceeds state capacity" from a cached
state that keeps the capacity of the request that created it, or from a
sequence length advanced twice per decode step), wrong outputs after a cache
hit, bad `cached_tokens`, and streaming mismatches. Each failure names its
session and round, then the first server exception and the server log tail.
The first run installs CPU torch into the user cache (about 40 s), outside the
candidate directory; later runs reuse it.

Keep `engine.server` accepting `--device cpu`, as the reference does, so the
check can run: give GPU-only kernels (fla, Triton, HIP graphs) the reference's
torch path when the device is CPU. Passing this check is necessary, not
sufficient: the accuracy checker on the 9B model stays the gate.

Numerics: weights and activations stay bf16. Weights, KV cache, and GDN
recurrent state are never quantized or stored below bf16 (e.g. no int8/fp8).
Outputs may differ from the reference only at rounding level, as judged by
the accuracy checker.
Computations the reference performs in fp32 (the zero-centered RMSNorm, the
GDN delta-rule recurrence and its state, rotary frequencies) stay fp32;
lowering their precision breaks this rule even when the accuracy checker
passes.

## No tuning to benchmark content

Optimizations must not depend on the benchmark corpus's token distribution or
on the trace's specific sessions, for example restricting a speculative-decoding
draft vocabulary to the corpus's frequent tokens. Each optimization must be
justified for general traffic of this workload shape (multi-turn sessions with
growing shared prefixes, at these prompt and output lengths).

## Held-out evaluation (anti-overfitting safeguard)

`quick` and `full` mode replay fixed prefixes (sessions 0-59 and 0-259) of one
6000-session synthetic trace, and every optimization on this task is tuned
against those same sessions. `benchmark/run.py --mode holdout` replays a
disjoint, fixed 260-session slice of the same trace (sessions 3000-3259; see
`benchmark/README.md` "Held-out evaluation" and `benchmark/slice_trace.py`)
that no `quick`/`full` run ever touches. Every mode's warmup sub-run also
replays its own disjoint pool (sessions 5000-5011), never a prefix of the
measured sessions -- see `README.md` "Warmup".

Binding rule: holdout is never used to tune a knob or choose between
candidates -- that stays on `quick`/`full`. It is run only at milestones (e.g.
a parity or win claim vs. tuned vLLM), always paired with tuned vLLM
(`benchmark/vllm_baseline.sh`) on the same node in the same job. A claimed
win or parity result must hold on holdout too; a `full`-mode win that does not
reproduce on holdout is evidence of overfitting to the tuned sessions, not
noise, and must not be reported as a validated result.
