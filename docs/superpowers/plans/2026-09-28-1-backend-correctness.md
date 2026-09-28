# Backend Correctness (Sub-project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reopened finished run serves its experiments, performance, and design without writing anything; the `project_attached` signal fires only once data is queryable, and clients stop waiting when a run ends before that; reopened gateways never collide with the live one or with each other; the demo reopens as a real recorded project.

**Architecture:** Reopen gains a run identity (`--web-reopen-run RUN_ID`, project from `--project` or the working directory). The entrypoint resolves the record with `open_run_store(Project.open(root)).get_record(run_id)` (the same call `_run_ready` uses), checks that the journal belongs to that run, and hands the record to `RunController.attach_read_only`, which checks every event's run id and stores it as `attached_run` without lifecycle writes. Two read paths that created state on lookup are made non-creating at the root: `ProjectState` prepares the state home only when a machine-local path is requested, and the record reads its effective objective with `read_bytes` instead of the materializing `external_directory()`. The `EXPERIMENTS_CHANGED(project_attached)` emit moves below `integration.publish_resources`, whose listener attaches the record synchronously; the TUI and web clients settle a pending experiments view when the run ends without it. Discovery records gain optional `run_id` and `mode` keys; reopens default to `.vibesys/web-gateway-<run-id>.json` (or a hash of the canonical log path when only a journal is given), the gateway records the requested project root, and the launcher refuses to reuse a record serving a different run or mode. The demo becomes a checked-in recorded project plus the existing journal, reopened through the identity path.

**Tech Stack:** Python 3.12, pytest, stdlib `json`/`hashlib`/`shutil`; TypeScript (Bun tests) in `clients/tui` and `clients/web`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-28-web-app-design.md` (section "1. Backend correctness", Testing bullet "1").

## Global Constraints

- No new dependency, module, or abstraction. Reuse `open_run_store(project).get_record(run_id)` as `_run_ready` does (`src/vibesys/api/_session.py:398`).
- Reopen is read-only: record lookup, journal attach, and experiment/performance/design queries create no file or directory in the project, the log directory, or the state home.
- Discovery records keep `version: 1`; `run_id` and `mode` (`live` or `reopen`) are optional keys; `_read_record` keeps ignoring unknown keys so `web stop`/`status` keep working with old and new records.
- `entrypoints` reaches core only through `vibesys.api`; `server.*` layering unchanged (tach). The one library edit is inside `vs_project` (`libs/vs-project/src/vs_project/_state.py`).
- Every new `monkeypatch.setattr` has a `# test-isolation: <reason>` comment on the line above (`scripts/check_test_isolation.py`). New `# noqa` waivers: exactly two, `LW-101105` (`PLR0913`, Task 3) and `LW-101106` (`TRY003`, Task 1); both ids are unused (grep confirms). Each carries its rationale and the rejected alternative (`docs/contributing/coding-best-practices.md`, Ratchets).
- The web `Rail` is replaced in sub-project 3: keep the web change to the derived rail state plus one render branch.
- Docs: no em dashes; links inside `docs/` follow `docs/contributing/coding-best-practices.md`.
- Commands run from the worktree root `/Users/grootbeat/Documents/vibesys-wt/web-ui`. Its `.venv` is a real directory (not a symlink to the main checkout), so no `PYTHONPATH` prefix is needed; confirm once with `uv run python -c "import server; print(server.__file__)"` (must print a path under `vibesys-wt/web-ui/src`; if it prints the main checkout, prefix every Python command with `PYTHONPATH=$PWD/src`).
- Sandbox notes: the worktree is outside the Bash sandbox write allowlist, so pytest (writes `.coverage` at the root) and `git commit` need the sandbox off. If `uv run` fails on `~/.cache/uv`, use `.venv/bin/python -m pytest ... --no-cov` with the same arguments. Tests that bind loopback ports or Unix sockets need local binding allowed.

## Review Focus

1. Unknown or malformed run id (including `../escape`) on `--web-reopen-run`: a configuration error before any discovery file, lock, or detached child exists. Task 1 (`test_web_reopen_run_rejects_an_unknown_run_before_launching`, `test_web_reopen_run_rejects_an_unsafe_run_id_before_discovery`).
2. Known run whose journal is missing, or a journal from another run passed with `--web-reopen`: a configuration error before launch, never a transcript of one run over another's data. Task 1 (`test_web_reopen_run_requires_the_recorded_event_journal`, `test_web_reopen_run_rejects_another_runs_journal`, `test_reopen_rejects_a_journal_from_another_run`).
3. `--web-reopen-run=RUN` (equals form) from a launcher: counts as a web request like the space form. Task 1 (`test_web_requested_accepts_reopen_flags_in_both_forms`).
4. Discovery record written by an older gateway (no `run_id`/`mode`) or with an unknown `mode`: the old record reads as `live`, `run_id=None`, and `web status`/`web stop` work on it; an unknown mode reads as no record. Task 3 (`test_instance_record_round_trips_run_identity_and_mode`, `test_status_and_stop_accept_old_and_new_records`).
5. A run that fails before its resources publish: the TUI experiments pane and the web rail leave their waiting state with a failure message, and an ended run that did attach still shows its data. Task 2 (`stops loading experiments when the run ends before its project attaches`, `shows recorded experiments for an ended run without a failure`, rail-state test).

---

## File Structure

| File | Change |
|---|---|
| `libs/vs-project/src/vs_project/_state.py` | prepare the state home lazily, on machine-local path requests |
| `src/vibesys/api/_store.py` | read the effective objective without materializing a directory |
| `src/server/controller.py` | `attach_read_only(log_dir, *, record=None)`; reject events from another run |
| `src/server/runtime.py` | `read_only_record`, `project_root` options; forward record, identity, mode, project root |
| `src/entrypoints/server.py` | `--web-reopen-run`, `_project_root_from_argv`, `_reopen_from_argv`, run-specific instance path, reuse guard |
| `src/vibesys/run/resources.py` | move the `project_attached` emit below `publish_resources` |
| `clients/tui/src/session-controller.ts` | settle a pending experiments log when the run ended unattached |
| `clients/web/src/{model.ts,derive.ts,App.tsx,ui/Rail.tsx}` | `ended-unattached` rail state |
| `src/server/transport/discovery.py`, `src/server/transport/websocket.py` | optional `run_id`, `mode` |
| `src/entrypoints/web.py` | demo reopens the recorded project with identity |
| `clients/web/src/fixtures/demo-project/**` | recorded demo project (generated once) |
| tests under `tests/`, `libs/vs-project/tests/`, `clients/*/src/*.test.ts`; `tests/vibesys/golden/snapshots/events/**` | tests and regenerated goldens |
| `docs/contributing/tui-architecture.md`, `docs/contributing/web-development.md` | flag, record keys, demo |

---

### Task 1: Reopen with run identity, read-only (spec 1.1)

**Files:**
- Modify: `libs/vs-project/src/vs_project/_state.py:808-811` (`__init__`), `:885-893` (`model_cache_directory`), `:1133-1140` (`set_current_run`), `:1222-1229` (`_contained_local_run_dir`)
- Modify: `src/vibesys/api/_store.py:254-262` (`_LocalRunRecord._effective_objective`)
- Modify: `src/server/controller.py:61-78`
- Modify: `src/server/runtime.py:60-81,160-161`
- Modify: `src/entrypoints/server.py` (imports; `_web_requested` :39; helpers after `_read_only_log_from_argv` :111-116; `main` :300-359)
- Modify: `tests/server/support.py` (imports; new `finished_run`)
- Test: `libs/vs-project/tests/test_state_guards.py`, `tests/server/test_detached_runtime.py`, `tests/entrypoints/test_server.py`
- Modify: `docs/contributing/tui-architecture.md:105-109`

**Interfaces:**
- Consumes: `vibesys.api.open_run_store(project).get_record(run_id) -> RunRecord`; `Project.state.log_directory(run_id) -> Path`; `StateNamespace.read_bytes(relative) -> bytes | None`.
- Produces:
  - `ProjectState` no longer creates the state home in `__init__`; `_contained_local_run_dir`, `model_cache_directory`, and `set_current_run(<id>)` prepare it (0o700) before returning or writing a machine-local path. `log_directory_for` is unchanged (still prepares).
  - `RunController.attach_read_only(self, log_dir: Path, *, record: RunRecord | None = None) -> None` (replaces the unused `run_id=` keyword); raises `ValueError` when an event's `run_id` differs from `record.run_id`.
  - `ServerRuntime.__init__(..., read_only_log: Path | None = None, read_only_record: RunRecord | None = None)`; attribute `self.read_only_record`.
  - `entrypoints.server._project_root_from_argv(argv: list[str]) -> Path` (`--project` resolved, else `Path.cwd()`).
  - `entrypoints.server._reopen_from_argv(argv: list[str]) -> tuple[Path | None, RunRecord | None]`; raises `ConfigurationError` for a missing journal or a journal whose first event names another run.
  - CLI flag `--web-reopen-run RUN_ID`: with `--web-reopen PATH` the journal comes from PATH; without it, from `project.state.log_directory(run_id)`.
  - Test helper `tests.server.support.finished_run(root: Path, run_id: str = "queue-run") -> tuple[Project, str, Path]`: one hypothesis `H-01`, round 1 measured at `42.0 ops_s`, a finished journal under the state home.

- [ ] **Step 1: Add the `finished_run` test helper**

In `tests/server/support.py`, add imports (keep the rest; `./scripts/format.sh` orders the block):

```python
from tests.support.run_execution import run_execution_record

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.hypothesis.state import Hypothesis, HypothesisState
from vibesys.orchestration.single.models import SingleState
from vs_loop_state.api import RoundRecord
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
```

Replace the existing `from vs_project.api import OrchestrationDescriptor` line with the last one, and remove `from vs_project.api import Project` from the `if TYPE_CHECKING:` block. Append:

```python
def finished_run(root: Path, run_id: str = "queue-run") -> tuple[Project, str, Path]:
    """Record a finished single-agent run with one measured round and a closed journal."""
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("queue")
    manifest = project.state.new_run_manifest(
        "queue",
        run_id=run_id,
        branch=f"vibesys/{run_id}",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=agent_descriptor(),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    plan = OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="claim for H-01",
        task="test H-01",
        pass_criteria="",
        reasoning="",
    )
    measured = RoundRecord(
        round_number=1,
        commit="c1",
        perf_metric=42.0,
        perf_unit="ops_s",
        perf_provenance="implementer",
        passed=True,
        judge_verdict="pass",
        hypothesis_id="H-01",
    )
    project.state.portable_namespace(run_id, "single-agent").slot(
        "state.json", SingleState
    ).save(
        SingleState(
            search=HypothesisState(
                hypotheses=[
                    Hypothesis(hypothesis_id="H-01", plan=plan, started_round=1, rounds=[measured])
                ]
            )
        )
    )
    log_dir = project.state.log_directory(run_id)
    writer = build_server_parts(log_dir, record=run_record(project, run_id))
    writer.controller.finish()
    writer.close()
    return project, run_id, log_dir
```

- [ ] **Step 2: Write the failing read-only reopen tests**

In `tests/server/test_detached_runtime.py` add imports (move `Path` out of the `TYPE_CHECKING` block):

```python
import os
import shutil
from pathlib import Path

from tests.server.support import build_server_parts, finished_run, run_record

from server.api.protocol import (
    DesignQuery,
    ExperimentQuery,
    PerformanceQuery,
    SnapshotQuery,
    StopCommand,
    SubscribeRequest,
)
from vs_project.api import Project
```

and tests:

```python
def _tree(root: Path) -> dict[Path, bytes | None]:
    """Every file and directory under *root*, with file contents."""
    return {path: path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def test_reopened_run_serves_its_record_without_writing(tmp_path: Path) -> None:
    project, run_id, recorded_logs = finished_run(tmp_path / "project")
    log_dir = tmp_path / "logs"
    shutil.copytree(recorded_logs, log_dir)
    state_home = Path(os.environ["VIBESYS_STATE_HOME"])
    shutil.rmtree(state_home)
    before = (_tree(project.root), _tree(log_dir))

    reader = build_server_parts()
    reader.controller.attach_read_only(
        log_dir, record=run_record(Project.open(project.root), run_id)
    )
    experiments = reader.api.execute(ExperimentQuery())
    performance = reader.api.execute(PerformanceQuery())
    design = reader.api.execute(DesignQuery())
    reader.close()

    assert experiments.experiments_ready is True
    assert [entry.hypothesis_id for entry in experiments.experiments] == ["H-01"]
    assert [(item.round, item.perf_metric) for item in performance.performance] == [(1, 42.0)]
    assert design.design_ready is True
    assert [item.round for item in design.design] == [1]
    assert not state_home.exists()
    assert (_tree(project.root), _tree(log_dir)) == before


def test_reopen_rejects_a_journal_from_another_run(tmp_path: Path) -> None:
    project, run_id, _logs = finished_run(tmp_path / "a")
    _other, _other_id, other_logs = finished_run(tmp_path / "b", run_id="other-run")

    reader = build_server_parts()
    with pytest.raises(ValueError, match="belongs to run other-run"):
        reader.controller.attach_read_only(other_logs, record=run_record(project, run_id))
    reader.close()
```

In `libs/vs-project/tests/test_state_guards.py`, replace `test_state_home_creation_failure_is_reported` and add a test:

```python
def test_state_home_creation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(blocker / "home"))

    with pytest.raises(ProjectStateError, match=r"Could not create VibeSys state home .*blocker"):
        Project.open(project).state.model_cache_directory("probe")


def test_reading_project_state_leaves_an_absent_state_home_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "absent-home"
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(home))
    store = _store(_project_dir(tmp_path))

    assert store.state.list_runs() == []
    assert store.state.current_run_id() is None
    assert not home.exists()

    store.state.model_cache_directory("probe")
    assert (home / "projects").is_dir()
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/server/test_detached_runtime.py libs/vs-project/tests/test_state_guards.py -q --basetemp=/tmp/vsw -k "without_writing or another_run or state_home"`
Expected: FAIL. `attach_read_only() got an unexpected keyword argument 'record'` (both server tests); `test_reading_project_state_leaves_an_absent_state_home_absent` fails on `assert not home.exists()`; `test_state_home_creation_failure_is_reported` may still pass.

- [ ] **Step 4: Make state-home preparation lazy (root cause for lookups creating directories)**

In `libs/vs-project/src/vs_project/_state.py`:

`__init__`: delete the line `_prepare_state_home(self._state_home)` (keep `self._state_home = _state_home()` and the rest; `_validate_storage_roots` does not need the directories to exist).

`model_cache_directory`: first statement after `self._validate_storage_roots()`:

```python
        _prepare_state_home(self._state_home)
```

`set_current_run`: after `normalized = _validate_run_id(run_id)` and `self.load_run(normalized)`, before `_atomic_write_text(...)`:

```python
        _prepare_state_home(self._state_home)
```

`_contained_local_run_dir`:

```python
    def _contained_local_run_dir(self, run_id: str) -> Path:
        self._validate_storage_roots()
        normalized = _validate_run_id(run_id)
        _prepare_state_home(self._state_home)
        return _contained_without_symlinks(
            self._local_dir,
            self._local_dir / "runs" / normalized,
            kind="local run",
        )
```

These are every path into `self._local_dir` (`local_namespace` and `_round_transaction_path` go through `_contained_local_run_dir`); `log_directory_for` already prepares. Reads (`load_run`, `list_runs`, `current_run_id`, `portable_namespace`) no longer create anything.

- [ ] **Step 5: Read the effective objective without materializing a directory**

In `src/vibesys/api/_store.py`, replace `_effective_objective`:

```python
    def _effective_objective(self) -> str | None:
        try:
            contents = self._project.state.portable_namespace(
                self._run_id, "runtime"
            ).read_bytes("effective-objective.md")
        except (OSError, ProjectStateError):
            return None
        return contents.decode("utf-8") if contents is not None else None
```

(`external_directory()` created `.vibesys/state/runs/<id>/runtime/` on every read.)

- [ ] **Step 6: Attach the record in `attach_read_only`, rejecting other runs' events**

Replace `src/server/controller.py:61-78` with:

```python
    def attach_read_only(self, log_dir: Path, *, record: RunRecord | None = None) -> None:
        """Open an ended journal and its run record without appending any event."""
        with self._condition:
            self._journal.attach(
                log_dir,
                run_id=record.run_id if record is not None else None,
                read_only=True,
            )
            status = RunStatus.STARTING
            for event in self._journal.read_history():
                if record is not None and event.run_id != record.run_id:
                    raise ValueError(  # noqa: TRY003  # lint-waiver: LW-101106 [TRY003]; name both runs when a reopened journal belongs to another run
                        # > A dedicated exception class would be raised once, here, and the
                        # > entrypoint already reports ValueError as a configuration error.
                        f"Journal {log_dir} belongs to run {event.run_id}, not {record.run_id}"
                    )
                if event.type is EventType.RUN_STATUS_CHANGED and isinstance(
                    event.data, RunStatusChangedData
                ):
                    status = event.data.status
                elif event.type is EventType.RUN_FINISHED:
                    status = RunStatus.COMPLETED
                elif event.type in {EventType.RUN_FAILED, EventType.RUN_INTERRUPTED}:
                    status = RunStatus.FAILED
            if not status.has_ended:
                raise ValueError("Read-only serving requires a finished run")  # noqa: TRY003  # lint-waiver: LW-101044 [TRY003]; reject reopening an unfinished run with a clear API error
            self._status = status
            self._attached_run = record
```

(Drops the dead `self._project_run = None`.) Confirm the id is free: `grep -rn "LW-101106" src tests libs scripts` prints only this line.

- [ ] **Step 7: Run the Task 1 server and library tests**

Run: `uv run pytest tests/server/test_detached_runtime.py libs/vs-project/tests -q --basetemp=/tmp/vsw`
Expected: PASS (including the existing `test_finished_journal_reopens_read_only_without_mutating_storage` and every other `vs-project` state test).

- [ ] **Step 8: Write the failing entrypoint tests**

In `tests/entrypoints/test_server.py` add imports and tests:

```python
from tests.server.support import finished_run

from vibesys.api import ConfigurationError, RunRecord
```

```python
def test_web_requested_accepts_reopen_flags_in_both_forms() -> None:
    assert _web_requested(["--web-reopen-run", "queue-run"]) is True
    assert _web_requested(["--web-reopen-run=queue-run"]) is True
    assert _web_requested(["--web-reopen=run-events.jsonl"]) is True
    assert _web_requested(["--local"]) is False


def test_web_reopen_run_attaches_the_recorded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, log_dir = finished_run(tmp_path / "project")
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            del socket_path
            observed.update(options)

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    # test-isolation: replace the dynamic runtime import to observe reopen wiring without serving
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)

    main(["--web-reopen-run", run_id, "--project", str(project.root)])

    record = observed["read_only_record"]
    assert isinstance(record, RunRecord)
    assert record.run_id == run_id
    assert observed["read_only_log"] == log_dir


def _refuse_launch(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("reopen must fail before launching a gateway")


def test_web_reopen_run_rejects_an_unknown_run_before_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _run_id, _log_dir = finished_run(tmp_path / "project")
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="does not exist"):
        main(["--web", "--detach", "--web-reopen-run", "missing-run", "--project", str(project.root)])


def test_web_reopen_run_rejects_an_unsafe_run_id_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _run_id, _log_dir = finished_run(tmp_path / "project")
    # test-isolation: discovery would create lock files named after the run id
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", _refuse_launch)

    with pytest.raises(ConfigurationError, match="Invalid VibeSys run ID"):
        main(["--web", "--detach", "--web-reopen-run", "../escape", "--project", str(project.root)])
    assert list(tmp_path.rglob("*web-gateway*")) == []


def test_web_reopen_run_requires_the_recorded_event_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, log_dir = finished_run(tmp_path / "project")
    (log_dir / "run-events.jsonl").unlink()
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="No event journal to reopen"):
        main(["--web", "--detach", "--web-reopen-run", run_id, "--project", str(project.root)])


def test_web_reopen_run_rejects_another_runs_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _logs = finished_run(tmp_path / "a")
    _other, _other_id, other_logs = finished_run(tmp_path / "b", run_id="other-run")
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="belongs to run other-run"):
        main(
            [
                "--web",
                "--detach",
                "--web-reopen-run",
                run_id,
                "--web-reopen",
                str(other_logs),
                "--project",
                str(project.root),
            ]
        )
```

- [ ] **Step 9: Run them to verify they fail**

Run: `uv run pytest tests/entrypoints/test_server.py -q --basetemp=/tmp/vsw -k "reopen_run or both_forms"`
Expected: FAIL. `both_forms` fails on the `--web-reopen-run` asserts; the `main` tests fail with `SystemExit` (`--control-socket is required`) because `--web-reopen-run` is not yet a web flag.

- [ ] **Step 10: Implement the entrypoint path**

In `src/entrypoints/server.py`, imports:

```python
import json

from vibesys.api import ConfigurationError, open_run_store
from vs_project.api import Project, ProjectError
```

and under `if TYPE_CHECKING:` extend to `from vibesys.api import Config, RunRecord`.

Replace `_web_requested`:

```python
_WEB_FLAGS = frozenset({"--web", "--web-reopen", "--web-reopen-run"})


def _web_requested(argv: list[str]) -> bool:
    return any(argument.partition("=")[0] in _WEB_FLAGS for argument in argv)
```

After `_read_only_log_from_argv`, add:

```python
def _project_root_from_argv(argv: list[str]) -> Path:
    value = cli.option_from_argv(argv, "--project")
    return Path(value).expanduser().resolve() if value else Path.cwd()


def _reopen_from_argv(argv: list[str]) -> tuple[Path | None, RunRecord | None]:
    """Resolve the read-only journal and, with ``--web-reopen-run``, its run record.

    Only the journal's first event is checked here, so a wrong journal fails
    before launch; ``RunController.attach_read_only`` checks every event.
    """
    log_dir = _read_only_log_from_argv(argv)
    record = None
    run_id = cli.option_from_argv(argv, "--web-reopen-run") or None
    if run_id is not None:
        project = Project.open(_project_root_from_argv(argv))
        record = open_run_store(project).get_record(run_id)
        log_dir = log_dir or project.state.log_directory(run_id)
    if log_dir is None:
        return None, None
    events = log_dir / "run-events.jsonl"
    if not events.is_file():
        cli.configuration_error(
            f"No event journal to reopen at {log_dir}",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if record is not None:
        with events.open(encoding="utf-8") as stream:
            journal_run = json.loads(stream.readline() or "{}").get("run_id")
        if journal_run != record.run_id:
            cli.configuration_error(
                f"Journal {log_dir} belongs to run {journal_run}, not {record.run_id}",
                code="invalid_arguments",
                stage="argument_parsing",
            )
    return log_dir, record
```

In `main`, resolve the reopen right after the `--detach requires --web` check, and compute `instance_path` inside the same `try` (replacing the existing `instance_path = ...` line). An invalid run id then fails before any discovery file, lock, or child exists, and a missing `--project` (`ProjectRootNotFoundError`, a `ProjectError`, from `ProjectLayout.open`'s strict resolve) is a configuration error instead of a traceback:

```python
    try:
        read_only_log, read_only_record = _reopen_from_argv(arguments)
        instance_path = _web_instance_from_argv(arguments) if web else None
    except (ValueError, ProjectError) as exc:
        cli.configuration_error(
            str(exc),
            code="invalid_arguments",
            stage="argument_parsing",
        )
```

Remove `read_only_log = _read_only_log_from_argv(arguments)` from the later `try` (keep `web_port`, `web_assets`, `web_origins` there) and pass the record in the web runtime construction:

```python
                instance_path=instance_path,
                detach=detach,
                read_only_log=read_only_log,
                read_only_record=read_only_record,
            )
```

- [ ] **Step 11: Accept the record in `ServerRuntime`**

In `src/server/runtime.py`, extend the `TYPE_CHECKING` import to `from vibesys.api import RunRecord, RunRequest, RunResult, RunSession`, add the keyword after `read_only_log`:

```python
        read_only_log: Path | None = None,
        read_only_record: RunRecord | None = None,
    ) -> None:
```

store it (`self.read_only_record = read_only_record`) next to `self.read_only_log`, and change line 161:

```python
            self.controller.attach_read_only(self.read_only_log, record=self.read_only_record)
```

- [ ] **Step 12: Run the Task 1 suites**

Run: `uv run pytest tests/entrypoints/test_server.py tests/server/test_detached_runtime.py tests/server/test_runtime.py tests/server/test_experiments.py tests/server/test_design.py libs/vs-project/tests tests/vibesys/api -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 13: Document the flag**

In `docs/contributing/tui-architecture.md`, after the paragraph starting "`vibesys --web --web-reopen PATH` serves a completed", append:

```markdown
`--web-reopen-run RUN_ID` adds the run's identity: the server opens the run record from the project
(`--project`, default the working directory) and attaches it read-only, so experiment, performance,
and design queries answer from the recorded run without writing to the project, the log directory,
or the state home. Without `--web-reopen`, the journal is read from that run's log directory. An
unknown or invalid run ID, a missing journal, or a journal recorded by another run is a
configuration error before any gateway starts.
```

- [ ] **Step 14: Commit**

```bash
git add libs/vs-project/src/vs_project/_state.py libs/vs-project/tests/test_state_guards.py src/vibesys/api/_store.py src/server/controller.py src/server/runtime.py src/entrypoints/server.py tests/server/support.py tests/server/test_detached_runtime.py tests/entrypoints/test_server.py docs/contributing/tui-architecture.md
git commit -m "fix(server): reopen a finished run read-only with its run record"
```

---

### Task 2: `project_attached` after the record attaches; clients settle when it never does (spec 1.2)

**Files:**
- Modify: `src/vibesys/run/resources.py:441-448` (remove) and after `:571-587` (`integration.publish_resources(...)`)
- Test: `tests/vibesys/test_context.py`
- Regenerate: `tests/vibesys/golden/snapshots/events/**/*.json`
- Modify: `clients/tui/src/session-controller.ts` (`#requestExperiments` ~:1066-1072, `#refreshExperimentsFor` ~:1532-1542)
- Test: `clients/tui/src/session-controller.test.ts`
- Modify: `clients/web/src/model.ts:31`, `clients/web/src/derive.ts:440-444`, `clients/web/src/App.tsx:221`, `clients/web/src/ui/Rail.tsx:90-92`
- Test: `clients/web/src/derive.test.ts:176-182`

**Interfaces:**
- Consumes: `LocalRunIntegration.add_resource_listener`, `integration.events.subscribe` (synchronous), `vibesys.api._session._run_ready(resources, registry) -> RunReady`; `hasRunEnded(core)` and `failExperiments(state, message)` (already imported in `session-controller.ts`).
- Produces:
  - Ordering contract: `EXPERIMENTS_CHANGED(reason="project_attached")` is emitted after the resource listener returns, so the server's `attached_run` is set when clients see it.
  - TUI: a pending experiments log becomes `{pending: false, error: 'The run ended before its experiments were available.'}` when the server answers `experiments_ready: false` for an ended run.
  - Web: `RailState = 'loading' | 'error' | 'unattached' | 'ended-unattached' | 'ready'`; `railState(experiments, error, ended: boolean)`.

- [ ] **Step 1: Write the failing server-side readiness test**

In `tests/vibesys/test_context.py` add imports:

```python
from tests.server.support import build_server_parts

from server.api.protocol import ExperimentQuery
from vibesys.api._session import _run_ready
from vibesys.events import CoreEvent, ExperimentsChangedData
from vibesys.run.integration import RunResources
```

and, after `test_run_context_announces_canonical_experiment_state`:

```python
def test_project_attached_signal_finds_the_record_attached(tmp_path: Path) -> None:
    """A client that queries on ``project_attached`` gets an attached run.

    Mirrors production: ``RunSession`` projects resources with ``_run_ready`` and
    ``RunIntegrationAdapter.handle_run_ready`` attaches the record, synchronously,
    inside the resource listener.
    """
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    integration = LocalRunIntegration()
    server = build_server_parts()
    answers: list[bool | None] = []

    def attach_record(resources: RunResources) -> None:
        ready = _run_ready(resources, built_in_orchestrations())
        server.integration.attach(ready.log_directory, record=ready.record)

    def query_on_signal(event: CoreEvent) -> None:
        data = event.data
        if isinstance(data, ExperimentsChangedData) and data.reason == "project_attached":
            answers.append(server.api.execute(ExperimentQuery()).experiments_ready)

    integration.add_resource_listener(attach_record)
    unsubscribe = integration.events.subscribe(query_on_signal)
    try:
        with _create_context(project, evaluator=evaluator, integration=integration):
            pass
    finally:
        unsubscribe()
        integration.close()
        server.close()

    assert answers == [True]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/vibesys/test_context.py::test_project_attached_signal_finds_the_record_attached -q --basetemp=/tmp/vsw`
Expected: FAIL with `assert [False] == [True]`.

- [ ] **Step 3: Move the emit**

In `src/vibesys/run/resources.py`, inside `with boot_trace.span("workspace_setup"):` delete (currently 442-448), leaving `integration.attach(log_dir, project=project, run_id=run_id)`:

```python
            integration.events.emit(
                CoreEventType.EXPERIMENTS_CHANGED,
                data=ExperimentsChangedData(reason="project_attached"),
            )
            logger.lprint(
                f"experiments gate open after {(time.perf_counter() - context_start) * 1000:.0f}ms"
            )
```

and insert, one level less indented, directly after the closing `)` of `integration.publish_resources(RunResources(...))`:

```python
        # The resource listener attaches the run record synchronously, so a
        # client that queries on this signal finds experiments ready.
        integration.events.emit(
            CoreEventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason="project_attached"),
        )
        logger.lprint(
            f"experiments gate open after {(time.perf_counter() - context_start) * 1000:.0f}ms"
        )
        project_resources.mark_ready()
```

In `test_context_assembly_logs_stage_timings`'s docstring, replace "(the gate flips when the second ``LocalRunIntegration.attach`` records ``EXPERIMENTS_CHANGED``)" with "(the gate flips when ``EXPERIMENTS_CHANGED`` is recorded after resource publication)".

- [ ] **Step 4: Run the context tests**

Run: `uv run pytest tests/vibesys/test_context.py -q --basetemp=/tmp/vsw`
Expected: PASS (new test, `test_run_context_announces_canonical_experiment_state` still sees exactly one signal, `test_context_assembly_logs_stage_timings`).

- [ ] **Step 5: Check and regenerate the event goldens**

Run: `uv run pytest tests/vibesys/orchestration/single/test_goldens.py tests/vibesys/orchestration/multi/test_goldens.py tests/vibesys/orchestration/issue_queue/test_goldens.py -q --basetemp=/tmp/vsw`
Expected: PASS, or FAIL only in `events` snapshots because the `project_attached` entry moved later.

If they fail:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/vibesys/orchestration/single/test_goldens.py tests/vibesys/orchestration/multi/test_goldens.py tests/vibesys/orchestration/issue_queue/test_goldens.py -q --basetemp=/tmp/vsw
git diff --stat tests/vibesys/golden
git diff tests/vibesys/golden | grep '^[-+] ' | sort | uniq -c
```

Expected: only files under `tests/vibesys/golden/snapshots/events/`, and in each only the `experiments_changed` object with `"reason": "project_attached"` moved (equal added and removed counts for its lines). Anything else is a regression: stop and investigate.

- [ ] **Step 6: Write the failing TUI tests**

In `clients/tui/src/session-controller.test.ts`, inside `describe('session controller', ...)`:

```ts
  it('stops loading experiments when the run ends before its project attaches', async () => {
    const transport = new FakeTransport();
    transport.experimentsReady = false;
    const controller = new SocketSessionController(transport);
    await controller.start();
    expect(controller.state.experimentLog?.pending).toBe(true);

    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_failed'),
          diagnostic: {
            code: 'run_failed',
            summary: 'Setup failed.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
      ],
      through_sequence: 1,
      active_executions: [],
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.error).toBe(
      'The run ended before its experiments were available.',
    );
  });

  it('shows recorded experiments for an ended run without a failure', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {})];
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({type: 'event', event: event(1, 'run_finished')});
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.error).toBeNull();
    expect(controller.state.experimentLog?.entries.map(item => item.hypothesis_id)).toEqual([
      'H-01',
    ]);
  });
```

Run: `(cd clients/tui && bun test src/session-controller.test.ts -t "ended run|ends before")`
Expected: the first test FAILS (`pending` stays `true`); the second passes (regression guard).

- [ ] **Step 7: Settle the TUI experiments log**

In `clients/tui/src/session-controller.ts`, add below the imports:

```ts
/** Shown when a run ends before its project, and so its experiments, ever attached. */
const EXPERIMENTS_NEVER_ATTACHED = 'The run ended before its experiments were available.';
```

In `#requestExperiments`, replace `if (response.experiments_ready === false) return;` with:

```ts
      if (response.experiments_ready === false) {
        if (hasRunEnded(this.#state.core)) {
          this.#setState(failExperiments(this.#state, EXPERIMENTS_NEVER_ATTACHED));
        }
        return;
      }
```

In `#refreshExperimentsFor`, replace `if (!relevant || !this.#experimentRefreshNeeded()) return;` with:

```ts
    if (!relevant) {
      // A run that ends before its project attaches never signals; ask once
      // more so the answer settles the pending log instead of loading forever.
      if (this.#state.experimentLog?.pending === true && hasRunEnded(this.#state.core)) {
        void this.#loadExperiments();
      }
      return;
    }
    if (!this.#experimentRefreshNeeded()) return;
```

Run: `(cd clients/tui && bun test src/session-controller.test.ts)`
Expected: PASS (including `does not refetch the log for events that cannot change it`: its log is not pending).

- [ ] **Step 8: Web rail state (minimal; the Rail is replaced in sub-project 3)**

Update `clients/web/src/derive.test.ts`'s rail test:

```ts
test('rail state: loading, unattached, ended before attaching, ready, and a failed first load', () => {
  const experiments = (ready: boolean) => ({request_id: 'q', ok: true, experiments_ready: ready});
  assert.equal(railState(null, null, false), 'loading');
  assert.equal(railState(experiments(false), null, false), 'unattached');
  assert.equal(
    railState(experiments(false), null, true),
    'ended-unattached',
    'an ended run stops waiting',
  );
  assert.equal(railState(experiments(true), null, true), 'ready', 'an ended run with data is ready');
  assert.equal(railState(experiments(true), null, false), 'ready');
  assert.equal(railState(null, 'Experiments unavailable', false), 'error', 'the error alone, no skeleton');
  assert.equal(railState(experiments(true), 'down', false), 'ready', 'a failed refetch keeps its rows');
});
```

Run: `(cd clients/web && bun test src/derive.test.ts)`
Expected: FAIL (`ended-unattached` expected, `unattached` returned).

`clients/web/src/model.ts:31`:

```ts
export type RailState = 'loading' | 'error' | 'unattached' | 'ended-unattached' | 'ready';
```

`clients/web/src/derive.ts`, replace `railState`:

```ts
/** What the rail shows from the experiments query: not ready is unattached, or never attached once ended. */
export function railState(
  experiments: ProtocolResponse | null,
  error: string | null,
  ended: boolean,
): RailState {
  if (experiments === null) return error === null ? 'loading' : 'error';
  if (experiments.experiments_ready !== false) return 'ready';
  return ended ? 'ended-unattached' : 'unattached';
}
```

`clients/web/src/App.tsx`: add `import {hasRunEnded} from '@vibesys/core-state';` and change the Rail prop to:

```tsx
          state={railState(queries.experiments.response, queries.experiments.error, hasRunEnded(core))}
```

`clients/web/src/ui/Rail.tsx`, after the `unattached` branch:

```tsx
      ) : state === 'unattached' ? (
        <p className="rail-note">Waiting for the project to attach</p>
      ) : state === 'ended-unattached' ? (
        <p className="rail-note">The run ended before the project attached</p>
      ) : model.rows.length === 0 ? (
```

Run: `(cd clients/web && bun test src)`
Expected: PASS.

- [ ] **Step 9: Run the other signal consumers**

Run: `uv run pytest tests/vibesys/api/test_runtime_commit_events.py tests/vibesys/api/test_orchestration_registry.py tests/server/test_experiments.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/vibesys/run/resources.py tests/vibesys/test_context.py tests/vibesys/golden/snapshots/events clients/tui/src/session-controller.ts clients/tui/src/session-controller.test.ts clients/web/src/model.ts clients/web/src/derive.ts clients/web/src/derive.test.ts clients/web/src/App.tsx clients/web/src/ui/Rail.tsx
git commit -m "fix(run): announce project_attached after the record attaches; settle clients when it never does"
```

---

### Task 3: Run-specific discovery records (spec 1.3)

**Files:**
- Modify: `src/server/transport/discovery.py:1,15-58,134-148`
- Modify: `src/server/transport/websocket.py:26,55-77,195-202`
- Modify: `src/server/runtime.py:60-81,176-186`
- Modify: `src/entrypoints/server.py` (`_web_instance_from_argv` :102-108; reuse block in `main` :309-316; web runtime construction)
- Test: `tests/server/test_detached_runtime.py`, `tests/server/test_websocket_transport.py`, `tests/entrypoints/test_server.py`, `tests/entrypoints/test_web.py`
- Modify: `docs/contributing/tui-architecture.md:96-103`

**Interfaces:**
- Consumes: Task 1's `_project_root_from_argv`, `_reopen_from_argv`, `ServerRuntime.read_only_record`, `finished_run`.
- Produces:
  - `server.transport.discovery.WebInstanceMode = Literal["live", "reopen"]`.
  - `WebInstanceRecord` fields `run_id: str | None = None`, `mode: WebInstanceMode = "live"` (after `started_at`).
  - `WebInstanceRecord.from_gateway(*, pid, port, token, project_root, run_id: str | None = None, mode: WebInstanceMode = "live")`.
  - `WebSocketGateway(..., run_id: str | None = None, mode: WebInstanceMode = "live")` (existing `project_root` keyword now supplied).
  - `ServerRuntime.__init__(..., project_root: Path | None = None)` forwarded to the gateway.
  - Default instance path: `<project>/.vibesys/web-gateway.json` for live; `web-gateway-<run-id>.json` for `--web-reopen-run`; `web-gateway-log-<sha256(canonical log dir)[:12]>.json` for `--web-reopen PATH` alone.
  - Launcher reuse only when the discovered record's `(mode, run_id)` equals the request's; otherwise a configuration error.

- [ ] **Step 1: Write the failing tests**

In `tests/server/test_detached_runtime.py` add `from server.transport.discovery import WebInstanceRecord, _read_record` (extend the existing import) and:

```python
def test_instance_record_round_trips_run_identity_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "web-gateway-queue-run.json"
    record = WebInstanceRecord.from_gateway(
        pid=1234,
        port=43_212,
        token="-".join(("capability", "token")),
        project_root=tmp_path,
        run_id="queue-run",
        mode="reopen",
    )
    record.write(path)

    written = json.loads(path.read_text())
    assert (written["version"], written["run_id"], written["mode"]) == (1, "queue-run", "reopen")
    assert _read_record(path) == record

    legacy = {key: value for key, value in written.items() if key not in {"run_id", "mode"}}
    path.write_text(json.dumps(legacy))
    upgraded = _read_record(path)
    assert upgraded is not None
    assert (upgraded.run_id, upgraded.mode) == (None, "live")

    path.write_text(json.dumps({**written, "mode": "paused"}))
    assert _read_record(path) is None


def test_runtime_reopen_publishes_its_own_record_and_serves_data(
    tmp_path: Path, socket_dir: Path
) -> None:
    project, run_id, log_dir = finished_run(tmp_path / "project")
    instance = tmp_path / f"web-gateway-{run_id}.json"
    runtime = ServerRuntime(
        socket_path=socket_dir / "control.sock",
        web=True,
        instance_path=instance,
        read_only_log=log_dir,
        read_only_record=run_record(project, run_id),
        project_root=project.root,
    )
    thread = threading.Thread(target=lambda: runtime.run(lambda: None))
    thread.start()
    try:
        _wait_for(instance)
        written = json.loads(instance.read_text())
        assert (written["mode"], written["run_id"]) == ("reopen", run_id)
        assert written["project_root"] == str(project.root.resolve())
        experiments = runtime.api.execute(ExperimentQuery()).experiments
        assert [entry.hypothesis_id for entry in experiments] == ["H-01"]
    finally:
        runtime.shutdown()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not instance.exists()
```

In `tests/server/test_websocket_transport.py`:

```python
def test_gateway_record_carries_run_identity_and_mode(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    instance_path = tmp_path / ".vibesys" / "web-gateway-queue-run.json"

    with WebSocketGateway(
        parts.api,
        instance_path=instance_path,
        project_root=tmp_path / "project",
        run_id="queue-run",
        mode="reopen",
    ):
        record = WebInstanceRecord.discover(instance_path)
        assert record is not None
        assert (record.run_id, record.mode) == ("queue-run", "reopen")
        assert record.project_root == str((tmp_path / "project").resolve())

    assert not instance_path.exists()
```

In `tests/entrypoints/test_server.py`:

```python
def test_reopen_instance_records_are_run_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    live = _web_instance_from_argv(["--web"])
    by_run = _web_instance_from_argv(["--web-reopen-run", "queue-run"])
    sibling_a = _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-a")])
    sibling_b = _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-b")])

    assert live == project.resolve() / ".vibesys" / "web-gateway.json"
    assert by_run.name == "web-gateway-queue-run.json"
    assert sibling_a.name.startswith("web-gateway-log-")
    assert sibling_a == _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-a")])
    assert len({live, by_run, sibling_a, sibling_b}) == 4


def test_reopen_never_reuses_the_live_project_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    discovered: list[Path] = []
    spawned: list[Path] = []

    def discover(path: Path) -> None:
        discovered.append(path)

    # test-isolation: observe which record the launcher consults instead of probing a real gateway
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", discover)
    # test-isolation: observe the detached launch instead of starting a child process
    monkeypatch.setattr(
        server_entrypoint, "_spawn_detached", lambda _arguments, path: spawned.append(path)
    )

    main(["--web", "--detach", "--web-reopen-run", run_id, "--project", str(project.root)])

    expected = (project.configuration_path() / f"web-gateway-{run_id}.json").resolve()
    assert discovered == [expected]
    assert spawned == [expected]
    assert expected != (project.configuration_path() / "web-gateway.json").resolve()


def test_launcher_refuses_to_reuse_a_gateway_serving_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    live = WebInstanceRecord(
        pid=123,
        port=43_211,
        token="-".join(("capability", "token")),
        url="http://127.0.0.1:43211/?token=capability-token",
        project_root=str(project.root),
        started_at=1.0,
    )
    opened: list[str] = []
    # test-isolation: inject a live gateway at the explicitly shared record path
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", lambda _path: live)
    # test-isolation: capture browser opening so a wrong reuse is observable and headless
    monkeypatch.setattr(
        server_entrypoint.webbrowser, "open", lambda url, **_kwargs: opened.append(url)
    )

    with pytest.raises(ConfigurationError, match="held by a live gateway"):
        main(
            [
                "--web",
                "--web-instance",
                str(tmp_path / "shared.json"),
                "--web-reopen-run",
                run_id,
                "--project",
                str(project.root),
            ]
        )
    assert opened == []


def test_web_runtime_records_the_requested_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            del socket_path
            observed.update(options)

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    # test-isolation: replace the dynamic runtime import to observe gateway metadata wiring
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)

    main(["--web-reopen-run", run_id, "--project", str(project.root)])

    assert observed["project_root"] == project.root.resolve()
```

In `tests/entrypoints/test_web.py` (imports `json`, `os`; `from tests.server.support import build_server_parts`; `from server.transport.websocket import WebSocketGateway`):

```python
def test_status_and_stop_accept_old_and_new_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    instance = tmp_path / "web-gateway-queue-run.json"
    signalled: list[int] = []
    # test-isolation: stop must not signal the test process that hosts the gateway
    monkeypatch.setattr(web.os, "kill", lambda pid, _signal: signalled.append(pid))
    # test-isolation: a fake stop leaves the record; skip the bounded removal wait
    monkeypatch.setattr(web, "_RECORD_WAIT_SECONDS", 0.0)

    with WebSocketGateway(parts.api, instance_path=instance, run_id="queue-run", mode="reopen"):
        current = json.loads(instance.read_text())
        legacy = {key: value for key, value in current.items() if key not in {"run_id", "mode"}}
        for payload in (current, legacy):
            instance.write_text(json.dumps(payload))
            assert _run_status(argparse.Namespace(instance=instance)) == 0
            assert _run_stop(argparse.Namespace(instance=instance)) == 0

    assert signalled == [os.getpid(), os.getpid()]
    assert capsys.readouterr().out.count("Stopped VibeSys web gateway") == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/server/test_detached_runtime.py tests/server/test_websocket_transport.py tests/entrypoints/test_server.py tests/entrypoints/test_web.py -q --basetemp=/tmp/vsw -k "identity or run_specific or never_reuses or publishes_its_own or another_run or project_root or old_and_new"`
Expected: FAIL: unexpected keyword `run_id`/`project_root`; `by_run.name == "web-gateway.json"`; the discovered path is the live one; the mismatched record is reused.

- [ ] **Step 3: Extend the discovery record**

In `src/server/transport/discovery.py` (docstring: "Crash-safe discovery for project-local web gateways."):

```python
from typing import Any, Literal

WebInstanceMode = Literal["live", "reopen"]
_MODES: dict[object, WebInstanceMode] = {"live": "live", "reopen": "reopen"}
```

Fields at the end of `WebInstanceRecord`:

```python
    run_id: str | None = None
    mode: WebInstanceMode = "live"
```

Replace `from_gateway`:

```python
    @classmethod
    def from_gateway(  # noqa: PLR0913  # lint-waiver: LW-101105 [PLR0913]; the factory takes one keyword per persisted discovery fact
        # > A parameter object was rejected: it would duplicate WebInstanceRecord's own
        # > fields, and the record itself cannot be built before the URL is derived.
        cls,
        *,
        pid: int,
        port: int,
        token: str,
        project_root: Path,
        run_id: str | None = None,
        mode: WebInstanceMode = "live",
    ) -> WebInstanceRecord:
        """Build a record only after the gateway has successfully bound."""
        return cls(
            pid=pid,
            port=port,
            token=token,
            url=f"http://127.0.0.1:{port}/?token={token}",
            project_root=str(project_root.resolve()),
            started_at=time.time(),
            run_id=run_id,
            mode=mode,
        )
```

Confirm `grep -rn "LW-101105" src tests libs scripts` prints only this line. In `_read_record`, extend the constructor call:

```python
            run_id=None if raw.get("run_id") is None else _nonempty_string(raw["run_id"]),
            mode=_MODES[raw.get("mode", "live")],
```

An unknown `mode` raises `KeyError`, an unhashable one `TypeError`; both are already caught (record is `None`).

- [ ] **Step 4: Forward identity and project root through the gateway and runtime**

`src/server/transport/websocket.py`: import `WebInstanceMode` with the existing discovery import; add after `allowed_origins`:

```python
        allowed_origins: Sequence[str] = (),
        run_id: str | None = None,
        mode: WebInstanceMode = "live",
    ) -> None:
```

store `self.run_id = run_id` and `self.mode = mode`, and pass both in `_run`:

```python
                self._instance_record = WebInstanceRecord.from_gateway(
                    pid=os.getpid(),
                    port=self._bound_port,
                    token=self.token,
                    project_root=self.project_root,
                    run_id=self.run_id,
                    mode=self.mode,
                )
```

`src/server/runtime.py`: add `project_root: Path | None = None,` after `read_only_record`, store `self.project_root = project_root`, and extend the gateway construction in `run`:

```python
                        WebSocketGateway(
                            self.api,
                            assets_dir=self.web_assets,
                            port=self.web_port,
                            allowed_origins=self.web_origins,
                            subscriptions=subscriptions,
                            instance_path=self.instance_path,
                            project_root=self.project_root,
                            run_id=(
                                self.read_only_record.run_id
                                if self.read_only_record is not None
                                else None
                            ),
                            mode="live" if self.read_only_log is None else "reopen",
                        )
```

In `src/entrypoints/server.py`'s web runtime construction add `project_root=_project_root_from_argv(arguments),` (this fixes live launches too: `web.py:196` starts the server from the checkout root with `--project`).

- [ ] **Step 5: Run-specific default path and a reuse guard**

`src/entrypoints/server.py`: `import hashlib`, then replace `_web_instance_from_argv`:

```python
def _web_instance_from_argv(argv: list[str]) -> Path:
    value = cli._option_from_argv(argv, "--web-instance")  # noqa: SLF001  # lint-waiver: LW-101042 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
    if value is not None:
        return Path(value).expanduser().resolve()
    run_id = cli.option_from_argv(argv, "--web-reopen-run") or None
    log_dir = _read_only_log_from_argv(argv)
    if run_id is None and log_dir is not None:
        run_id = "log-" + hashlib.sha256(str(log_dir).encode()).hexdigest()[:12]
    name = "web-gateway.json" if run_id is None else f"web-gateway-{run_id}.json"
    return (Project.open(_project_root_from_argv(argv)).configuration_path() / name).resolve()
```

(`_read_only_log_from_argv` already returns a resolved path. `main` calls this inside the `try` that catches `(ValueError, ProjectError)` (Task 1 Step 10), after `_reopen_from_argv` validated the run id, so `vibesys --web --project /missing` is a clean configuration error.) Add to `tests/entrypoints/test_server.py`:

```python
def test_web_launch_with_a_missing_project_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Project root does not exist"):
        main(["--web", "--project", str(tmp_path / "missing")])
```

In `main`, replace the reuse block:

```python
        existing = _discover_web_instance(instance_path)
        if existing is not None:
            requested = (
                "live" if read_only_log is None else "reopen",
                read_only_record.run_id if read_only_record is not None else None,
            )
            if (existing.mode, existing.run_id) != requested:
                cli.configuration_error(
                    f"{instance_path} is held by a {existing.mode} gateway for run "
                    f"{existing.run_id}",
                    code="invalid_arguments",
                    stage="argument_parsing",
                )
            print(f"VibeSys web UI: {existing.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101039 [T201]; expose the reused capability URL to the launcher user
            webbrowser.open(existing.url, new=2)
            return
```

- [ ] **Step 6: Run the Task 3 tests plus existing discovery users**

Run: `uv run pytest tests/server/test_detached_runtime.py tests/server/test_websocket_transport.py tests/server/test_runtime.py tests/entrypoints/test_server.py tests/entrypoints/test_web.py -q --basetemp=/tmp/vsw`
Expected: PASS (existing `test_web_instance_record_is_project_local`, `test_second_web_launch_reuses_live_instance`, stale-record and status/stop tests unchanged).

- [ ] **Step 7: Document the record keys**

In `docs/contributing/tui-architecture.md`, in the paragraph starting "The detached gateway publishes `.vibesys/web-gateway.json` by default.", replace "and contains the PID, loopback port, capability token, and project root." with:

```markdown
and contains the PID, loopback port, capability token, the requested project root, `mode` (`live`
or `reopen`), and, for a reopen, the `run_id`. Both keys are optional under `version: 1`; readers
treat a record without them as `live`. A reopen publishes `.vibesys/web-gateway-<run-id>.json`
(or `web-gateway-log-<hash>.json` for a journal given without a run ID), so it never reuses the
live gateway, and the launcher refuses to reuse any record whose mode or run differs from the
request. A live launch with `--project` now publishes its record under that project, not the
working directory.
```

- [ ] **Step 8: Commit**

```bash
git add src/server/transport/discovery.py src/server/transport/websocket.py src/server/runtime.py src/entrypoints/server.py tests/server/test_detached_runtime.py tests/server/test_websocket_transport.py tests/entrypoints/test_server.py tests/entrypoints/test_web.py docs/contributing/tui-architecture.md
git commit -m "fix(server): give reopened runs their own discovery record"
```

---

### Task 4: Demo bundle reopened through the identity path (spec 1.4)

**Files:**
- Create: `clients/web/src/fixtures/demo-project/` (`OBJECTIVE.md`, `.vibesys/state/.gitignore`, `.vibesys/state/project.json`, `.vibesys/state/runs/20260925-140000-8f21c3a0-web-live/run.json`, `.vibesys/state/runs/20260925-140000-8f21c3a0-web-live/single-agent/state.json`), generated once by a throwaway script
- Modify: `src/entrypoints/web.py:27,119-144,162-170`
- Test: `tests/entrypoints/test_web.py`
- Modify: `docs/contributing/web-development.md:38-49`

**Interfaces:**
- Consumes: Task 1's `--web-reopen-run`/`--project` handling and journal identity check.
- Produces: `entrypoints.web._DEMO_PROJECT = Path("clients/web/src/fixtures/demo-project")`, `entrypoints.web._DEMO_RUN_ID = "20260925-140000-8f21c3a0-web-live"`. `_live_command(project=..., replay_log=...)` requires `project` with `replay_log` and emits `--project P --web-reopen LOG --web-reopen-run _DEMO_RUN_ID`. `clients/web/src/replay.ts` is unchanged (browser-only fallback).

- [ ] **Step 1: Generate the recorded demo project**

Write the throwaway generator (not committed) and run it with an isolated state home:

```bash
cat > "$TMPDIR/build_demo_project.py" <<'EOF'
"""One-shot generator for clients/web/src/fixtures/demo-project. Not committed."""

import json
import sys
from pathlib import Path

from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.hypothesis.state import Hypothesis, HypothesisState
from vibesys.orchestration.single.models import SingleState
from vs_loop_state.api import RoundRecord
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)

journal, root = Path(sys.argv[1]), Path(sys.argv[2])
events = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
run_id = events[0]["run_id"]
plans: dict[int, OrchestratorPlan] = {}
rounds: dict[int, RoundRecord] = {}
for event in events:
    data = event["data"] or {}
    label = event["round_label"] or ""
    number = int(label.split("-")[1]) if label.startswith("round-") else None
    result = data.get("result") or {}
    if event["type"] == "agent_execution_finished" and "task" in result and number:
        fields = {k: v for k, v in result.items() if k in OrchestratorPlan.model_fields}
        plans[number] = OrchestratorPlan.model_validate(fields)
    if event["type"] == "round_finished" and number:
        rounds[number] = RoundRecord(
            round_number=number,
            commit=None,
            perf_metric=data["perf_metric"],
            perf_unit=data["perf_unit"],
            perf_provenance="implementer" if data["perf_metric"] is not None else None,
            passed=data["judge_verdict"] == "pass",
            judge_verdict=data["judge_verdict"],
            hypothesis_id=plans[number].hypothesis_id,
        )
hypotheses = [
    Hypothesis(
        hypothesis_id=plans[n].hypothesis_id,
        plan=plans[n],
        started_round=n,
        rounds=[rounds[n]],
    )
    for n in sorted(rounds)
]
root.mkdir(parents=True)
(root / "OBJECTIVE.md").write_text("Maximize decode throughput (tok/s) of the demo LLM server.\n")
project = Project.open(root)
project.state.create_project("llm-serve")
options = AgentOrchestrationOptions(
    interface="inprocess",
    max_rounds=12,
    max_retries_per_round=1,
    judge_every=1,
    official_eval_every=1,
)
manifest = project.state.new_run_manifest(
    "llm-serve",
    run_id=run_id,
    branch=f"vibesys-runs/{run_id}",
    vibesys_version="0.0.0-demo",
    run_environment=RunEnvironmentRecord(name="local"),
    execution=RunExecutionRecord(
        model="demo-model",
        agent_backend="stub",
        compute_backend="cpu",
        requested_profiler="none",
        resolved_profiler="none",
        agent_roles={},
    ),
    orchestration=OrchestrationDescriptor(
        id="single-agent", config_version=1, options=options.model_dump(mode="json")
    ),
    trusted_input_baseline="0" * 40,
)
project.state.create_run(manifest)
project.state.portable_namespace(run_id, "single-agent").slot("state.json", SingleState).save(
    SingleState(search=HypothesisState(hypotheses=hypotheses))
)
print(run_id)
EOF
VIBESYS_STATE_HOME="$TMPDIR/demo-state" uv run python "$TMPDIR/build_demo_project.py" clients/web/src/fixtures/demo-run.jsonl clients/web/src/fixtures/demo-project
find clients/web/src/fixtures/demo-project -type f | sort
```

Expected: prints `20260925-140000-8f21c3a0-web-live`, then exactly:

```
clients/web/src/fixtures/demo-project/.vibesys/state/.gitignore
clients/web/src/fixtures/demo-project/.vibesys/state/project.json
clients/web/src/fixtures/demo-project/.vibesys/state/runs/20260925-140000-8f21c3a0-web-live/run.json
clients/web/src/fixtures/demo-project/.vibesys/state/runs/20260925-140000-8f21c3a0-web-live/single-agent/state.json
clients/web/src/fixtures/demo-project/OBJECTIVE.md
```

Seven hypotheses (H-01..H-07), one round each, matching the journal's `round_finished` events. The bundle is not a git repository, so design rounds carry no file lists (`files` is `None`); experiments and performance are complete.

- [ ] **Step 2: Write the failing tests**

In `tests/entrypoints/test_web.py` add imports (move `Path` out of `TYPE_CHECKING`):

```python
import shutil
from pathlib import Path

from entrypoints.web import _DEMO_PROJECT, _DEMO_RUN_ID, _repository_root
from server.api.protocol import DesignQuery, ExperimentQuery, PerformanceQuery
from vibesys.api import open_run_store
from vs_project.api import Project
```

Bundle test:

```python
def test_demo_bundle_reopens_with_the_recorded_experiments(tmp_path: Path) -> None:
    root = _repository_root()
    project_root = tmp_path / "project"
    shutil.copytree(root / _DEMO_PROJECT, project_root)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    shutil.copy2(root / _DEMO_LOG, log_dir / "run-events.jsonl")
    journal = [json.loads(line) for line in (root / _DEMO_LOG).read_text().splitlines() if line]
    measured = [
        (int(event["round_label"].split("-")[1]), float(event["data"]["perf_metric"]))
        for event in journal
        if event["type"] == "round_finished" and event["data"]["perf_metric"] is not None
    ]

    record = open_run_store(Project.open(project_root)).get_record(_DEMO_RUN_ID)
    reader = build_server_parts()
    reader.controller.attach_read_only(log_dir, record=record)
    experiments = reader.api.execute(ExperimentQuery()).experiments
    performance = reader.api.execute(PerformanceQuery()).performance
    design = reader.api.execute(DesignQuery())
    reader.close()

    assert journal[0]["run_id"] == _DEMO_RUN_ID
    assert [entry.hypothesis_id for entry in experiments] == [f"H-0{n}" for n in range(1, 8)]
    assert [(item.round, item.perf_metric) for item in performance] == measured
    assert design.design_ready is True
    assert [item.round for item in design.design] == list(range(1, 8))
```

Update `test_live_demo_command_uses_the_shared_server_entrypoint`:

```python
def test_live_demo_command_uses_the_shared_server_entrypoint(tmp_path: Path) -> None:
    replay_log = tmp_path / "run-events.jsonl"
    project = tmp_path / "project"
    command = _live_command(
        project=project,
        replay_log=replay_log,
        task=None,
        port=8765,
        instance=tmp_path / "web-gateway.json",
        run_args=(),
        browser_origins=(),
    )

    assert command[-6:] == [
        "--project",
        str(project),
        "--web-reopen",
        str(replay_log),
        "--web-reopen-run",
        _DEMO_RUN_ID,
    ]
    assert "--stub-agent" not in command
    assert command[1:4] == ["-m", "entrypoints.server", "--web"]
```

In `test_run_live_demo_stages_replay_log_for_gateway`, after writing `replay_source`, stage a fake bundle:

```python
    demo_source = tmp_path / _DEMO_PROJECT
    demo_source.mkdir(parents=True)
    (demo_source / "OBJECTIVE.md").write_text("demo\n")
```

and replace `assert captured["project"] is None` with:

```python
    staged_project = cast("Path", captured["project"])
    assert (staged_project / "OBJECTIVE.md").read_text() == "demo\n"
    assert staged_project.parent == replay_log.parent
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/entrypoints/test_web.py -q --basetemp=/tmp/vsw`
Expected: FAIL with `ImportError: cannot import name '_DEMO_PROJECT' from 'entrypoints.web'`.

- [ ] **Step 4: Reopen the demo with identity**

In `src/entrypoints/web.py` below `_DEMO_LOG`:

```python
_DEMO_PROJECT = Path("clients/web/src/fixtures/demo-project")
_DEMO_RUN_ID = "20260925-140000-8f21c3a0-web-live"
```

In `_live_command`, replace the `replay_log` branch:

```python
    if replay_log is not None:
        if project is None:
            raise AssertionError
        command.extend(
            (
                "--project",
                str(project),
                "--web-reopen",
                str(replay_log),
                "--web-reopen-run",
                _DEMO_RUN_ID,
            )
        )
    elif project is not None:
```

In `_run_live`, replace the `if args.demo:` block:

```python
    if args.demo:
        replay_source = (root / _DEMO_LOG).resolve()
        demo_source = (root / _DEMO_PROJECT).resolve()
        if not replay_source.is_file() or not demo_source.is_dir():
            raise SystemExit(f"vibesys web: demo bundle is incomplete under {root}")  # noqa: TRY003  # lint-waiver: LW-101104 [TRY003]; report an incomplete source checkout before gateway startup
        demo_dir = Path(tempfile.mkdtemp(prefix="vibesys-web-demo-"))
        replay_log = demo_dir / "run-events.jsonl"
        shutil.copy2(replay_source, replay_log)
        project = demo_dir / "project"
        shutil.copytree(demo_source, project)
        default_instance = demo_dir / "web-gateway.json"
```

- [ ] **Step 5: Run the web tests**

Run: `uv run pytest tests/entrypoints/test_web.py tests/entrypoints/test_web_script.py -q --basetemp=/tmp/vsw`
Expected: PASS.

- [ ] **Step 6: Smoke the real demo (manual, needs local port binding)**

```bash
uv run python -m entrypoints.web live --demo --no-build --port 8799
```

Expected: `VibeSys web UI ready: http://127.0.0.1:8799/?token=...` and an `Instance record:` path; `cat <instance record>` shows `"mode": "reopen"` and `"run_id": "20260925-140000-8f21c3a0-web-live"`. Stop with `uv run python -m entrypoints.web stop --instance <instance record>`.

- [ ] **Step 7: Document the bundle**

In `docs/contributing/web-development.md`, replace "The demo serves the repository's deterministic recorded run through the real\nHTTP and WebSocket gateway:" with:

```markdown
The demo reopens the repository's recorded run through the real HTTP and
WebSocket gateway. The bundle is the event journal
`clients/web/src/fixtures/demo-run.jsonl` plus the recorded project
`clients/web/src/fixtures/demo-project`, so experiment and performance queries
return the recorded data:
```

Run: `uv run python scripts/check_doc_links.py`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add clients/web/src/fixtures/demo-project src/entrypoints/web.py tests/entrypoints/test_web.py docs/contributing/web-development.md
git commit -m "feat(web): reopen the demo as a recorded project with its run record"
```

---

### Task 5: Repository gates

**Files:** none new; fix what the gates report in files touched by Tasks 1 to 4.

- [ ] **Step 1: Python format, lint, types, boundaries, waivers, isolation, length, docs**

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
./scripts/check_types.sh
uv run tach check
uv run python scripts/check_tach_graph.py --check
uv run python scripts/check_lint_waivers.py
uv run python scripts/check_test_isolation.py
uv run python scripts/check_file_length.py
uv run python scripts/check_doc_links.py
uv run vibesys --headless --help
```

Expected: every command exits 0. Likely findings: `TC` import placement in the touched test modules (runtime vs `TYPE_CHECKING` imports); `RUF100` if a waiver is unneeded (delete it).

- [ ] **Step 2: TypeScript gates (as `.repoctl/checks.toml` `tui_quality` and `tui_tests` run them in CI)**

```bash
cd clients
pnpm check:ts
pnpm check:ts-architecture
pnpm check:knip
pnpm --dir backend-client generate:protocol
git diff --exit-code -- backend-client/src/generated/protocol.schema.json backend-client/src/generated/protocol.generated.ts
pnpm check:clients
pnpm --filter @vibesys/tui test
pnpm --filter @vibesys/web test
pnpm test:ts-architecture
pnpm test:conformance
pnpm build:clients
cd ..
```

Expected: all exit 0; the generated protocol diff is empty (no protocol model changed). Skipped on purpose: `pnpm --filter @vibesys/web test:e2e` and `clients/scripts/check_package_smoke.sh`, because e2e runs `pnpm dev` against the browser-only replay (`clients/web/playwright.config.ts:5`), not the demo bundle, and this plan changes neither the replay nor packaging. If biome reports cognitive complexity on `Rail`, extract the note branch into a small `RailNote` lookup inside `Rail.tsx` rather than adding a suppression.

- [ ] **Step 3: Touched Python suites**

```bash
uv run pytest tests/server tests/entrypoints tests/vibesys/test_context.py tests/vibesys/api libs/vs-project/tests tests/vibesys/orchestration/single/test_goldens.py tests/vibesys/orchestration/multi/test_goldens.py tests/vibesys/orchestration/issue_queue/test_goldens.py -q --basetemp=/tmp/vsw
```

Expected: PASS. Run in the foreground; split by directory if slow.

- [ ] **Step 4: Commit any gate fixes**

```bash
git add -u
git commit -m "chore: satisfy lint, type, and client gates for backend correctness"
```

Skip if there is nothing to commit.

---

## Self-Review

- **Spec coverage:** 1.1 (identity through reopen, `get_record`, read-only attach with no writes anywhere, non-empty experiments, performance, design): Task 1. 1.2 (emit after record attach; a client querying on the signal gets data, checked through `ExperimentQuery`; clients stop waiting when it never comes): Task 2. 1.3 (run-specific record, no cross reuse, optional `run_id`/`mode` under `version: 1`, `web stop`/`status` on old and new records, project root recorded): Task 3. 1.4 (recorded project plus journal, reopened through 1.1, `replay.ts` kept as fallback): Task 4. Testing bullet "1": Tasks 1 to 3.
- **Placeholders:** none; every code step carries code, every run step a command and expected result.
- **Type consistency:** `attach_read_only(log_dir, *, record=None)`, `read_only_record`, `project_root`, `_reopen_from_argv -> tuple[Path | None, RunRecord | None]`, `finished_run(root, run_id="queue-run")`, `WebInstanceMode`, `run_id`/`mode` keywords, `railState(experiments, error, ended)` are identical across tasks.
- **Review Focus:** each of the five lines has named tests in its owning task.
