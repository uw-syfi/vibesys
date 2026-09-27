# Orchestration plugins and runtime

VibeSys orchestration is ordinary Python policy over a small runtime contract.
A plugin declares its agent roles and policy entry point. The runtime supplies
sessions, workspaces, evaluation, durable state, control, commands, skills, and
typed observations through `Run`.

The boundary is deliberate:

| Owner | Holds |
|---|---|
| `src/vibesys/orchestration/` | Agent roles, prompts, reply schemas, search and selection policy, evaluation cadence and interpretation, policy state, and plugin declarations |
| `src/vibesys/plugin_catalog.py` | The product catalog of in-repository plugins and their read projections |
| `src/vibesys/run/`, `src/vibesys/composition.py` | Thin VibeSys composition: bind product config to roles, translate semantic events, and connect a selected plugin to the runtime |
| `libs/vs-runtime/` | Reusable session, workspace, state, evaluation, control, command, skill, and cleanup mechanics |
| `libs/vs-agent/`, `libs/vs-sandbox/`, `libs/vs-project/` | Agent harnesses, isolated execution, and project persistence |

Policy imports public contracts from `vs_runtime.api`. It does not construct
agent clients, sandboxes, worktrees, or product run resources. Runtime libraries
do not import VibeSys policy. [`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml)
enforces the dependency direction.

## Plugin declaration

An `OrchestrationPlugin` is the complete declaration of one policy. Its
`agents` tuple is the sole role catalog. There is no separate agent registry.
The product's `OrchestrationRegistry` selects plugins by stable ID; it does not
redeclare their roles.

```python
from pydantic import BaseModel, ConfigDict
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    OrchestrationPlugin,
    Run,
    RunStatus,
    WorkspaceAccess,
)


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = 1


class WorkerReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


WORKER = AgentRole(
    id="worker",
    system_prompt="Implement the requested change and report what you verified.",
    tools=(AgentTool(id="shell"),),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)


async def orchestrate(run: Run, raw_options: BaseModel) -> RunStatus:
    options = Options.model_validate(raw_options)
    session = await run.agents.create_session(
        WORKER,
        workspace=run.workspaces.root,
    )
    try:
        for round_number in range(1, options.max_rounds + 1):
            reply = await session.turn(
                f"Implement round {round_number}.",
                response=WorkerReply,
            )
            run.observations.note(reply.summary)
    finally:
        await session.close()
    return RunStatus.SUCCEEDED


PLUGIN = OrchestrationPlugin(
    id="example",
    agents=(WORKER,),
    options=Options,
    orchestrate=orchestrate,
)
```

Roles contain facts fixed for every session of that role:

- `id`, which also binds the role to user-supplied model and reasoning config;
- `system_prompt`;
- declared tools and skills;
- workspace access;
- required driver capabilities.

Changing evidence belongs in the message passed to `turn`, not in a rebuilt
role. A response schema is selected per turn, so one session may request
different typed replies as the conversation progresses. Omitting `response`
returns text.

Plugin declarations may also name their typed state model, projection, resume
policy, memory paths, or maximum-round projection. Declare only the hooks the
policy uses. Product configuration is validated against `plugin.agents`, so an
unknown role override fails before any run resource opens.

Built-in plugins live in `orchestration/{single,multi,issue_queue,evolve}` and
are registered in `vibesys.plugin_catalog.built_in_orchestrations`. Keep role
declarations in the owning plugin package, normally in `agents.py`, and expose
them through the plugin's `agents` tuple.

## Explicit session semantics

`run.agents.create_session` makes conversation lifetime visible in policy
code:

```python
first = await run.agents.create_session(WORKER, workspace=workspace)
await first.turn("Inspect the failure.")
await first.turn("Now implement the fix.")  # continues the same context

second = await run.agents.create_session(WORKER, workspace=workspace)
await second.turn("Review independently.")  # a fresh conversation
```

An unnamed session starts a fresh provider conversation. Repeated calls to
`turn` on that session preserve its context. A non-empty `member_id` gives the
conversation a durable policy identity, allowing a later session with the same
role and member ID to resume provider context when the configured driver
supports `PROVIDER_SESSION_RESUME`. Use it only when that durable identity is
part of the policy.

The role, workspace, writable paths, and resolved harness/model binding are
fixed when the session is created. `WorkspaceAccess.LIMITED` requires explicit
workspace-relative `writable_paths`; other access modes reject them. A session
bound to a discarded candidate workspace is invalidated.

Close sessions in `finally`. `close()` is idempotent, and the runtime closes
all remaining sessions in reverse creation order as a failure-path fallback.
Explicit cleanup in policy keeps the intended lifetime readable and releases
resources before the whole run ends.

## Static and dynamic teams

A team needs no framework DSL. It is a normal Python collection of explicit
sessions. This avoids nested context managers and keeps message routing in the
policy that owns it.

```python
roles = (PLANNER, IMPLEMENTER, REVIEWER)
team = [
    await run.agents.create_session(role, workspace=workspace)
    for role in roles
]
try:
    plan = await team[0].turn("Plan the next change.", response=Plan)
    result = await team[1].turn(render_implementation(plan), response=Result)
    verdict = await team[2].turn(render_review(plan, result), response=Verdict)
finally:
    for session in reversed(team):
        await session.close()
```

For a dynamic team, keep a dictionary keyed by a policy-owned member ID and
pass that ID as `member_id` when the member joins. The policy decides which
member sees which follow-on message, whether members share a workspace, and
when a member leaves. The runtime still owns isolation and eventual cleanup.

Concurrent work is also ordinary Python. Use `asyncio.TaskGroup` only when the
chosen workspaces and policy state are independent. Do not concurrently call
`turn` on one session; turns in a conversation are sequential.

## `Run` capabilities

`Run` exposes semantics needed by policy, not product or provider
implementation objects:

| Capability | Policy use |
|---|---|
| `facts` | Read immutable objective, domain, environment, evaluation, profiler, and workspace-source facts |
| `agents` | Create and own explicit agent sessions |
| `workspaces` | Use the root workspace, create/discard candidates, adopt a retained revision, or export a patch |
| `evaluation` | Run trusted accuracy, benchmark, and audited local-validation operations |
| `state` | Load and atomically commit the plugin's declared Pydantic state model |
| `control` | Check cooperative pause and stop state at policy-selected boundaries |
| `commands` | Run validated commands in a selected workspace |
| `skills` | Resolve policy-selected installed skill resources |
| `observations` | Publish informational notes or non-fatal semantic warnings |

Workspace and candidate lifetimes are explicit. A plugin creates a candidate
with `run.workspaces.create_candidate()`, retains or adopts a revision through
the workspace APIs, and discards the candidate in `finally`. The lower runtime
owns Git, sandbox, worktree, and cleanup mechanics. Similarly, orchestration
decides when correctness or performance evaluation is due and interprets the
typed result; the runtime performs the trusted evaluation.

## Policy organization

Keep code near the plugin that owns the decision:

- `agents.py`: immutable role declarations;
- `plugin.py`: the `OrchestrationPlugin` declaration;
- `orchestration.py`: high-level sequencing and control flow;
- `models.py` or focused policy modules: options, state, reply schemas, and
  deterministic transitions;
- `prompts/`: agent-visible prompt rendering owned by that policy.

Shared modules under `vibesys.orchestration` are policy shared by multiple
built-in plugins, such as hypothesis search, metric interpretation, profiler
selection, and domain prompt content. Extract shared policy only when real
callers need the same semantics. Do not introduce setup objects, registries,
builders, or callback bundles merely to shorten orchestration code.

## Adding a plugin

1. Create `src/vibesys/orchestration/<plugin>/` with strict Pydantic options,
   any typed state, role declarations, prompts, and the orchestration function.
2. Construct one `OrchestrationPlugin`; its `agents` tuple is authoritative.
3. Use only `Run` capabilities for effects. Keep selection, cadence,
   message routing, and response interpretation in the plugin.
4. Register the plugin in `vibesys.plugin_catalog.built_in_orchestrations`.
5. Add policy tests through `FakeRun`, prompt or state-transition tests as
   appropriate, and integration coverage for product composition only when the
   boundary itself changes.
6. Add any new module edge to `tach.toml` and run `uv run tach check`.

A preset that shares an implementation but changes policy options should be a
separate plugin with its own stable ID and options type. Plugin IDs and option
versions are persisted in the run manifest, so change them deliberately.

## Testing orchestration policy

Use the public fake from `vs_runtime.api.testing`. It derives agent and state
contracts from the plugin, so tests cannot silently invent a second role
catalog.

```python
import asyncio
from collections import deque

from pydantic import BaseModel
from vs_runtime.api.testing import FakeRun


replies = deque([WorkerReply(summary="implemented")])


def respond(
    role: AgentRole,
    history: tuple[str, ...],
    message: str,
    response: type[BaseModel] | None,
) -> object:
    assert role is WORKER
    assert history == ()
    assert message == "Implement round 1."
    assert response is WorkerReply
    return replies.popleft()


async def scenario() -> None:
    run = FakeRun(PLUGIN, responder=respond)
    try:
        status = await PLUGIN.orchestrate(run, Options())
        assert status is RunStatus.SUCCEEDED
        assert run.agents.sessions[0].closed
    finally:
        await run.close()


asyncio.run(scenario())
```

The fake records sessions, per-session message history, workspaces, evaluation
calls, state commits, control checkpoints, commands, skills, and observations.
Configure those public fake capabilities directly. Do not patch runtime
internals or replace private functions.

Useful focused commands:

```bash
uv run pytest tests/vibesys/orchestration/single/test_designer.py
uv run pytest tests/vibesys/orchestration/single/test_plugin.py
uv run pytest tests/vibesys/orchestration/test_explicit_agent_sessions.py
uv run pytest tests/vibesys/architecture
uv run tach check
uv run python scripts/check_test_isolation.py
```

Prompt snapshots live with their owning policy tests. Regenerate only the
affected snapshots with `UPDATE_PROMPT_SNAPSHOTS=1`; regenerate end-to-end
goldens with `UPDATE_GOLDEN=1 uv run pytest tests/vibesys/golden`, and review
the resulting diff before committing it.
