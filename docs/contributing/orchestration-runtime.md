# Orchestration runtime

Every run starts from one frozen `RunRequest`. Its `orchestration` descriptor
contains a stable ID, `config_version`, and JSON policy options. The CLI and SDK
build the same request. A registered policy constructor validates the ID,
version, and strict typed options before any run resources are opened.

The descriptor contains only policy decisions, such as budgets, search strategy,
and metric axes. `RunRequest` owns host execution settings: model, agent and
compute backends, provider, profiler selection, thinking settings, feature
flags, and skill directories. Policies obtain these through `ctx.request`,
not duplicate descriptor options. The v4 manifest stores these settings in
`execution`, including the profiler selected after capability resolution.
Resume compares the full execution record before changing run state. This
rejects changes to resolved profiler, available skills, and other execution
inputs even when the requested profiler was `auto`.

`OrchestrationRegistry` maps each ID to a concrete `Orchestrator` class and an
optional read projector. The built-ins register six classes: multi-agent,
single-agent, profile-guided multi-agent, profile-guided single-agent, plain,
and evolve. The runner creates one `RunContext` and awaits the selected
policy's `run(ctx) -> bool`. It does not inspect policy IDs.

```python
from vibesys.api import OrchestrationDescriptor, OrchestrationRegistry, RunRequest
from vibesys.context import RunSetup


class MyOrchestrator:
    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        if descriptor.id != "my-policy" or descriptor.config_version != 1:
            raise ValueError("unsupported my-policy descriptor")
        if descriptor.options:
            raise ValueError("my-policy has no options")
        self.setup = RunSetup()

    async def run(self, ctx) -> bool:
        await ctx.control.boundary()
        return True


registry = OrchestrationRegistry()
registry.register("my-policy", MyOrchestrator)
# Pass registry to create_session(request, sink=..., registry=registry).
```

`RunSetup` declares the policy's durable state namespace and Pydantic model,
resume descriptor comparator, and whether it needs the default agent. It may
also carry `RunStartHints(max_rounds=..., expected_roles=(...))`. The generic
session emits those hints in `run_started` without inspecting the policy ID;
custom policies can omit them. The host uses setup data during recovery. A
policy with durable state calls
`ctx.state.load(Model)` and `ctx.state.checkpoint(state, sequence=...)`.
Checkpoint retains the Git revision, persists the state transition, then
publishes the committed view. `ctx.control.boundary()` is the cooperative
pause and stop boundary between host operations.

The host owns agent client and sandbox cleanup. `ctx.agents.spawn(definition,
scope=scope)` returns a handle with awaitable text and structured turns in the
selected workspace. Omit `scope` to use the parent workspace. Each agent gets
its own environment session, backend and provider settings, and host resource
grants. `ctx.workspaces` forks, snapshots, adopts, and discards candidate
scopes. A fork opens an isolated run environment session when that environment
supports parallel candidate evaluation. Discard drains its agents and trusted
gates before removing the worktree. Parent Git mutations, including checkpoints,
forks, snapshots, adoption, and worktree removal, share one run-scoped lock.
Isolated candidate snapshots run independently until they retain a revision in
the parent. The trusted evaluator uses the same scope:

```python
from vibesys.orchestration.runtime import MeasurementOptions
from vibesys.runtime import AgentDefinition

scope = await ctx.workspaces.fork()
try:
    agent = await ctx.agents.spawn(AgentDefinition("candidate-1", spec), scope=scope)
    await agent.turn("Try a candidate edit")
    accuracy = await ctx.evaluator.check("candidate-1", scope=scope)
    benchmark = await ctx.evaluator.measure(
        "candidate-1", scope=scope, options=MeasurementOptions(objectives=axes)
    )
finally:
    await ctx.workspaces.discard(scope)
```

`check` also accepts an event label and execution command override. `measure`
accepts `MeasurementOptions` with objective axes, an event label, and a
benchmark command override. Both use the input bundle's trusted commands,
result contract, and timeouts. Their implementations live in
`vibesys.evaluators.gates` and `vibesys.evaluators.metrics`; policy packages
decide when to call them. Synchronous policy helpers can use
`await ctx.run_blocking(function, *args, **kwargs)`; cancellation waits for the
worker before host resources close. A temporary `ctx.run_context` property
supports built-in migration helpers and must not become a custom policy
interface.

The projector is registered separately from execution. It derives a `RunView`
from durable state for live and historical reads and projects committed state
for listeners. A policy without a projector has an identity and status view.
The policy owns the optional JSON projection schema. Generic lifecycle events
carry run identity and status; policies emit their own progress facts.

Only v4 run manifests are supported. Older run files fail with an explicit
unsupported-schema error. `config_version` permits future changes to a
policy's options within the active manifest format. Old SDK request imports
and legacy loop selectors are removed in this cutover.
