# Qwen3.6 TraceLab serving on 1 AMD MI300X GPU

Build an OpenAI-compatible serving system for `Qwen/Qwen3.6-35B-A3B` on **1 AMD
MI300X GPU**. The candidate lives in the project root, outside trusted
`.vibesys/` inputs.

The project root holds a baseline engine, `engine/`, started by the project's
documented service command. It runs the BF16
checkpoint with Transformers, sharded over every visible GPU, prefilling one
request at a time and decoding running requests as one batch, with a snapshot
prefix cache. It meets the service contract below but is
unoptimized and is not a validated performance baseline. Making the ROCm serving
path fast, by changing or replacing any part of it, is the task. Keep that
service command working.

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

Expose streaming `/v1/completions` with exact requested token lengths,
`ignore_eos`, and accurate prompt, cached-prompt and output token usage for
Request Factory's session replay. Expose `/v1/chat/completions` for the service
checker's short chat probes, using a non-thinking default so answers fit their
small output budgets. Honor explicit template options, preserve supplied
reasoning/tool history, and make temperature-zero completions repeatable.

The configured service checker is shared by every task of this project and
checks sentinel echo, known answers, and greedy repeatability. These are
behavioral probes, **not numerical parity or proof of checkpoint identity**.
Correct hybrid state and routing remain requirements; a passing behavioral probe
does not authorize altered model execution. No full-size AMD run has validated
this example yet.

The server runs on the one MI300X GPU, reads the pinned checkpoint from the
operator's read-only Hugging Face cache offline, listens on `127.0.0.1`, keeps
the full 262,144-token context, and answers `GET /health` with a 2xx status once
it can serve requests, within 30 minutes of starting. The image it runs in and
everything it builds from are candidate code. The checker and the replay reach
the server only over HTTP.

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
accepted.
