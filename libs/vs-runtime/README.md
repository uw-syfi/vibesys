# vs-runtime

`vs-runtime` defines the policy-to-runtime boundary for running agent-driven
plugins. Import its public contracts from `vs_runtime.api` and its in-memory
test implementations from `vs_runtime.api.testing`.

The package intentionally does not define workflow, team, or search concepts.
Policies own those decisions. The runtime owns role configuration binding,
conversation lifetime, run resource lifetime, and plugin dispatch.

Each `OrchestrationPlugin` is an explicit value with a stable ID, an options
schema, an authoritative tuple of immutable agent roles, state and projection
contracts, and its orchestration function. Sessions bind one declared role,
workspace, and optional member ID for their full lifetime.
