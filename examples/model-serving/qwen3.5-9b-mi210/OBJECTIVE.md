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
and teacher-forced logprobs. Throughput counts only if that gate passes; do
not trade correctness for throughput, and do not retune its thresholds.

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
