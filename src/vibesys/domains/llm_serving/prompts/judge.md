## LLM-serving review invariants

audit the implementer's retained performance evidence and verify the real
request-to-model-to-stream path and the input-owned API/model
contract. Do not infer a required language, framework, process boundary, or
filename. Audit custom model-layer ownership when declared by the objective,
weight/device placement, cache/mask/position alignment, EOS/stop/usage behavior,
and deterministic prompt-dependent generation. Live cohorts may share active
execution. Completed output/token replay for later arrivals is model bypass;
test a novel miss and scope claims to the measured hit mix.

{% if workspace_sources %}
The pinned checkout(s)—{% for source in workspace_sources %}`{{ source.dest }}/` ({{ source.name }}){% if not loop.last %}, {% endif %}{% endfor %}—are mutable candidate code. Verify that the implementation adapted them, or supplied concrete inspection evidence justifying each replacement; include their production paths in static review.
{% endif %}

For every optimization claim, verify production activation at its source. An
import, configured backend, object construction, or zero-valued field that is
never updated proves nothing. Check point-local telemetry scope and observer
cost; post-drain occupancy can be zero after valid activation, so distinguish
historical totals/peaks/events from instantaneous state.

Audit LLM-serving measurement fidelity: fixed prompt/output shape and offered
load, successful completions, one logical streaming delta per generated model
token, consistent token counts, and throughput/TTFT/TPOT/latency from the same
selected row. Batched writes may contain multiple complete records; merging or
splitting their model-token accounting is reward hacking.

Treat attention/layout claims precisely. A path that gathers or reconstructs
dense logical KV before dense attention is an allocator/layout experiment, not
paged-attention compute. A backend comparison must show the kernel that actually
consumed the production tensors and whether fallback occurred.

For roofline or Amdahl claims, require a whole-decode model and an observer-
controlled end-to-end comparison. Do not add overlapping CPU/CUDA durations,
call hardware peak automatically attainable, or use the reference engine as the
hardware ceiling. Verify that any throughput-only gain remains a legitimate
Pareto tradeoff and that terminal parity uses one joint operating point.
