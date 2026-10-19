"""Resuming a run whose stored result is terminal is refused before anything boots, with why.

A run that ended (an operator's Ctrl-C cancels it; a stop, a deadline or a result ends it) is
terminal for good: core never applies another request to it. Resuming one once booted the whole
run (container, environment, agents), found it ended at once and exited saying only that it
"stopped early". The refusal now comes where the resume reference is resolved, before any host
probe or resource, and names the stored outcome and reason, so the CLI, headless and the server
all give it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.cli import build_run_request, parse_cli_invocation
from entrypoints.headless import main as headless_main
from entrypoints.production import production_runs
from entrypoints.run import run_headless
from entrypoints.server import main as server_main
from vibesys.api import ConfigurationError
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    FlowState,
    HostFence,
    HostId,
    RunEnvelope,
    RunId,
    RunResultProposal,
    initial_state,
)
from vs_core.api import RunStatus as CoreRunStatus
from vs_core.api.testing import FakeView
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import RuntimeRecord
from vs_runtime.api.core_shell_fixtures import CounterConfig, counter
from vs_sim.api.testing import FakeSignalSource

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import CoreState

_RUN_ID = "20261018-170000-22222222-dynamic"
_NOW = datetime(2026, 10, 18, 17, tzinfo=UTC)
_OUTCOMES = ("success", "failure", "blocked", "cancelled")
# The reason the live run (S174) stored when Ctrl-C cancelled it.
_REASON = "stopped: no trusted result: the input did not pass its measurement"


def _project(root: Path) -> Path:
    root.mkdir()
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Make the queue faster.\n",
    )
    project = Project.open(root)
    project.state.create_project(root.name, now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        root.name,
        run_id=_RUN_ID,
        branch=f"vibesys-runs/{_RUN_ID}",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic",
            config_version=1,
            options=DynamicLaunch(interface="service", max_rounds=2, judge_every=2).model_dump(
                mode="json", exclude={"profiling"}
            ),
        ),
        trusted_input_baseline="0" * 40,
        now=_NOW,
    )
    project.state.create_run(manifest, make_current=True)
    return root


def _commit_core(root: Path, *, status: CoreRunStatus, result: RunResultProposal | None) -> None:
    """Commit a core record for the run whose run state has ``status`` and ``result``."""
    strategy = counter.driver(CounterConfig())
    core: CoreState = initial_state()
    run = core.run.model_copy(
        update={
            "run_id": RunId(root=_RUN_ID),
            "declaration": strategy.declaration,
            "status": status,
            "result": result,
        }
    )
    core = core.model_copy(update={"run": run})
    envelope = RunEnvelope[FlowState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="writer"), epoch=1),
        strategy_id=strategy.declaration.strategy_id,
        state_schema=strategy.declaration.state_schema,
        core=core,
        strategy=strategy.init(CounterConfig(), core.run.facts, FakeView().current())[0],
        event_cursor=EventCursor(sequence=0),
    )
    record = RuntimeRecord[FlowState].fresh(envelope)
    store = Project.open(root).state_store(_RUN_ID)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(revision=0, schema_version=1, payload=record.model_dump_json().encode()),
        fence,
        now=0,
    )


@pytest.mark.regression
@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_resuming_a_terminal_run_is_refused_naming_its_outcome_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """S174: a run cancelled by Ctrl-C booted fully on resume, then said only "stopped early"."""
    root = _project(tmp_path / "project")
    result = RunResultProposal(outcome=outcome, reason=_REASON)  # ty: ignore[invalid-argument-type]
    _commit_core(root, status=CoreRunStatus.TERMINAL, result=result)
    before = Project.open(root).state.portable_run_export(_RUN_ID)
    monkeypatch.chdir(root)

    with pytest.raises(ConfigurationError) as raised:
        build_run_request(parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID]))

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == "run_ended"
    assert diagnostic.stage == "resume_resolution"
    assert repr(_RUN_ID) in diagnostic.message
    assert f"already terminal ({outcome}: {_REASON})" in diagnostic.message
    assert diagnostic.message.endswith("start a new run")
    # The refusal reads the record only: the run is left as it was.
    assert Project.open(root).state.portable_run_export(_RUN_ID) == before


def _ended_run(root: Path) -> Path:
    _commit_core(
        _project(root),
        status=CoreRunStatus.TERMINAL,
        result=RunResultProposal(outcome="cancelled", reason=_REASON),
    )
    return root


def test_the_headless_entrypoint_refuses_before_booting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_ended_run(tmp_path / "project"))

    with pytest.raises(SystemExit) as raised:
        headless_main(["--outer-loop", "dynamic", "--resume", _RUN_ID])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert f"already terminal (cancelled: {_REASON})" in error
    assert "Traceback" not in error


def test_the_server_entrypoint_refuses_before_booting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_ended_run(tmp_path / "project"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    with pytest.raises(SystemExit) as raised:
        server_main(
            [
                "--control-socket",
                str(tmp_path / "runtime" / "vibesys" / "control.sock"),
                "--outer-loop",
                "dynamic",
                "--resume",
                _RUN_ID,
            ]
        )

    assert raised.value.code == 2
    assert f"already terminal (cancelled: {_REASON})" in capsys.readouterr().err


def test_the_session_refuses_a_run_that_ended_after_its_request_was_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless and the server start runs through the session, which checks again first."""
    root = _project(tmp_path / "project")
    monkeypatch.chdir(root)
    request = build_run_request(
        parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID])
    )
    _commit_core(
        root,
        status=CoreRunStatus.TERMINAL,
        result=RunResultProposal(outcome="cancelled", reason=_REASON),
    )

    with pytest.raises(ConfigurationError) as raised:
        run_headless(request, production_runs(), FakeSignalSource())

    assert raised.value.diagnostic.code == "run_ended"
    assert f"already terminal (cancelled: {_REASON})" in raised.value.diagnostic.message


@pytest.mark.parametrize("status", [s for s in CoreRunStatus if s is not CoreRunStatus.TERMINAL])
def test_a_run_that_has_not_ended_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: CoreRunStatus
) -> None:
    root = _project(tmp_path / "project")
    _commit_core(root, status=status, result=None)
    monkeypatch.chdir(root)

    request = build_run_request(
        parse_cli_invocation(["--outer-loop", "dynamic", "--resume", _RUN_ID])
    )

    assert request.resume is not None
    assert request.resume.run_id == _RUN_ID
