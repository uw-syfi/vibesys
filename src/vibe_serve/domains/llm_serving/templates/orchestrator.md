## Optimization priority (read before choosing the next task)

Serving systems have a well-established **optimization floor**: three techniques every production LLM server ships with, because each addresses a fundamental cost source the workload cannot avoid on NVIDIA hardware. Before proposing any workload-specific optimization (speculative decoding, prompt/prefix caching, grammar-constrained decoding fast paths, schema minimization, etc.), confirm all three are in place unless a specific one is **absolutely incompatible** with the objective:

1. **Continuous batching** (see `skills/serving-systems/algorithms/continuous-batching/`).
2. **Attention kernel** — FlashInfer or FlashAttention (see `skills/serving-systems/backends/flashinfer/` and `skills/serving-systems/backends/flashattention/`).
3. **CUDA graphs** (see `skills/serving-systems/backends/cuda-graph/`). 

**Only after these three are present and verified** (profiler-confirmed kernel count drops, FlashInfer calls visible, graph replay counters non-zero) should you spend rounds on workload-specific optimizations like speculative decoding, grammar-based fast paths, or prompt / prefix caching.

The three exceptions that let you skip a floor item:

- **Continuous batching**: skip when the benchmark / objective is single-batch by contract.
- **Attention kernel**: skip when running on non-NVIDIA hardware where neither FlashInfer nor FlashAttention ships (Apple → MLX; AMD → the upstreamed FA AMD port).
- **CUDA graphs**: skip when the decode shapes are genuinely unbucketable (very rare — even speculative-decoding tree depths and chunked-prefill chunk sizes are ≤ 16 buckets).

If you skip a floor item, cite the specific incompatibility in your `reasoning`. Do NOT skip because "the current profile shows something else is the dominant cost" — the floor items *become* the dominant cost in turn once other work lands, and cycling between "revert this, try that" over exotic optimizations without the floor in place is a common failure mode of this loop.

## LLM-serving task examples

Good round-sized tasks for this domain include:
- "Build a self-contained FastAPI server for the reference model."
- "Add continuous batching to the decode loop."
- "Replace manual attention with FlashInfer batched decode."
- "Add CUDA graph capture/replay for the decode path."
- "Fix the 8 ms launch overhead shown in `linear_layer_N` (top kernel in the last profile)."

## Scoping API work

When your task touches HTTP endpoint or message-schema work, name the specific endpoint(s) and point the implementer at the authoritative skill file — typically `skills/serving-systems/tooling/openai-api/SKILL.md` (per-modality OpenAI-compatible contracts). You can start with a single endpoint (e.g. "`POST /v1/completions` only, streaming SSE") and grow the surface as the roadmap progresses.

## LLM-serving performance criteria

This matters whenever a round adds a path that *trades per-call work for fewer calls* (speculative decoding, xgrammar jump-forward, batched extend, prefix caching, prompt caching, larger CUDA-graph buckets). A wider or heavier kernel can win on the headline metric while losing on per-call latency — that's the entire point of the technique. Pass criteria like *"verify_replay_ms < decode_replay_ms"* or *"graph replay ≤ X ms"* can't see those wins and will silently kill correct implementations. Phrase the gate on the headline metric instead, and tell the implementer to wire any runtime fallback the same way: *"after N steady requests, if the new path's headline metric trails the existing baseline path's by more than M%, fall back"*. Avoid asking for a startup-only gate that uses a fixed per-call time threshold — it can't see acceptance/forced-token/host-side effects and will give the wrong answer.

For static-inspection criteria, prefer wordings like:

- "no `torch.profiler.profile(...)` invocations in `main.py` or any module the implementer added"
- "no per-token `torch.cat` against the KV cache in `main.py`'s decode path"

Avoid broad clauses like "no profiler/Nsight code"; those trip on framework-provided profiler directories.
