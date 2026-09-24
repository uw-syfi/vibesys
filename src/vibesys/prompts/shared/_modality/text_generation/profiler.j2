{% if profile_execution is not defined %}{% set profile_execution = "local" %}{% endif %}
## Server Type: Text Generation (Causal LM)

The server exposes OpenAI-compatible text generation endpoints:
- `POST /v1/completions` — `{prompt, max_tokens, temperature, stream}`
- `POST /v1/chat/completions` — `{messages, max_tokens, temperature, stream}`
- `GET /health` — health check

Key performance axes: **time-to-first-token (TTFT)**, **time-per-output-token (TPOT)**, **throughput** (tokens/s under concurrent requests). Prefill and decode are distinct phases with different bottlenecks.

{% if profile_execution == "remote" %}
## Profiling workload (remote execution)

Do **not** start a production server in the editor when its accelerator runtime
is remote. The representative workload must run on the remote candidate path.
Discover the declared deployment and bounded
profiling/controller command from the runtime notes, manifest, or candidate
build/startup configuration; do not require a fixed Python module or local
entrypoint. The remote function or live service should drive a steady-state mix
matching the benchmark. After it returns an analyzer-compatible artifact, run
the analysis locally. Per-token throughput, if computed, should come from the
artifact's remote wall time and completed token count, not from local dispatch
timing.
{% else %}
## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
curl -s -X POST http://localhost:8077/v1/completions -H "Content-Type: application/json" -d '{"prompt":"warmup","max_tokens":4,"temperature":0}' --max-time 300
```

**Workload** (run during profiling when no benchmark tool is available):
```
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8077/v1/completions -H "Content-Type: application/json" \
    -d '{"prompt":"The capital of France is","max_tokens":32,"temperature":0}'
done
```
{% endif %}
