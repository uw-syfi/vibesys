# Qwen3.6 TraceLab serving on 1 AMD MI300X GPU

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

Expose streaming `/v1/completions` with exact requested token lengths,
`ignore_eos`, and accurate prompt, cached-prompt and output token usage for
Request Factory's session replay. Expose `/v1/chat/completions` for the service
checker's short chat probes, using a non-thinking default so answers fit their
small output budgets. Honor explicit template options, preserve supplied
reasoning/tool history, and make temperature-zero completions repeatable.

Run the configured service checker before benchmarking. It is shared with the
existing H100 task and checks sentinel echo, known answers, and greedy
repeatability. These are behavioral probes, **not numerical parity or proof of
checkpoint identity**. Correct hybrid state and routing remain requirements;
a passing behavioral probe does not authorize altered model execution.
No full-size AMD run has validated this example yet.

## TraceLab recorded workload and objectives

Replay the trusted recorded sample: 48 sessions and 303 rounds, published as
[`multi_session_large.csv`](https://github.com/uw-syfi/request-factory/blob/89dce4a64ae12e7084fbe464ddda882fa0a4c482/examples/multi_session_large.csv) in Request Factory revision
`89dce4a64ae12e7084fbe464ddda882fa0a4c482`, materialized from TraceLab rounds.
This is the recorded sample, not the larger statistically resampled Qwen trace
or the complete TraceLab public release. The adapter verifies its SHA256.

Preserve every row, session dependency, growing prefix, output length and tool
wait. Saturated replay ignores arrival times and permits eight active sessions;
no sessions are dropped on context overflow. Preserve real prefix-cache usage
accounting so the runner's prefix-cache preflight can pass. The token pool is
synthetic coding text because these sanitized traces describe lengths and
topology rather than private prompt content. Setup occurs before measurement;
throughput and percentiles come from the runner's native measured interval.
No separate warmup is supplied beyond the runner's preflight; keep that policy
fixed when comparing candidates. This small sample can be sensitive to cold
kernels; it is a separate frontier from the 15-minute AgentX task.

Optimize output tokens/s (maximize), TTFT p50/p90 and TPOT p50/p90 in milliseconds
(minimize), with 3% relative noise tolerance. A failed round, missing metric,
wrong token count or incomplete replay fails measurement. No dry-run result is
accepted. See [the TraceLab adapter](../../shared/tracelab/README.md).
