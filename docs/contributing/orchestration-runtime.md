# Orchestration runtime

Every run starts from one frozen `RunRequest`. Its `orchestration` descriptor
carries a stable ID, `config_version`, and JSON policy options; `RunRequest`
itself owns host execution settings (model, agent/compute backends,
provider, profiler selection, feature flags, skill directories) that a
policy reads through `ctx.request` rather than duplicating as descriptor
options. Only v4 run manifests are supported.

`src/vibesys` splits into five layers. Dependency direction is strict: a
layer imports only the ones below it.

| Layer | Holds | Must not hold |
|---|---|---|
| `loops/<strategy>/` | orchestration: which roles run, in what order, with which workspace and search tools | effects outside `RunContext`; imports of another strategy |
| `roles/` | `Role` declarations, typed prompt context models, reply schemas (data only) | turn execution, sequencing, state transitions |
| `search/` | pure, deterministic guidance: `hypothesis/`, `profile_focus/`, `population/` | `RunContext`, agents, prompts, filesystem, clocks, global RNG |
| `prompts/` | every Jinja template, under `roles/<role>/`, `loops/<strategy>/`, and a `shared/` fallback root | Python policy |
| `orchestration/` (the host, `RunContext`) | all side effects and invariants | strategy knowledge |

## loops/: strategy replaceability

A strategy is its folder + one registry line + its own prompt folder. Six
strategies are registered today, each a peer of the others: `multi-agent`
(`loops/multi/`), `single-agent` (`loops/single/`), `profile-guided-multi-agent`
(`loops/profile_multi/`), `profile-guided-single-agent`
(`loops/profile_single/`), `plain` (`loops/issue_queue/`), and `evolve`
(`loops/evolve/`). `loops/registry.py` is the only module that imports every
strategy; peers never import each other and nothing outside `loops/` imports
a strategy package directly (`vibesys.loops.registry.built_in_orchestrations()`
is the only doorway in).

`profile_multi` and `profile_single` are deliberately kept as separate peers
rather than folded into `multi`/`single`: they compose `roles/` with
`search/hypothesis` and `search/profile_focus` directly, so they stay small.

## roles/: declarations, not execution

Each module in `roles/` is one role *family* (`designer`, `pre_round`,
`implementer`, `judge`, `profiler`, `single_agent`, `perf_eval`, `mutator`,
`common`). A role is a `vibesys.runtime.Role`: a template path, a pydantic
reply type, a fallback, a typed prompt-context model, workspace-access
policy, and session policy. Different prompts or reply types are always
different roles, never one role branching on which strategy called it. A
strategy invokes a role through `ctx.agents.turn(role, ...)`; it never
hand-rolls rendering, isolation, timeout fallback, or correction retries.

```python
MULTI_JUDGE = Role(
    id="judge",
    template="loops/multi/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_hypothesis_judge,
    context=JudgeContext,
    access=ReadOnly(),
    session=Fresh(),
    filter_skills=True,
)
```

`roles/__init__.py` collects `ALL_ROLES` from every family; a test asserts
every role there is used by at least one registered strategy (no dead
roles), and that `roles/` never imports `vibesys.orchestration` or
`vibesys.loops`.

## search/: pure guidance, no shared interface

`search/hypothesis/`, `search/profile_focus/`, and `search/population/` are
each their own API; there is no common interface across them. Rules,
enforced by an architecture test:

- no imports of `vibesys.orchestration`, `vibesys.loops`, `vibesys.roles`,
  or `vibesys.prompts`: search answers questions and returns new state, it
  never drives agents, renders prompts, or decides which agent runs next;
- no `os`/`subprocess`/`pathlib`/`time`/`datetime` imports: no I/O, no
  clock reads;
- state is a serializable pydantic value that orchestration persists.
  Where a search style needs randomness, its RNG state lives inside that
  state value (module-level `random.*` calls are forbidden; a `random.Random()`
  instance must immediately restore its state from the persisted value), so
  resume is deterministic.

`search/population/openevolve_selector.py` follows this: it reconstructs
OpenEvolve's `ProgramDatabase` from `OpenEvolveSelectorState.files` (an
in-memory dict of the same relative-path-to-JSON-text shape the upstream
database would otherwise write to disk), mutates it, and serializes it back
into that same state value. There is no directory on disk this module owns
and no snapshot history: `files` always holds the complete current database,
so state size is bounded by the database's own size limits, not by how many
times `admit` has been called.

## prompts/: centralized templates

All Jinja templates live under `prompts/`: `prompts/roles/<role>/`,
`prompts/loops/<strategy>/`, and `prompts/shared/` as the fallback root a
role's template resolves against when it isn't found in the strategy's own
folder. `prompts/renderer.py` and `prompts/contexts.py` are the only Python
in this layer.

## orchestration/: the host

`RunContext` (`orchestration/runtime.py`, assembled from `agents.py`,
`workspaces.py`, `gates.py`, `state.py`, `control.py`, `tools.py`, ...) owns
every side effect. `OrchestrationRegistry` maps each registered ID to its
`Orchestrator` class and optional read projector; each `Orchestrator`
declares a `RunSetup` (durable state namespace and typed state slots, resume
comparator, optional recovery hook, `memory_paths`) consumed when the host
opens the run. Strategies call these verbs instead of hand-rolling
sequences:

**`ctx.agents.turn(role, *, agent, context, label, ...)`** runs one role
turn end to end and returns a typed reply. It owns:

- rendering `role.template` (with `role.context`/the passed `context`)
  through `render_template`, or through `Prompt(template_dir, backend)` when
  a `backend` is given, for compute-fragment injection;
- the paid-work marker (`before_paid`, invoked when `role.paid`) right
  before the pre-turn snapshot, so a crash after it still resumes from a
  committed tree;
- workspace isolation: a pre-turn snapshot, and for a `ReadOnly` role,
  reverting unauthorized changes after the turn (raising `RoleIsolationError`
  if they cannot be reverted); `access.allow` names paths the role may still
  write;
- timeout fallback: `subprocess.TimeoutExpired` resolves to
  `role.timeout_fallback(seconds)` if declared, else `role.fallback()`, for
  every role, not just the strategies that used to special-case it;
- structured-reply correction retries while `role.check(reply)` returns an
  error, up to `role.max_corrections`, raising `CorrectionExhaustedError` if
  exhausted;
- skill-selection filtering when `role.filter_skills`;
- recording the turn (lifecycle events plus a turn artifact).

**`ctx.workspaces.<handle>.transaction(*, preserve=(), label=...)`** is an
async context manager: snapshot on entry, restore to it on exit unless the
body calls `tx.commit()`. Declared agent-memory paths
(`RunSetup.memory_paths`) are always preserved on that exit restore, in
addition to `preserve`. A failed restore raises `WorkspaceRestoreError`
consistently, regardless of which strategy triggered it.

**`ctx.gates.run(*, round_number, retry, commit, objectives, record, ...)`**
runs the accuracy gate then the benchmark gate, recording each outcome once
through the `GateRecorder` protocol the caller supplies, and returns a typed
`GateRunResult`. It shares the same lock domain as
checkpoint/adopt/snapshot, so gate execution and parent-tree Git mutation
never race.

**`ctx.state.commit(*, sequence, writes, ...)`** checkpoints typed writes
(journal, then Git commit, then publish) and, from the `RunView` diff
between the previous and newly published state, emits `ROUND_FINISHED` for
every newly completed round and `EXPERIMENTS_CHANGED` when the experiment
revision moved. Strategies with no round/revision concept (`evolve`,
`issue_queue`) see no events derived here. `ctx.state.checkpoint` is the
lower-level primitive without that event derivation.

**Declared agent memory**: `RunSetup.memory_paths` names workspace-relative
paths a strategy writes agent memory into once; the host preserves them
across `workspaces.transaction`/restore/adopt, instead of every call site
passing `preserve_paths=...` itself.

**Tool serving**: `orchestration/tools.py` turns a strategy's generic MCP
tool descriptor (`vs_agent.expose_as_tools`, a `StdioServerDescriptor`) into
the concrete `MCPServerSpec` a driver launches: the one place that
construction happens, so no strategy hand-builds an `MCPServerSpec`.

The former progress board (`agent_run/issue_board.py`) has split into three
host-owned modules, `loops/` no longer calls it directly: declared agent
memory (`orchestration/memory.py`: roadmap, progress log, Pareto archive),
typed turn artifacts (`orchestration/artifacts.py`: plan, implementer
evidence, validation ledger, profiler root), and the framework log
(`orchestration/progress_log.py`, already split out earlier). `agent_run/`
has dissolved entirely.

## Adding a strategy

1. Create `loops/<strategy>/` with an `Orchestrator` implementing
   `run(ctx) -> bool` and (if it has durable state) a projector.
2. Create `prompts/loops/<strategy>/` for any templates not already covered
   by `prompts/shared/`.
3. Register it in `loops/registry.py`: one call to `registry.register(...)`.
4. Reuse existing `roles/` and `search/` where the strategy's steps match an
   existing role family or search style; add a new role/search module only
   for genuinely new behavior.
5. Do not import another strategy package. Do not add a new cross-strategy
   abstraction to make this easier: `tests/vibesys/architecture/` will
   reject both.

## Adding a role

1. Add it to the role family module it belongs to (or start a new family
   module for a genuinely new job).
2. Give it its own template, reply schema, and typed context model; never
   reuse another role's prompt/reply pair by branching on the caller.
3. Add it to that module's `ALL_ROLES` tuple, which `roles/__init__.py`
   aggregates.
4. Have at least one registered strategy call it through `ctx.agents.turn`;
   an unused role fails the roles-catalog check.

## Architecture tests

`tests/vibesys/architecture/` enforces the rules above with static `ast`
scans (no import of the scanned packages required):

| Test | Checks |
|---|---|
| `test_loops_replaceability.py` | no strategy package imports another; nothing outside `loops/` imports a strategy directly |
| `test_prompt_folder_registry_parity.py` | every registered strategy has a `prompts/loops/<strategy>/` folder, and vice versa |
| `test_roles_boundaries.py` | `roles/` never imports `vibesys.orchestration`/`vibesys.loops`; the role catalog has no dead entries |
| `test_search_purity.py` | `search/` never imports `orchestration`/`loops`/`roles`/`prompts`, does no I/O, and never reads or mutates global RNG state |

## Testing

Prefer injecting fakes through `run_orchestration`'s seams over
monkeypatching: `agent_client_factory` and `backend_factory` are optional
constructor overrides that default to the real `build_agent_client` and
`vibesys.backends.get`. Pass a `FakeAgentClient`
(`vs_agent.api.testing`), a `FakeComputeBackend` (`vibesys.api.testing`,
wrapping a `FakeSandbox` from `vs_sandbox.api.testing`) to drive a real
strategy's `run(ctx)` end to end without a real agent CLI or sandbox.

`tests/vibesys/golden/` drives every registered strategy this way against a
scripted `FakeAgentClient` and asserts the run's written state and emitted
events against a stored snapshot. Regenerate snapshots with
`UPDATE_GOLDEN=1 uv run pytest tests/vibesys/golden`; review the diff before
committing an update.
