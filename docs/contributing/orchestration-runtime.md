# Orchestration strategies and the runtime

Every VibeSys orchestration is a strategy on the `vs-core` lifecycle. A strategy is a pure
object that decides the next step from a read-only view of the run; the runtime owns the
loop that executes those steps (agent turns, workspaces, measurements, operations) and
feeds the outcomes back as events. A plugin is the declaration that joins a strategy to its
roles, options and prompts. There is no other way to run a policy: a plugin without a
`core` policy is rejected, and the older imperative contract (`orchestrate(run, options)`
over a `Run` bundle of sessions, state and evaluation) no longer exists.

The boundary is deliberate:

| Owner | Holds |
|---|---|
| `src/vibesys/orchestration/<strategy>/` | The strategy (`strategy/`), its core policy (`core_policy/`), agent roles, prompts, reply schemas, options and plugin declaration |
| `src/vibesys/*_core.py`, `core_wiring.py`, `core_roles.py` | Thin product wiring: build the plan from the run's facts, register the strategy's operations, and join it to the runtime |
| `src/vibesys/plugin_catalog.py` | The product registry for declared plugin registrations |
| `src/vibesys/run/`, `src/vibesys/composition.py` | Thin VibeSys composition: open the run's resources, adapt trusted evaluation, translate semantic events, and compose the core host |
| `libs/vs-core/` | The pure lifecycle: state, decisions, events, the authoring kit (`vs_core.api`) |
| `libs/vs-runtime/` | The shell that drives core: request executors, workspaces, sessions, evaluation, cleanup |
| `libs/vs-agent/`, `libs/vs-sandbox/`, `libs/vs-project/` | Agent harnesses, isolated execution, and project persistence |

A strategy imports `vs_core.api` and the product's shared policy packages; it never
constructs agent clients, sandboxes, worktrees, or run resources. Runtime libraries do not
import VibeSys policy.
[`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml) enforces the
dependency direction. The design behind this contract, and the vocabulary (decision,
attempt, settlement, operation, adoption, suspension), is in
[Running single, multi, evolve and issue_queue on the vs-core lifecycle](../design/strategies-on-core.md).

## Plugin declaration

An `OrchestrationPlugin` is the complete declaration of one policy:

- `id` and `config_version`: persisted in the run manifest, so change them deliberately;
- `options`: a strict Pydantic model; unknown keys are rejected;
- `agents`: the sole role catalog, a tuple of `AgentRole`. There is no separate agent
  registry; the product's `OrchestrationRegistry` selects plugins by stable ID and does not
  redeclare their roles;
- `core`: a `CorePolicy`, the declarative data that makes the plugin a run.

A `CorePolicy` carries no loop and no service. It holds `plan` (a pure function of the
run's `CoreRunContext` that returns the strategy, its limits, reply schemas, facts and the
measurement recipe), the operations the strategy registers and the runtime role behind
each, the retention label of a verified revision, the prompt template directory, and the
workspace directories a write-artifacts turn may write (`artifact_directories`, which the
run also preserves across restores).

Registration validates the policy and names the offending key (for example
`core.artifact_directories[0]`) before any run resources open. The registration may also
carry a resume policy, a round-count projection and a projector that reads the run's
committed record for live and historical views.

The projector returns the strict `vibesys.api.PluginProjection` contract. Its `iterations`
field is the policy-owned typed navigation tree; `RoundSummary` remains a compatibility
projection for older consumers. Every built-in registration declares an `IterationSupport`
disposition. Catalog construction rejects `UNMAPPED`, so a new built-in cannot silently
publish empty navigation. A policy whose required state is held outside its durable model
returns typed unavailability instead of reconstructing or duplicating that external state.

## What a strategy declares

A strategy declares its phases, its turn table and its settle policy, and the runtime
handles corrections, lost turns, suspension, checkpoints, refunds, stop and exhaustion. The
shared pieces live in `vs_core.api` (identity grammar, decision builders, turn chains,
attempt tracking, evidence views) and in sibling packages `vibesys.candidates`,
`vibesys.hypothesis` and `vibesys.metrics`. Time comes from `view.run.now_at`; a strategy
has no clock, I/O or randomness of its own.

Agents that measure a candidate in the middle of a turn do it through the evaluation tool
(`submit_evaluation`, then `validate_evaluation_wait` and a waiting reply). The run's
evaluation bridge answers it on the host; core resumes the same conversation once the
measurement ends. On every run environment the agent is given no shell client for
evaluation, and the framework's own trusted measurement runs through the core evaluation
executor.

## Policy organization

Keep code near the strategy that owns the decision:

- `roles` (or `*_roles.py`): immutable role declarations;
- `plugin.py`: the plugin declaration and registration;
- `strategy/`: the pure strategy, its state and its transitions;
- `core_policy/`: the plan, limits, projection and reply parsing the runtime joins to the
  strategy, with `prompts/` for the agent-visible templates;
- `models.py` or focused policy modules: options, reply schemas, deterministic transitions.

Every direct subfolder of `vibesys.orchestration` is one strategy package. Shared policy and
resources live beside it under `vibesys`, including `hypothesis`, `metrics`,
`profile_focus`, `steering`, `domains`, and shared `prompts`; these siblings never import
orchestration. Follow the layout and placement rule in [architecture.md](architecture.md).
Extract shared policy only when real callers need the same semantics.

## Adding a plugin

1. Create `src/vibesys/orchestration/<plugin>/` with strict Pydantic options, role
   declarations, prompts, the strategy and its core policy.
2. Construct one `OrchestrationPlugin` with a `CorePolicy`; its `agents` tuple is
   authoritative.
3. Export its product registration and add it to `launch.built_in_orchestrations` through
   the individual registration facade `vibesys.api.catalog`. Declare its iteration
   disposition and, when projected, map durable policy state to stable typed IDs, parent
   links, sibling ordinals, status, and bounded headline metrics.
4. Add the strategy to the conformance suite and its pure-tier tests (see the `testing`
   skill), and wiring tests only when the boundary itself changes.
5. Add any new module edge to `tach.toml` and run `uv run python scripts/check_tach.py`.

A preset that shares an implementation but changes policy options should be a separate
plugin with its own stable ID and options type.

## Testing a strategy

Drive the strategy as the shell does: build `RunView`s, feed `StrategyEvent`s, and assert on
the proposals. `vs_runtime.api.testing` offers the runtime shell kit (`ScriptedExecutors`,
`drive_shell`, restart-at-boundary) for tests that run a strategy against scripted
executors, and `tests/support/core_plugins.py` offers stub core plugins for tests of the
wiring around a run (registration, launch, projection, resource lifetime) that need a plugin
that composes and ends rather than one that searches.
