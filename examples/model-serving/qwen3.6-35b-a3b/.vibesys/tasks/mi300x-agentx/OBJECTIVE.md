# Qwen3.6 AgentX serving on 1 AMD MI300X GPU

Build an OpenAI-compatible serving system for `Qwen/Qwen3.6-35B-A3B` on
**1 AMD MI300X GPU**. This project supplies no candidate engine or validated
AMD baseline. Implementing and optimizing the ROCm serving path is the task.
The candidate lives in the project root, outside trusted `.vibesys/` inputs.

Keep BF16 weights on the single allocated GPU. Optimize fused expert kernels,
continuous batching, prefix caching and recurrent-state management without
cross-GPU communication.

## Model and hardware contract

Use the BF16 checkpoint in `reference/meta.json`, including its pinned tokenizer
and chat template. The model has 35B total and about 3B active parameters,
256 experts with eight routed and one shared expert, and 40 hybrid layers:
30 Gated DeltaNet layers and ten gated-attention layers. Preserve routing,
convolution and recurrent state, positional encoding, trained weights, and the
262,144-token context capacity. Do not quantize, substitute a smaller model,
skip experts, or change precision to improve the measurement.

Serve the text-only coding workload; no vision throughput is scored. Preserve
all text-model weights and the checkpoint's tokenizer/template semantics.
PyTorch and Transformers may supply weight loading and reference operations;
the candidate must own its serving path rather than proxy an existing server.
Do not use extra GPUs, another machine, or a hosted model. Host-side cache
offload is allowed within the allocation, with its costs inside measurement.
See [the project README](../../../README.md) for AMD environment setup.

## Correctness and service interface

Expose streaming `/v1/chat/completions` for AgentX, preserving reasoning and
tool-call history, the pinned chat template, requested output lengths,
`ignore_eos`, and final token usage. Also expose streaming `/v1/completions`
for the existing service checker. Temperature-zero requests must be repeatable.
The short-answer correctness probes require a non-thinking default; preserve
explicit template options and any reasoning history supplied by AgentX.

Run the configured service checker before benchmarking. It is shared with the
existing H100 task and checks sentinel echo, known answers, and greedy
repeatability. These are behavioral probes, **not numerical parity or proof of
checkpoint identity**. Correct hybrid state and routing remain requirements;
a passing behavioral probe does not authorize altered model execution.
No full-size AMD run has validated this example yet.

## AgentX workload and objectives

Use the trusted `agentx-v1` evaluator. Full mode replays the date-pinned June 21
coding corpus with parent/subagent dependencies, four concurrent session trees,
seed 42, trajectory start ratios 0.25–0.75, ten warmup requests per lane and
900 seconds of profiling. Child agents may add simultaneous requests.
Preserve recorded pacing, output lengths, streaming and first-turn cache busting.

Optimize output tokens/s (maximize), TTFT p50/p90 and TPOT p50/p90 in
milliseconds (minimize), with 3% relative noise tolerance. P90 request latency
is diagnostic. Smoke mode is unscored and cannot produce an accepted result.
Failed requests, fewer output tokens, changed traces or a lower context cap
are not improvements. Maintain separate frontiers for each hardware task;
the H100 fixed-text Request Factory task has a different workload and metrics.
AgentX measures serving performance, not coding-agent task success.
