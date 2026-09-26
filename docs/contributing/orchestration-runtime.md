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

`tach.toml` enforces this direction as an import ratchet, grouped into four
layers: `loops` > `roles_search` (`roles/` and `search/` together) >
`orchestration_host` > `prompts_and_lower_libs` (`prompts/` and the
mechanics libraries below it). A module in a layer may depend only on
modules in the same or a strictly lower layer; `uv run tach check` fails a
PR that adds an upward edge.

## loops/: strategy replaceability

A strategy is its folder + one registry line + its own prompt folder. Four
strategy folders are registered today, each a peer of the others: `multi-agent`
(`loops/multi/`), `single-agent` (`loops/single/`), `plain`
(`loops/issue_queue/`), and `evolve` (`loops/evolve/`). `loops/registry.py`
is the only module that imports every strategy; peers never import each
other and nothing outside `loops/` imports a strategy package directly
(`vibesys.loops.registry.built_in_orchestrations()` is the only doorway in).

Profiling is an option of `multi` and `single`
(`AgentOrchestrationOptions.profile_guided`), not a separate strategy
folder: when set, a strategy composes `roles/` with `search/hypothesis` and
`search/profile_focus` directly. `profile-guided-multi-agent` and
`profile-guided-single-agent` stay registered as presets, each a peer
registry entry that requires `profile_guided` and keeps its own
orchestration ID and state namespace (`profile_multi`, `profile_single`) so
existing runs and option files keep working unchanged; they run the same
`Orchestrator` subclass and prompts as `multi`/`single`, not a separate
folder.

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

`search/hypothesis/search.py`'s `HypothesisSearch` is the public facade over
hypothesis-lifecycle transitions; `multi` and `single` hold one, built from a
`HypothesisConfig`. `resume(state, metric_space)` recovers a run's last
committed `HypothesisState` (or starts fresh from `initial()` when there is
none) and reprojects it onto the current `MetricSpace`, a no-op when the
metric space is unchanged so calling it unconditionally on every open is
safe; `initial_carry(records)` seeds the resumed carry-over from any pending
workspace rollback notice left in the round history.

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

**`ctx.workspaces.<handle>.restore_or_warn(revision, *, clean=True, preserve_paths=(), round_label=None)`**
is for rollback-style restores that must not abort the run: it restores to
*revision* like `restore`, but on a failed checkout it publishes a framework
warning instead of raising `WorkspaceRestoreError`, returns `False`, and
leaves the caller free to retry the same restore on a later round.

**`ctx.gates.run(*, round_number, retry, commit, objectives, record, ...)`**
runs the accuracy gate then the benchmark gate, recording each outcome once
through the `GateRecorder` protocol the caller supplies, and returns a typed
`GateRunResult`. It shares the same lock domain as
checkpoint/adopt/snapshot, so gate execution and parent-tree Git mutation
never race. It also flushes `ctx.progress`'s pending blocks (see below).

**`ctx.state.commit(*, sequence, writes, ...)`** checkpoints typed writes
(journal, then Git commit, then publish) and, from the `RunView` diff
between the previous and newly published state, emits `ROUND_FINISHED` for
every newly completed round and `EXPERIMENTS_CHANGED` when the experiment
revision moved. Strategies with no round/revision concept (`evolve`,
`issue_queue`) see no events derived here. `ctx.state.checkpoint` is the
lower-level primitive without that event derivation. Like `ctx.gates.run`,
it flushes `ctx.progress`'s pending blocks.

**`ctx.progress`** is the host-owned pending framework-log buffer. A
strategy calls `ctx.progress.declare(path)` once, early, to name its
progress-board path, then `ctx.progress.note(block)` as pure
`orchestration/progress_log.py` `render_*` blocks become available; only
`ctx.state.commit` and `ctx.gates.run` drain and write those blocks to disk,
so a strategy never renders or writes the board itself.

**Declared agent memory**: `RunSetup.memory_paths` names workspace-relative
paths a strategy writes agent memory into once; the host preserves them
across `workspaces.transaction`/restore/adopt, instead of every call site
passing `preserve_paths=...` itself. The one caller that opts out is
`ctx.agents.turn`'s `ReadOnly`-role isolation revert, which restores with
`preserve_memory=False`: a role with no write access should not be able to
plant an unauthorized file inside a memory path and have it survive as
"preserved" instead of reverted. `orchestration/memory.py` owns these
paths: the roadmap and the per-round progress log, each supporting two
layouts (`RunSetup.memory_paths`'s `layout` argument) so a run stays
scannable at scale, `roadmap.md` + `progress.md` (compact) or
`roadmap/index.md` + `progress/round-NNNN.md` (directory, one file per
round). `memory.structured_artifact_root` resolves the framework-owned
directory beside (or, for legacy `progress.md` runs, a sibling of) the
progress log, shared with `orchestration/artifacts.py`.

**Typed turn artifacts**: `orchestration/artifacts.py` is where a
strategy's designer, implementer, and judge roles hand off large evidence
through files instead of prompt text: a plan, parsed implementer claims,
framework-executed validation results, and the profiler root, one category
subdirectory per kind under the structured artifact root. Every typed
writer goes through the same atomic-write primitive (`write_json` /
`write_model`). `write_implementer_start_marker` writes an
`ImplementerStartMarker` (round, attempt) *before* an implementer turn
runs, so a crash mid-turn still leaves a durable record that the attempt
began, letting resume recover cleanly instead of silently losing it.

**Tool serving**: `orchestration/tools.py` turns a strategy's generic MCP
tool descriptor (`vs_agent.expose_as_tools`, a `StdioServerDescriptor`) into
the concrete `MCPServerSpec` a driver launches: the one place that
construction happens, so no strategy hand-builds an `MCPServerSpec`.

## Adding a strategy

First check whether this is really a new strategy or a variant of an
existing one. If it runs the same round control and prompts as an existing
strategy with one policy toggle on (as `profile-guided-multi-agent` does
for `multi`), add an option plus a registered preset instead: extend
`AgentOrchestrationOptions`, branch on it inside the existing strategy
folder, and register a second `registry.register(...)` line with its own
orchestration ID, state namespace, and projector, pointing at an
`Orchestrator` subclass in the *same* folder. Do not start a new
`loops/<strategy>/` folder for a variant; that only earns its own folder
when its round control or prompts genuinely diverge.

For a genuinely new strategy:

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
monkeypatching: `agent_client_factory`, `backend_factory`, and
`gate_executor` are optional constructor overrides that default to the real
`build_agent_client`, `vibesys.backends.get`, and the real trusted
accuracy/benchmark command executor. Pass a `FakeAgentClient`
(`vs_agent.api.testing`), a `FakeComputeBackend` (`vibesys.api.testing`,
wrapping a `FakeSandbox` from `vs_sandbox.api.testing`), and/or a
`FakeGateExecutor` (`vibesys.orchestration.fake_gates`, also re-exported
from `vibesys.api.testing`) to drive a real strategy's `run(ctx)` end to end
without a real agent CLI, sandbox, or gate command.

`tests/vibesys/golden/` drives every registered strategy this way against a
scripted `FakeAgentClient` and asserts the run's written state and emitted
events against a stored snapshot. Regenerate snapshots with
`UPDATE_GOLDEN=1 uv run pytest tests/vibesys/golden`; review the diff before
committing an update.
