# Flint AgentX serving on 1 AMD MI300X GPU (192 GB HBM)

Build and optimize an OpenAI-compatible serving system for `RecursiveSI/flint`
on **1 AMD MI300X GPU (192 GB HBM)**. No candidate server or validated ROCm baseline is provided;
porting the model correctly to this hardware is part of the task. The shared
candidate lives in the project root; `.vibesys/` contains trusted task inputs.

Keep model execution on the allocated GPU. Start with correct BF16 execution, then investigate sparse expert kernels, sink-aware attention, batching and prefix-cache retention under concurrent long sessions.

## Model contract

Use the pinned BF16 checkpoint in `reference/meta.json`, including its tokenizer
and chat template. Preserve the 262,144-token context capacity and all trained
weights. Do not quantize or change the model to improve a performance reading.
Flint has 39.8B total parameters (about 2.5B active per token), two dense layers,
47 MoE layers, sigmoid top-six routing among 256 experts plus a shared expert,
35 sliding-window attention layers (513 tokens), and 14 full-attention layers
without positional encoding. Per-head normalization and learned attention sinks
are required model semantics. An attention backend that ignores sinks is not
an acceptable fallback.

The hardware allocation is a budget, not a claim that every operation already
has an available ROCm implementation. PyTorch and Transformers can provide
weight loading and reference operations. Do not proxy requests to a different
machine, an extra accelerator, or a hosted model. Host-side scheduling and
cache offload are allowed within the declared allocation; their costs remain
inside the measurement.

## Correctness before performance

Run the configured numerical gate against the service. It independently loads
the pinned Transformers eager reference on CPU, checks greedy continuations and
log probabilities around the local-attention boundary, repeats requests to
check determinism, and compares chat with the pinned template's raw completion.
All consumed response fields are validated, including exact token usage.

Provision at least 100 GiB of available host RAM for the CPU reference plus
candidate host memory. The task's 30-minute accuracy deadline includes reference
loading. There is no numerical fallback or reference-free success shortcut.
See [the shared checker](../../shared/checker.py) for the tolerance policy and
[the project README](../../../README.md) for the HTTP interface requirements.

## AgentX workload and objectives

Use the manifest's trusted `agentx-v1` entrypoint. Both Flint hardware tasks use
the same date-pinned June 21 coding corpus with parent/subagent dependencies,
four concurrent session trees, seed 42, trajectory start ratios 0.25–0.75,
ten warmup requests per lane, and 900 seconds of profiling. Preserve streaming,
requested output lengths, recorded pacing, and first-turn cache busting.
The four-tree limit is not a four-request limit: child agents can add requests.

Optimize the Pareto frontier of output tokens/s (maximize) and TTFT p50/p90 plus
TPOT p50/p90 in milliseconds (minimize), with the 3% noise tolerance in
`objectives.toml`. P90 request latency is diagnostic. A shorter output, failed
request, reduced context cap or altered trace is not an improvement.

Smoke runs are diagnostic and cannot emit accepted measurements. Confirm
progress with the full recipe after the numerical gate passes. Compare only
identical model, precision, workload and hardware settings; maintain separate
runs for the MI210 and MI300X targets. Coding-agent success is a separate quality
question from this serving replay.
