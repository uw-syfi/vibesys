"""Fresh-only preflight preserves legacy files and validates new-envelope identity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support.run_execution import run_execution_record
from tests.support.runtime_core_shell import CounterState, CounterStrategy, ShellTraceTransitions

from vs_core.api import EventCursor, HostFence, HostId, RunEnvelope, RunId, initial_state
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_runtime.api.core import (
    CoreResumeError,
    CoreRuntime,
    CoreRuntimeBindings,
    RuntimeRecord,
    resolve_core_resume,
)

if TYPE_CHECKING:
    from pathlib import Path


def project_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("shell-test", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "shell-test",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def record(run_id: str) -> RuntimeRecord[CounterState]:
    strategy = CounterStrategy()
    core = initial_state()
    core = core.model_copy(
        update={"run": core.run.model_copy(update={"run_id": RunId(root=run_id)})}
    )
    envelope = RunEnvelope[CounterState](
        schema_version=3,
        fence=HostFence(host_id=HostId(root="writer"), epoch=1),
        strategy_id=strategy.declaration.strategy_id,
        state_schema=strategy.declaration.state_schema,
        core=core,
        strategy=strategy.state,
        event_cursor=EventCursor(sequence=0),
    )
    return RuntimeRecord[CounterState].fresh(envelope)


@pytest.mark.parametrize("version", [1, 6, 9])
def test_legacy_state_rejected_without_changes(tmp_path: Path, version: int) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    source = ('{"schema_version":' + str(version) + ',"history":["preserve"]}').encode()
    namespace.write_bytes("state.json", source)
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy(), run_id=run_id)
    diagnostic = failure.value.diagnostic
    assert diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert diagnostic.stage == "resume_resolution"
    assert diagnostic.path == namespace.agent_visible_path("state.json")
    assert diagnostic.source_schema == str(version)
    assert project.state.portable_run_export(run_id) == before
    assert namespace.read_bytes("state.json") == source


def test_journal_alone_does_not_authorize_resume(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "agent")
    namespace.write_bytes("invocations.jsonl", b'{"request_id":"legacy"}\n')
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy())
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.path.endswith("agent/invocations.jsonl")
    assert project.state.portable_run_export(run_id) == before


def test_valid_new_envelope_preflight_is_read_only(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    candidate = record(run_id)
    store.commit(
        None,
        StoredEnvelope(revision=0, schema_version=1, payload=candidate.model_dump_json().encode()),
        fence,
        now=0,
    )
    before = project.state.portable_run_export(run_id)
    resolved = resolve_core_resume(project, CounterStrategy())
    assert resolved.run_id == run_id
    assert resolved.record == candidate
    assert resolved.store.load() == store.load()
    assert project.state.portable_run_export(run_id) == before


@pytest.mark.parametrize("mutation", ["schema", "run_id", "missing_outbox", "unknown_key"])
def test_invalid_new_record_rejects_before_acquiring_a_lease(tmp_path: Path, mutation: str) -> None:
    project, run_id = project_run(tmp_path)
    candidate = record("another-run" if mutation == "run_id" else run_id)
    source = candidate.model_dump_json()
    if mutation == "missing_outbox":
        source = source.replace('"pending_publications":[],', "")
    elif mutation == "unknown_key":
        source = source[:-1] + ',"unknown":1}'
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(
            revision=0, schema_version=2 if mutation == "schema" else 1, payload=source.encode()
        ),
        fence,
        now=0,
    )
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy())
    assert failure.value.diagnostic.code == "core_resume_invalid"
    assert store.acquire("next", now=1, duration=1) is not None


def test_resume_factory_enforces_legacy_preflight_before_start(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    namespace.write_bytes("state.json", b'{"schema_version":9}')
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError, match="legacy dynamic resume unsupported"):
        CoreRuntime[CounterState].resume(project, CounterStrategy())
    assert project.state.portable_run_export(run_id) == before


def test_resume_factory_acquires_new_epoch_and_commits_recovery(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    candidate = record(run_id)
    store.commit(
        None,
        StoredEnvelope(revision=0, schema_version=1, payload=candidate.model_dump_json().encode()),
        fence,
        now=0,
    )
    shell = CoreRuntime[CounterState].resume(
        project,
        CounterStrategy(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions()),
    )
    shell.start("reader", now_at=1, lease_duration=10)
    assert shell.record.envelope.fence.epoch > candidate.envelope.fence.epoch
    assert shell.record.envelope.core.intents.recovery.epoch == shell.record.envelope.fence.epoch
    assert shell.storage_revision == 1


def commit_record(project: Project, run_id: str) -> None:
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(
            revision=0, schema_version=1, payload=record(run_id).model_dump_json().encode()
        ),
        fence,
        now=0,
    )


def test_run_that_never_committed_a_record_is_not_labelled_legacy(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy(), run_id=run_id)
    diagnostic = failure.value.diagnostic
    assert diagnostic.code == "core_resume_invalid"
    assert diagnostic.source_schema == "absent"
    assert diagnostic.path.endswith("core-store/store.json")
    assert project.state.portable_run_export(run_id) == before


def test_top_level_journal_file_is_a_legacy_diagnostic_not_a_project_error(
    tmp_path: Path,
) -> None:
    project, run_id = project_run(tmp_path)
    run_directory = next(path for path in tmp_path.rglob(run_id) if path.is_dir())
    (run_directory / "events.jsonl").write_bytes(b'{"schema_version":4}\n')
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.path.endswith("events.jsonl")
    assert failure.value.diagnostic.source_schema == "4"


def test_unreadable_legacy_state_path_is_a_resume_diagnostic(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    run_directory = next(path for path in tmp_path.rglob(run_id) if path.is_dir())
    (run_directory / "dynamic" / "state.json").mkdir(parents=True)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.path == namespace.agent_visible_path("state.json")


def test_legacy_state_file_rejects_even_beside_a_valid_new_envelope(tmp_path: Path) -> None:
    # Pinned policy: the dynamic/state.json name is reserved for legacy runs, so a
    # strategy must never write it in the dynamic namespace.
    project, run_id = project_run(tmp_path)
    commit_record(project, run_id)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    namespace.write_bytes("state.json", b"not json")
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, CounterStrategy(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.source_schema == "unparseable"
