# Objective - Qwen3.5-397B-A17B From-Scratch 4xB200 Serving

Build an OpenAI-compatible serving system for `Qwen/Qwen3.5-397B-A17B` from
scratch across 4 NVIDIA B200 192GB GPUs. No serving engine is provided: there
is no vLLM or SGLang checkout. Implement the whole
stack yourself: weight loading and sharding across the 4 GPUs (tensor
parallelism, expert parallelism, pipeline parallelism, or a hybrid), FP8
grouped-GEMM MoE experts and dequant, expert routing/top-k dispatch and the
all-to-all communication it requires, the hybrid attention (Gated DeltaNet
linear-attention state plus full-attention KV cache), a request scheduler,
and the OpenAI-compatible HTTP server. `torch` and `transformers` are
available as utilities (weight loading, tokenizer, reference ops), not as a
serving engine.

Qwen3.5-397B-A17B is a sparse Mixture-of-Experts model: 397B total parameters
but only ~17B activated per token (512 experts, 10 routed + 1 shared). It uses
a hybrid architecture, mixing Gated DeltaNet (linear/recurrent attention)
layers with sparse MoE layers; only a subset of layers carry a standard KV
cache, and the Gated DeltaNet layers carry recurrent state instead. The model
is natively multimodal (early-fusion), but this task serves the text
`/v1/completions` path only. Native context is 262K tokens, extensible to
~1M.

At bf16 the weights (~794 GB) do not fit 4xB200 (768 GB total). The candidate
must serve the model in a lower-precision format such as FP8 (~397 GB), which
fits with headroom for KV cache and recurrent state; the specific
quantization approach and calibration/conversion strategy are the
candidate's choice. The implementation must also shard the model across all
4 GPUs and handle the all-to-all expert-dispatch communication this
requires.

This is a combined capacity- and MoE-efficiency workload: fitting weights
into HBM, expert routing/dispatch across 512 experts under a ~17B active
budget, FP8 grouped-GEMM MoE kernels on Blackwell, and managing both the
full-attention KV cache and the Gated DeltaNet recurrent state, all under
concurrent decode, with none of it delegated to an existing serving engine.

## Workload

Run the Request Factory benchmark through the bundle adapter. The evaluator
may pass a different serving URL:

```bash
python3 benchmark/benchmark.py --request-factory-engine <RF_ENGINE> --url <SERVER_URL>
```

Default load:

- `/v1/completions`
- streaming responses
- 64 independent requests, saturated replay, maximum concurrency 48
- exact 2048-token prompts and `max_tokens = 512`
- `temperature = 0`

Exact token lengths replace the legacy approximate prompt word count, and the
fixed trace replaces its 90-second closed-loop duration. The workload still
stresses multi-GPU expert-parallel communication, FP8 MoE kernel efficiency,
KV-cache and recurrent-state management, scheduling, and concurrent decode.
Candidates must not reduce request count, prompt/output lengths, or concurrency
to improve the score.

## Metrics

Pareto axes:

- `output_token_throughput_per_s`: RF-measured output tokens per second,
  maximize.
- `p90_latency_ms`: RF-measured end-to-end request latency in milliseconds,
  minimize.

These intentionally replace legacy `aggregate_throughput` (which counted
nonempty SSE chunks as tokens) and p99 latency. Old and new scores are not
directly comparable. The scalar headline metric is
`output_token_throughput_per_s`.

## Correctness

The accuracy checker drives the running server over HTTP with three
reference-free gates: sentinel echo, known-answer, and greedy determinism. It
requires a real prompt-conditioned Qwen3.5-397B-A17B forward pass. Canned
responses, prompt echoing, skipped model execution, or non-deterministic
temperature-0 decoding fail the task.
