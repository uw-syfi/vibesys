## LLM-serving combined-round invariants

Trace every claim through the actual request-to-model-to-stream path. For
batching, slot/KV/mask/position changes, retain deterministic concurrent mixed-
length correctness including one request finishing while others continue. For
kernel/layout work, name the removed production operator and its frequency,
bytes, or launches; configuration alone is not activation, and dense KV
reconstruction before attention is not paged-attention compute.

{% if workspace_sources %}
Build on the pinned mutable checkout(s): {% for source in workspace_sources %}`{{ source.dest }}/` ({{ source.name }}){% if not loop.last %}, {% endif %}{% endfor %}. Inspect and adapt their relevant paths before replacing components; if replacement is necessary, record what you inspected and why.
{% endif %}

Preserve one logical SSE delta per generated model token even when writes are
coalesced, and verify token IDs, records, completion counts, EOS/stop behavior,
and usage accounting. Splitting/merging logical token records or replaying a
completed output/token trajectory to a later arrival is reward hacking; live
exact cohorts may share one active model execution.

Inventory hot-loop telemetry synchronization and measure observer effect. A
materially perturbed profile is qualitative and cannot establish an Amdahl
share. Read only serving-systems references justified by the typed plan or new
evidence; do not preload the library or preserve Python/FastAPI boundaries
without a contract reason.

As your own judge, do not let yourself cheat: reject prerecorded/constant output, evaluator-specific
branches, weakened checks, omitted failures, or any steady-state response that
uses completed replay instead of the request's declared model execution.
