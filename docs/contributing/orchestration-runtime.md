# Custom orchestration runtime

`ExecutableOrchestration` is the public policy hook. Its `execute(request,
runtime) -> bool` method decides which agents exist, when they take turns,
what messages they receive, and when the run succeeds. `VibeSysRuntime`
provisions agents and owns their sandbox and client lifetimes. The framework
does not define a round or require a particular agent graph. The registry
normalizes this hook to its internal lifecycle contract.

The framework's internal registered contract has two parts:
`OrchestrationExecution` handles run setup and policy execution, while
`OrchestrationProjection` derives metadata and views. Their aggregate,
`Orchestration`, is used by the registry and runner. Only `execute` is required
of a custom policy; the registry supplies defaults for the other hooks.

| Hook | When called | Input and output |
| --- | --- | --- |
| `describe` | Before `RUN_STARTED` and runtime setup | `RunRequestLike` to `RunDescription` (round budget and expected roles for the start event) |
| `prepare` | After runtime setup, before `execute` | `RunRequestLike`, `VibeSysRuntime` to `None`; policy setup |
| `execute` | Once per run, after `prepare` | `RunRequestLike`, `VibeSysRuntime` to `bool` (run success) |
| `view` | On live session and persisted-history reads | `Project`, run ID, status, orchestration ID to `RunView` |
| `project_committed` | After a state commit when a live view listener exists | State namespace, committed `BaseModel`, run ID to `RunView | None` |
| `resume_projection` | During built-in CLI resume restoration | `OrchestrationRunManifest` to `ResumeProjection` |

`execute` can spawn agents, route their messages, and schedule later turns
dynamically from earlier outputs. Rounds are optional policy code. The built-in
agent control flow is narrower: its fixed roles and round transactions implement
one policy. Other policies can choose different agents and schedules.

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
    OrchestrationRunRequest,
    VibeSysRuntime,
    create_session,
)
from vibesys.api.request import load_input_bundle


class ThreeAgentRounds:
    def execute(self, request: OrchestrationRunRequest, runtime: VibeSysRuntime) -> bool:
        descriptor = request.orchestration
        if descriptor.config_version != 1:
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
request = OrchestrationRunRequest(
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

`OrchestrationRunRequest` is the generic descriptor-based request. The framework
reads it through `RunRequestLike`. `RunRequest` and `LoopKind` remain public
compatibility imports for built-in CLI selection and are deprecated for new
policies. Accessing either name through `vibesys.api` or
`vibesys.api.contracts` emits a deprecation warning. Their policy options
belong to the built-in adapters; importing the generic API does not load them.
`RunResult.loop` and `RunView.loop` return ID strings for every policy;
`LoopKind` still compares equal to its corresponding string value.
`RunView` contains run identity, lifecycle status, and an optional JSON object
`projection` owned by the selected policy. Policies without a read model leave
it absent. The built-in agent policy projects its experiment and round facts as
`AgentRunProjection`; `agent_projection(view)` validates that payload and
returns `None` for other policy projections. Committed-view `changed_keys` are
policy-specific; for the built-in agent policy they identify changed hypotheses.
Agent-only projection types, `agent_projection`, manifest helpers, and
`framework_memory_paths` are imported from `vibesys.api.agent`. Explicit
imports of these names from `vibesys.api` remain available with a deprecation
warning; the generic API does not load the agent projection until selected.

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

The built-in agent loop selects `MultiAgentFlow`, `SingleAgentFlow`, or
`ProfileGuidedFlow` through its internal `BuiltInAgentControlFlow` protocol.
`policy_scheduler.py` chooses new and continuing hypotheses and terminal
transitions; `policy_attempts.py` handles retries. These policies use typed
`AgentTurns`, `RoundEffects`, and profile effect ports. `policy_local.py` binds
the ports to the run context, role handles, and issue board. Policy decisions
have fake-based tests without agents or run environments. The local bindings
use the existing shared context rather than `VibeSysRuntime.spawn_agent`.

The evolve policy in `src/vibesys/loops/evolve/policy_flow.py` selects parents,
records outcomes, and chooses the final candidate. `EvolveRunScheduler`
schedules bootstrap, serial or parallel generations, checkpoints, and final
selection. `retry_bootstrap` owns first-seed retry numbering and stop-on-pass or
exhaustion through `BootstrapEffects`. `evaluate_candidate` orders mutation,
review, framework gates, measurement, and snapshotting through typed
`CandidateEffects`. The loop
adapter binds these effects to agents, task gates, Git, and the run environment.
The concrete bootstrap attempt body and candidate workers remain there.
Policy tests use fake effects and in-memory populations.

The built-in plain loop's `PlainPolicy` in
`src/vibesys/loops/plain/policy.py` owns issue draining, retry budgets,
implementer-to-judge handoffs, performance evaluation, resume cursor decisions,
and termination. Its typed `PlainPolicyPort` supplies issue operations, turns,
checkpoints, and progress effects. `run_plain_loop` provisions the existing run
context and binds those effects in `_PlainEffects`; it retains the current
prompts, issue board, persistence, and workspace snapshots. The policy can run
with an in-memory fake port, as tested in
`tests/vibesys/loops/plain/test_plain_policy.py`, without constructing a task,
project, agent client, sandbox, or run environment.
Run the fake-port checks with
`uv run pytest tests/vibesys/loops/plain/test_plain_policy.py`.

Custom orchestrations implement `execute(request, runtime)` above and may
choose agents, messages, and later actions from earlier outputs.

This is the first runtime slice. Agents share the run workspace unless the
selected run environment isolates it. The runtime does not yet offer a generic
message bus, checkpoint API, profiler capability, or custom resume policy.
`ProfilerKind.AUTO` uses no profiler for custom policies; selecting an active
profiler is rejected. SkyPilot custom runs are rejected until each spawned
agent can own a bridge without replacing the run's bridge socket. Docker and
Modal agent environments use each agent's backend and provider for container
authentication. Custom resume is rejected until an orchestration-owned
checkpoint contract exists. History currently has a generic view for
execute-only policies. The registry currently forwards optional `describe`,
`view`, `project_committed`, `prepare`, and `resume_projection` methods if a
policy supplies them. These are provisional internal lifecycle hooks, not the
stable custom policy contract. The former public `Orchestration` and
`RunDescription` imports remain available for compatibility but are deprecated.
The broader agent spawning, sandbox, workspace, and remote runtime design is
tracked in [RFC #937](https://github.com/uw-syfi/vibesys/issues/937).
