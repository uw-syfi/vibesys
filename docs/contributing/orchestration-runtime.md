# Custom orchestration runtime

`Orchestration` is the policy interface. Its `execute(request, runtime) -> bool`
method decides which agents exist, when they take turns, what messages they
receive, and when the run succeeds. `VibeSysRuntime` provisions agents and owns
their sandbox and client lifetimes. The framework does not define a round or
require a particular agent graph.

```python
import asyncio
from pathlib import Path

from vibesys.api import (
    AgentDefinition,
    AgentSpec,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    RunRequest,
    VibeSysRuntime,
    create_session,
)
from vibesys.api.request import load_input_bundle


class ThreeAgentRounds:
    def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
        descriptor = request.orchestration
        if descriptor is None or descriptor.config_version != 1:
            raise ValueError("unsupported three-agent-rounds configuration")
        rounds = descriptor.options.get("rounds")
        if not isinstance(rounds, int) or rounds < 1:
            raise ValueError("rounds must be a positive integer")
        spec = AgentSpec(model=request.config.model.name)
        planner = runtime.spawn_agent(AgentDefinition("planner", spec))
        implementer = runtime.spawn_agent(AgentDefinition("implementer", spec))
        reviewer = runtime.spawn_agent(AgentDefinition("reviewer", spec))

        feedback = request.objective or "Start"
        for number in range(1, rounds + 1):  # Round boundaries belong to this policy.
            label = f"round{number:03d}"
            plan = planner.turn(feedback, label=label)
            change = implementer.turn(plan, label=label)
            feedback = reviewer.turn(change, label=label)
        return "approved" in feedback.lower()


project_root = Path("path/to/project")  # Contains vibesys.input.toml.
request = RunRequest(
    project_root=project_root,
    orchestration=OrchestrationDescriptor(
        id="three-agent-rounds", config_version=1, options={"rounds": 2}
    ),
    config=Config.model_validate({"model": {"name": "your-model"}}),
    input_bundle=load_input_bundle(project_root),
    exp_name="team-demo",
    profiler_kind=ProfilerKind.NONE,
)
registry = OrchestrationRegistry()
registry.register("three-agent-rounds", ThreeAgentRounds())
session = create_session(request, sink=lambda _event: None, registry=registry)
session.start()
result = asyncio.run(session.await_result())
```

`AgentDefinition` accepts an `AgentSpec` per agent and optional `resources` as
`HostResource` grants. `spawn_agent` opens an agent environment with those
mounts, passes the grants to the driver, and rejects drivers that cannot enforce
them. `AgentHandle.turn(message, system_prompt="", label="")` returns text;
the policy routes that text. Handles close when execution ends, including on
failure, and may be closed earlier. Agent IDs must be unique within a run.
`turn_structured(message, response_cls=..., fallback_factory=...)` returns a
validated Pydantic response and accepts an optional session key and reuse
choice. It uses the same run control and event path as text turns.
Use `AgentDefinition.resources` for grants; nondefault `AgentSpec.execution`
is rejected by this runtime slice rather than silently ignored.

The built-in multi-agent policy declares orchestrator, implementer, judge, and
profiler roles as named handles over its existing shared run context. Its policy
code still chooses whether to profile or review, routes typed plans and feedback,
and controls retries and round completion. These internal bindings do not call
`VibeSysRuntime.spawn_agent`; preserving the built-in client, sandbox, hypothesis
sessions, and round transaction behavior is part of this incremental migration.

This is the first runtime slice. Agents share the run workspace unless the
selected run environment isolates it. The runtime does not yet offer a generic
message bus, checkpoint API, profiler capability, or custom resume policy.
`ProfilerKind.AUTO` uses no profiler for custom policies; selecting an active
profiler is rejected. SkyPilot custom runs are rejected until each spawned
agent can own a bridge without replacing the run's bridge socket. Docker and
Modal agent environments use each agent's backend and provider for container
authentication. Custom resume is rejected until an orchestration-owned
checkpoint contract exists. History currently has
a generic view for custom IDs, without policy-specific rounds. The broader
agent spawning, sandbox, workspace, and remote-runtime design is tracked in
[RFC #937](https://github.com/uw-syfi/vibesys/issues/937).
