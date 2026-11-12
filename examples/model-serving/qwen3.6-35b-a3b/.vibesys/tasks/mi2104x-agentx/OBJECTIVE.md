# Qwen3.6 AgentX serving on 4 AMD MI210 GPUs

Build an OpenAI-compatible serving system for `Qwen/Qwen3.6-35B-A3B` on **4 AMD
MI210 GPUs**. The candidate lives in the project root, outside trusted
`.vibesys/` inputs.

The project root holds a baseline engine, `engine/`, started by the project's
documented service command. It runs the BF16
checkpoint with Transformers, sharded over every visible GPU, prefilling one
request at a time and decoding running requests as one batch, with a snapshot
prefix cache. It meets the service contract below but is
unoptimized and is not a validated performance baseline. Making the ROCm serving
path fast, by changing or replacing any part of it, is the task. Keep that
service command working.

The server is built from the candidate's commit, started by the documented
service command on the four MI210 GPUs, and must answer `GET /health` within 30 minutes.
Weights come only from the operator's read-only model cache, offline, and are
never baked into the image. The checker and the AgentX replay run separately,
without GPUs, and reach the server over HTTP.

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
PyTorch and Transformers may supply weight loading and model operations, as
they do in the baseline; the candidate must own its serving path rather than
proxy an existing server.
Do not use extra GPUs, another machine, or a hosted model. Host-side cache
offload is allowed within the allocation, with its costs inside measurement.

## Correctness and service interface

Expose streaming `/v1/chat/completions` for AgentX, preserving reasoning and
tool-call history, the pinned chat template, requested output lengths,
`ignore_eos`, and final token usage. Also expose streaming `/v1/completions`
for the existing service checker. Temperature-zero requests must be repeatable.
The short-answer correctness probes require a non-thinking default; preserve
explicit template options and any reasoning history supplied by AgentX.

Run the configured service checker before benchmarking. Every task of this
project shares it; it checks sentinel echo, known answers, and greedy
repeatability. These are behavioral probes, **not numerical parity or proof of
checkpoint identity**. Correct hybrid state and routing remain requirements;
a passing behavioral probe does not authorize altered model execution.
No full-size AMD run has validated this example yet.

## AgentX workload and objectives

The trusted task image replays AgentX with `agentx-harness==1.0.7`, the
recipe of the earlier `agentx-v1` evaluator. Full mode replays the date-pinned June 21
coding corpus with parent/subagent dependencies, four concurrent session trees,
seed 42, trajectory start ratios 0.25–0.75, ten warmup requests per lane and
900 seconds of profiling. Child agents may add simultaneous requests.
Preserve recorded pacing, output lengths, streaming and first-turn cache busting.

Optimize output tokens/s (maximize), TTFT p50/p90 and TPOT p50/p90 in
milliseconds (minimize), with 3% relative noise tolerance. P90 request latency
is diagnostic. Smoke mode is unscored and cannot produce an accepted result.
Failed requests, fewer output tokens, changed traces or a lower context cap
are not improvements. Maintain separate frontiers for each hardware task and
workload.
AgentX measures serving performance, not coding-agent task success.
