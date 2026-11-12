# Qwen3.6 TraceLab concurrency sweep on one AMD MI250 node

Build an OpenAI-compatible serving system for `Qwen/Qwen3.6-35B-A3B` on **one
AMD MI250 node: 4 MI250 cards, 8 ROCm devices**, under load that rises until
the engine saturates. Each MI250 card holds two graphics compute dies (GCDs);
each GCD is a separate ROCm device (gfx90a, CDNA2) with its own 64 GB of HBM2e,
so the allocation is 8 devices and 512 GB of aggregate HBM, never one 128 GB
device per card. The candidate lives in the project root, outside trusted
`.vibesys/` inputs.

The project root holds a baseline engine, `engine/`, started by the project's
documented service command. It runs the BF16
checkpoint with Transformers, sharded over every visible GPU, prefilling one
request at a time and decoding running requests as one batch, with a snapshot
prefix cache. It meets the service contract below but is unoptimized and is not
a validated performance baseline. Making the ROCm serving path fast, by changing
or replacing any part of it, is the task. Keep that service command working.

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

The configured service checker is shared by every task of this project and checks
sentinel echo, known answers, and greedy repeatability. These are behavioral
probes, **not numerical parity or proof of checkpoint identity**. Correct hybrid
state and routing remain requirements; a passing behavioral probe does not
authorize altered model execution.

The server runs on the node's eight GCDs, reads the pinned checkpoint only from
the operator's read-only model cache offline (weights are never baked into the
image), keeps the full 262,144-token context, and answers `GET /health` with a
2xx status once it can serve requests, within 30 minutes of starting. The checker and the replay reach
the server only over HTTP.

## TraceLab concurrency-sweep workload and objectives

The engine is measured under load that rises until it saturates, on real
TraceLab coding-agent traffic. The trace holds 2,000 sessions and 42,439 rounds
sampled from the public TraceLab release, in Request Factory's
`session-execution-v2` format. Sessions join part-way through their original
run, so each one's first prompt is cold: the median first prompt is about 46k
tokens, and contexts reach 261k tokens. Each later round extends its session's
growing prefix, waits for its recorded tool time, and has a recorded output
length. The adapter verifies the trace's SHA256.

The adapter measures a ladder of steps at 4, 8, 16, 32, 64, 128 and 256 concurrent
sessions. Each step is a fixed-time window: 60 seconds of warmup, then 180
seconds measured, after which every request still in flight is abandoned. Steps
are separated by a 15-second pause, and every step uses freshly generated
synthetic text, so no step benefits from the server's cache of an earlier one.

* **Latency cap.** A step passes the cap if its TPOT p50 is at most 100 ms and
  its TTFT p50 is at most 10,000 ms.
* **Climb.** Steps run in ascending order. The climb continues while a step passes
  the cap and its output throughput is at least 1.03 times the best throughput of
  the earlier passing steps; the first step that fails either condition ends it.
* **Fixed level.** Concurrency 4 is always measured, whatever its result.
* **Refinement.** After the climb, let p be the best passing step. If steps p and
  2p both ran, one more step runs at round(p x sqrt(2)) (23 for 16, 45 for 32). The
  same cap applies, and it counts as a candidate for the peak.
* **Peak.** The throughput objective is the maximum output throughput over all
  passing steps. The reported peak concurrency is the lowest-concurrency passing
  step whose throughput is within 3% of that maximum. If no step passes the cap,
  the throughput of the lowest step run is used and `latency_cap_met` is 0.

The score has exactly three objectives:

* output tokens per second (maximize): the peak above. Output tokens that arrive in
  the window, divided by the window length;
* TTFT p50 in milliseconds at 4 concurrent sessions (minimize): time to
  first token over requests sent inside the window. A request still waiting for its
  first token at the end of the window counts with the time it has waited so far as
  a lower bound, so leaving long prompts waiting does not help;
* TPOT p50 in milliseconds at 4 concurrent sessions (minimize):
  per-request time between output tokens, from the tokens that arrive inside the
  window.

Preserve every session dependency, growing prefix, output length and tool wait;
no session is dropped on context overflow. Preserve real prefix-cache usage
accounting so the runner's prefix-cache preflight can pass, and stream token ids
with every response: a request that streams none cannot be placed in time, so the
measurement fails. The token pool is synthetic coding text because the trace
describes lengths and topology rather than private prompt content. Setup occurs
before measurement; the window is the runner's native one. The sweep must finish
inside the benchmark's 3,600-second budget (it keeps a 3,300-second deadline). The
step at 4 runs first, and the deadline only cuts the climb above it and the
refinement. If 4 cannot finish, the measurement fails. Results are also evaluated on held-out TraceLab traffic, drawn the same way from different sessions and users. Do not special-case this trace's sessions, their order or their lengths; a change must improve serving of coding-agent traffic in general.

Any failed request in or before a window, any output of the wrong length, a
missing or non-finite metric, or a runner failure fails the measurement. No
dry-run result is accepted. Promotion is decided on these three objectives, judged
on their Pareto frontier with a per-objective relative noise tolerance. The following are reported for reference and are **not**
scored: TTFT and TPOT p90 at 4, the peak concurrency, `latency_cap_met`,
and the throughput of every step that ran (`tok_s_at_c<N>`). Tail percentiles are
not scored at this tier.
