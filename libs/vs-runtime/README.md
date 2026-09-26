# vs-runtime

`vs-runtime` defines the policy-to-runtime boundary for running agent-driven
plugins. Import its public contracts from `vs_runtime.api` and its in-memory
test implementations from `vs_runtime.api.testing`.

The package intentionally does not define workflow, team, or search concepts.
Policies own those decisions. The runtime owns role configuration binding,
conversation lifetime, trusted candidate evaluation, run resource lifetime,
and plugin dispatch.

Each `OrchestrationPlugin` is an explicit value with a stable ID, an options
schema, an authoritative tuple of immutable agent roles, state and projection
contracts, and its orchestration function. Sessions bind one declared role,
workspace, and optional member ID for their full lifetime.

`RunHost.evaluation` exposes separate semantic accuracy and benchmark calls,
so policy owns their cadence and interpretation. Accuracy passes carry a
serializable receipt that may be reused only for the same run, workspace, and
candidate revision; trusted command construction and execution remain private.
