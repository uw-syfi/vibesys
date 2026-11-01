# Orchestration strategies and the runtime

Every VibeSys orchestration is a search on the `vs-core` lifecycle. `dynamic` and `issue_queue`
are written as flows: one `@search(...)` function each that runs top to bottom, which the runtime
journals and replays (see [flows.md](flows.md)). The other searches (`evolve`, `multi`,
`single`) are still state-machine strategies and are being ported to flows one at a time; the
rest of this page describes that older form wherever it says "strategy", and says "search" where
both forms apply. A strategy is a pure
object that decides the next step from a read-only view of the run; the runtime owns the
loop that executes those steps (agent turns, workspaces, measurements, operations) and
feeds the outcomes back as events. A plugin is the declaration that joins a strategy to its
roles, options and prompts. There is no other way to run a policy: a plugin without a
`core` policy is rejected, and the older imperative contract (`orchestrate(run, options)`
over a `Run` bundle of sessions, state and evaluation) no longer exists.

The boundary is deliberate:

| Owner | Holds |
|---|---|
| `src/vibesys/orchestration/<search>/` | The search: a flow in `search/` (or, until ported, a state-machine strategy in `strategy/`), its core policy (`core_policy/`), prompts, options and plugin declaration |
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

## What a flow search declares

The dynamic search shows the layout. Everything about the search sits in
`src/vibesys/orchestration/dynamic/search/`, whose public surface is `search/api.py`:

- `_search.py`: the root flow, `dynamic = @search("dynamic", config=DynamicConfig,
  operations=OPERATIONS, roles=ROLES)`. The remaining `_*.py` modules are flows for one
  workstream (plan, implement, review, measure) and the pure helpers they share (run record,
  plan validation, parent choice, prompt contexts). They are private to the package.
- `_roles.py`: one `Role` per agent (`PLANNER`, `IMPLEMENTER`, `JUDGE`, `PROFILER`), beside the
  search that asks them. A role holds the prompt template, the reply model, the workspace mode,
  access, turn time, corrections, lost turns and charge. Role ids are `dynamic-<role name>`.
  The host adds only what it alone can in `src/vibesys/dynamic_roles.py` (system prompts, tools,
  capabilities).
- `_config.py`: `DynamicConfig`, the search's own knobs: `max_rounds`, `max_in_flight`,
  `evaluation_slots`, `max_backlog`, `pick`, `input_wait_seconds`, `judge_every`,
  `max_unmeasured_turns`, `max_continuation_rounds`, `profiling`.
- `_queue.py` and `_reports.py`: the run's trusted evaluations. A workstream submits its
  candidate on the `REPORTS` channel and parks (within `max_backlog`, its agent slot goes to new
  work, as it does while an agent turn is suspended on its own evaluations, until the root
  grants its resume a free slot on `RESUMES.of(...)`); the root's `EvaluationQueue` binds a free evaluation slot to the candidate the pick
  policy chooses (the newest, or the orchestrator's `evaluation_order`) and answers each
  workstream on its own `VERDICTS.of(key)` latch. A newer candidate of the same line drops the
  waiting one as superseded.
- `_budget.py`: `budget(config)`, what the run may spend, derived from the config in one place.
- `_prompts.py`: the typed prompt contexts the search sends. The search never builds prompt
  text; templates are rendered from `core_policy/prompts/`.
- `_run_state.py`: the `RunRecord` the flow folds and the `RunSnapshot` it publishes with
  `ctx.publish`; status views and tests read the snapshot, not a persisted state class.

The old state-machine package `orchestration/dynamic/strategy/` is deleted, with
`DynamicStrategyState`, `DynamicOptions` and `from_options`, the child-routing and fold code, and
`dynamic_core.resolve_core_policy`. `DynamicStrategyState` is replaced by core's journaled
`FlowState`, which the runtime replays.

Where each setting lives now:

| Setting | Before | Now |
|---|---|---|
| Rounds, width, review cadence, give-up rules, profiling | `DynamicOptions` (one model for the operator, the strategy and the task) | `DynamicConfig` in `search/_config.py` |
| Operator keys at launch | `DynamicOptions` | `DynamicLaunch` in `dynamic/launch.py`: `DynamicConfig` plus the task's inputs (`interface`, `modality`, `operator_constraints`, `metric_space`, `profile_guided`), flat keys, unknown keys rejected |
| Gates, metric space, stage limits, queue allowance | `CandidateConfig` and the dynamic options | `vs_core.api.TaskTerms` on the run's facts, built by `vibesys.candidates.api.terms_of` and read by flows as `ctx.task.terms` |
| Turn time, corrections, lost turns, charge | per-role seconds and re-ask fields in the config | the `Role` |
| Infrastructure retries (node faults, lost jobs) | the config and core | the backend |
| Run budget | a formula copied into each search | `budget(config)` |

Host wiring for `dynamic` is `src/vibesys/dynamic_core.py` (builds the run plan: the search, its
config, the run facts and limits), `dynamic_roles.py`, and the dynamic `core_policy/` (prompt
variables, evidence requirements, and the projection from `RunSnapshot`). Core, not the host,
ends the run from the winner the flow chose with `ctx.choose`.

`issue_queue` (search name `plain`) has the same layout in
`src/vibesys/orchestration/issue_queue/search/`: the root `issue_queue` in `_search.py` reads the
board, asks the pure `Queue` what the board calls for and runs each issue or performance job as a
flow of its own (`_issue.py`, `_perf.py`); `_board.py`, `_writes.py`, `_queue.py` and the other
helpers are pure. The board's operations belong to the tracker library: the search lists them in
`owned=` and its flows ask their owner with `ctx.owners`. Its old `strategy/` package,
`IssueQueueStrategyState` and `IssueQueueOptions` are gone; the operator's keys are
`IssueQueueLaunch` (`issue_queue/launch.py`: `IssueQueueConfig` plus `modality` and `tracker`,
unknown keys rejected).

`single` and `multi` share their designer (`src/vibesys/serial_search/`), and with it the
designer's planning state, the roadmap: Majors (directions) and the Minors that block them, each
with a status (`todo`, `in_progress`, `done`, `parked`, `abandoned`). The roadmap sits behind the
`Roadmap` interface of the tracker library (`vs_issue_tracker.api`), which exposes typed
`RoadmapItem`s and never a storage layout:

- The designer's role reads only. Its prompt shows the items, and its reply's `roadmap` field
  lists the items to create or replace by id. The search checks the change with the pure
  `roadmap_problem` (a refusal is corrected like a plan the ledger refuses) and then puts it.
- The search reaches the roadmap only through the `ReadRoadmap` and `PutRoadmapItems`
  operations, declared `owned=ROADMAP`. A put is idempotent, so a replay is safe.
- The owner (`src/vibesys/serial_roadmap.py`) performs them over a `DocumentRoadmap` kept in the
  run's portable state (namespace `roadmap`), opened through `Project`. It is per run, survives a
  restart or a resume, and is untracked VibeSys state, so it never makes the project a dirty
  baseline for the next run.
- Another backend (GitHub or Linear issues, say) is a new `Roadmap` implementation that passes
  `libs/vs-issue-tracker/tests/test_roadmap_contract.py` and is chosen in `roadmap_entries`; the
  searches, the operations and the prompts stay as they are.

To add a search, follow [flows.md](flows.md) and the steps under "Adding a plugin" below.

## What a strategy declares (state-machine form)

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
- `search/`: a search written as a flow, with its roles, config and pure helpers, exposed only
  through `search/api.py`;
- `strategy/`: for a search not yet ported, the pure strategy, its state and its transitions;
- `core_policy/`: the plan, limits, projection and reply parsing the runtime joins to the
  strategy, with `prompts/` for the agent-visible templates;
- `models.py` or focused policy modules: options, reply schemas, deterministic transitions.

Every direct subfolder of `vibesys.orchestration` is one strategy package. Shared policy and
resources live beside it under `vibesys`, including `hypothesis`, `metrics`,
`profile_focus`, `steering`, `domains`, and shared `prompts`; these siblings never import
orchestration. Follow the layout and placement rule in [architecture.md](architecture.md).
Extract shared policy only when real callers need the same semantics.

## Adding a plugin

1. Create `src/vibesys/orchestration/<plugin>/` with strict Pydantic options, prompts, its core
   policy and a `search/` package: the `@search` flow, its `Role` declarations and an `api.py`
   that lists the public surface. Flow code takes `ctx` first and uses only `ctx`; see
   [flows.md](flows.md).
2. Construct one `OrchestrationPlugin` with a `CorePolicy`; its `agents` tuple is
   authoritative.
3. Export its product registration and add it to `launch.built_in_orchestrations` through
   the individual registration facade `vibesys.api.catalog`. Declare its iteration
   disposition and, when projected, map durable policy state to stable typed IDs, parent
   links, sibling ordinals, status, and bounded headline metrics.
4. Add the search to the conformance suite and its tests: flow stories with
   `vs_core.api.testing.FlowRun` plus property tests of the pure helpers (see the `testing`
   skill), and wiring tests only when the boundary itself changes.
5. Add any new module edge to `tach.toml` and run `uv run python scripts/check_tach.py`.

A preset that shares an implementation but changes policy options should be a separate
plugin with its own stable ID and options type.

## Testing a flow search

A flow search is tested with `vs_core.api.testing.FlowRun` (see "Testing a flow" in
[flows.md](flows.md)); the dynamic stories are in `tests/vibesys/orchestration/dynamic/`.
`tests/architecture/test_search_packages.py` checks that a search package imports `vs_core` only
through `vs_core.api.search`, declares `api.py`, and that nothing outside imports its private
modules; `tests/architecture/test_flow_code_is_restricted.py` checks the flow code's imports.

## Testing a strategy (state-machine form)

Drive the strategy as the shell does: build `CoreView`s, feed `StrategyEvent`s, and assert on
the proposals. `vs_runtime.api.testing` offers scripted executors (`ScriptedExecutors`) and
`vs_dst` hosts a strategy over them on the step loop, killing, pausing or stopping it at any
boundary, for tests that run a strategy against scripted executors, and `tests/support/core_plugins.py` offers stub core plugins for tests of the
wiring around a run (registration, launch, projection, resource lifetime) that need a plugin
that composes and ends rather than one that searches.
