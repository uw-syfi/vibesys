## Evidence-led optimization method

Choose from measured end-to-end evidence, not technique popularity. Quantify
the reference gap and each mechanism's defensible gain. While the gap exceeds
2x, prefer material bottlenecks or needed prerequisites over single-digit tweaks.

The optimization floor is hardware-specific. Read
`references/platforms/<backend>/floor.md` before selecting a mechanism. On
`cuda` and `rocm`, check continuous batching, fused attention, and graph capture;
on `trainium`, `metal`, and `cpu`, follow that platform's floor instead. Skip a
floor item only for a stated objective incompatibility, not because another
profiled cost is currently larger.

{% if workspace_sources %}
Treat the pinned checkout(s)—{% for source in workspace_sources %}`{{ source.dest }}/` ({{ source.name }}){% if not loop.last %}, {% endif %}{% endfor %}—as the implementation starting point. Direct the implementer to inspect and adapt relevant paths; require concrete evidence before replacing seeded components.
{% endif %}

At the first baseline, after an architecture change, and on a plateau, use
`serving-systems/references/tooling/performance-modeling.md` to build a ranged
whole-decode roofline and reconcile the current-architecture ceiling with
end-to-end wall time and an observer-controlled profile. Translate terminal throughput into required step
time and useful active batch; Queued concurrency is not useful model work.

Require production-path activation tied to the claimed removed operation,
frequency, bytes/launches, or boundary. Telemetry in token/layer/request loops
must not add synchronization or large rescans. A KV-layout change that still
reconstructs dense logical KV before attention is not paged-attention compute.

Streaming is part of the measurement contract: preserve model-token accounting
and one logical delta record per generated model token even when writes are
coalesced. Live exact cohorts may share contemporaneous model work; completed
output/token replay for later arrivals is model bypass, not engine work.
