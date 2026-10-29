{% if modality %}
{% include "modality/" ~ modality ~ "/implementer.md" %}

{% endif %}
## LLM-serving implementation invariants

Trace every claim through the real request-to-model-to-stream path. Prove the
claimed mechanism activates; configuration/import/zero counters are not
activation. Record point-local useful batch/tokens, kernel/path, fallbacks,
graph bucket, and resource limits without hot-loop synchronization.

{% if workspace_sources %}
Build on pinned checkout(s): {% for source in workspace_sources %}`{{ source.dest }}/` ({{ source.name }}){% if not loop.last %}, {% endif %}{% endfor %}. Inspect and adapt relevant paths before replacing components; cite evidence for any necessary replacement.
{% endif %}

For candidate components that use Python, use `uv`; this is not a requirement that the serving hot path remain Python.

Keep correctness and workload shape fixed. Preserve prompt-dependent generation,
cache/mask/position alignment, deterministic greedy output where required, and
one logical streaming delta per generated model token. Coalescing writes is
allowed; changing token-record accounting is not. Live exact cohorts may share
one active execution; never serve a later arrival via completed output/token
replay without model execution.

Use the existing benchmark/controller path. Extend it only when the hypothesis
changes control flow or serialization; prove injected failure makes zero paid
calls and a synthetic success traverses it. Capture source/build inputs before
launch, retain rows immediately, and run compatible phases on one initialized
server when valid.

## Profile under the benchmark's load

Isolated-request profiles miss costs that appear only under load. Before
tuning kernels, compare isolated-request throughput with throughput under the
benchmark workload and attribute any gap. Profile over a short post-warmup
window of the real benchmark workload{% if profiler_id == "nsys" %}, with nsys and NVTX ranges per step
and per pipeline stage{% elif profiler_id == "torch" %}, with the torch profiler and a
`record_function` range per step and per pipeline stage{% elif profiler_id == "none" %}, timing each
step and pipeline stage inside the server{% else %}, with the run's profiler (`{{ profiler_id }}`){% endif %}. Report per-stage busy share,
host gaps by cause, batch token fill and sequences per batch, and kernel time
against context length. Size each change in ms per step against the
measurement noise, and batch changes too small to confirm one at a time.
This applies to prefill-bound tasks as well as decode-bound ones.

## Use references as implementation support

Read `serving-systems/SKILL.md`, then the one materialized
`references/platforms/<backend>/floor.md`. The platform floor is authoritative:
do not apply another backend's guidance. For every mechanism named by the
plan, read its portable contract and the selected platform implementation when
one exists; load only narrow references needed by the evidence. Search the
reference tree before relying on priors. Name consulted references and their
recommendations in the summary.
