"""Resume admission accepts every previous epoch ending without modifying the run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.cli import build_reader_request, build_run_request, parse_cli_invocation
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
from vs_project.api.testing import run_execution_record, seed_stored_record
from vs_runtime.api.core import RuntimeRecord
from vs_runtime.api.testing import CounterConfig, counter

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

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
    seed_stored_record(
        Project.open(root),
        _RUN_ID,
        StoredEnvelope(revision=0, schema_version=1, payload=record.model_dump_json().encode()),
    )


@pytest.mark.regression
@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_resuming_every_terminal_outcome_is_admitted_read_only(
    tmp_path: Path, outcome: Literal["success", "failure", "blocked", "cancelled"]
) -> None:
    root = _project(tmp_path / "project")
    _commit_core(
        root,
        status=CoreRunStatus.TERMINAL,
        result=RunResultProposal(outcome=outcome, reason=_REASON),
    )
    before = Project.open(root).state.portable_run_export(_RUN_ID)
    request = build_run_request(
        parse_cli_invocation(
            [
                "--input",
                str(root),
                "--outer-loop",
                "dynamic",
                "--resume",
                _RUN_ID,
            ]
        )
    )
    assert request.resume is not None
    assert request.resume.run_id == _RUN_ID
    assert request.project_root == root
    assert Project.open(root).state.portable_run_export(_RUN_ID) == before


@pytest.mark.parametrize("status", [s for s in CoreRunStatus if s is not CoreRunStatus.TERMINAL])
def test_a_run_that_has_not_ended_is_not_refused(tmp_path: Path, status: CoreRunStatus) -> None:
    root = _project(tmp_path / "project")
    _commit_core(root, status=status, result=None)

    request = build_run_request(
        parse_cli_invocation(["--input", str(root), "--outer-loop", "dynamic", "--resume", _RUN_ID])
    )

    assert request.resume is not None
    assert request.resume.run_id == _RUN_ID


@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_chat_reader_restores_terminal_run_without_resuming(
    tmp_path: Path, outcome: Literal["success", "failure", "blocked", "cancelled"]
) -> None:
    root = _project(tmp_path / "read")
    _commit_core(
        root,
        status=CoreRunStatus.TERMINAL,
        result=RunResultProposal(outcome=outcome, reason=_REASON),
    )
    request = build_reader_request(root, _RUN_ID)
    assert request.resume is not None
    assert request.resume.run_id == _RUN_ID
    assert request.project_root == root
