"""Fresh-only preflight preserves legacy files and validates new-envelope identity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

import pytest

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    OLDEST_SUPPORTED_ENVELOPE_VERSION,
    EventCursor,
    FlowState,
    HostFence,
    HostId,
    RoleId,
    RunEnvelope,
    RunId,
    RunResultProposal,
    initial_state,
)
from vs_core.api import RunStatus as CoreRunStatus
from vs_core.api.testing import FakeView
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import (
    CoreResumeError,
    CoreRuntime,
    CoreRuntimeBindings,
    RuntimeRecord,
    reject_ended_resume,
    reject_legacy_resume,
    resolve_core_resume,
)
from vs_runtime.api.core_shell_fixtures import CounterConfig, ShellTraceTransitions, counter
from vs_runtime.api.infrastructure import recorded_ending

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import FlowDriver


def driver() -> FlowDriver[CounterConfig]:
    return counter.driver(CounterConfig())


def project_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("shell-test", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "shell-test",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def record(run_id: str) -> RuntimeRecord[FlowState]:
    strategy = driver()
    core = initial_state()
    core = core.model_copy(
        update={
            "run": core.run.model_copy(
                update={"run_id": RunId(root=run_id), "declaration": strategy.declaration}
            )
        }
    )
    envelope = RunEnvelope[FlowState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="writer"), epoch=1),
        strategy_id=strategy.declaration.strategy_id,
        state_schema=strategy.declaration.state_schema,
        core=core,
        strategy=strategy.init(CounterConfig(), core.run.facts, FakeView().current())[0],
        event_cursor=EventCursor(sequence=0),
    )
    return RuntimeRecord[FlowState].fresh(envelope)


@pytest.mark.parametrize("version", [1, 6, 9])
def test_legacy_state_rejected_without_changes(tmp_path: Path, version: int) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    source = ('{"schema_version":' + str(version) + ',"history":["preserve"]}').encode()
    namespace.write_bytes("state.json", source)
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver(), run_id=run_id)
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
        resolve_core_resume(project, driver())
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
    resolved = resolve_core_resume(project, driver())
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
        resolve_core_resume(project, driver())
    assert failure.value.diagnostic.code == "core_resume_invalid"
    assert store.acquire("next", now=1, duration=1) is not None


@pytest.mark.parametrize(
    ("version", "origin"),
    [
        (OLDEST_SUPPORTED_ENVELOPE_VERSION - 3, "older"),
        (OLDEST_SUPPORTED_ENVELOPE_VERSION - 1, "older"),
        (ENVELOPE_SCHEMA_VERSION + 1, "newer"),
    ],
    ids=["old", "last-unsupported", "new"],
)
def test_an_envelope_of_an_unsupported_version_names_the_vibesys_that_wrote_it(
    tmp_path: Path, version: int, origin: str
) -> None:
    """The version is checked before the shape, which an envelope of another version breaks."""
    project, run_id = project_run(tmp_path)
    current = f'"envelope":{{"schema_version":{ENVELOPE_SCHEMA_VERSION},'
    source = record(run_id).model_dump_json()
    assert current in source
    # A field this version does not have, as an envelope of another shape would carry.
    source = source.replace(current, f'"envelope":{{"schema_version":{version},"moved":1,')
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None, StoredEnvelope(revision=0, schema_version=1, payload=source.encode()), fence, now=0
    )
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver())
    message = failure.value.diagnostic.message
    assert f"envelope version {version} is not supported" in message
    assert (
        f"written by an {origin} VibeSys" in message or f"written by a {origin} VibeSys" in message
    )


@pytest.mark.parametrize(
    "version", range(OLDEST_SUPPORTED_ENVELOPE_VERSION, ENVELOPE_SCHEMA_VERSION)
)
def test_a_run_saved_at_an_older_supported_version_resumes_at_the_current_one(
    tmp_path: Path, version: int
) -> None:
    """Regression: a version bump with no migration refused every run saved before it."""
    project, run_id = project_run(tmp_path)
    saved = record(run_id)
    current = f'"envelope":{{"schema_version":{ENVELOPE_SCHEMA_VERSION},'
    source = saved.model_dump_json()
    assert current in source
    source = source.replace(current, f'"envelope":{{"schema_version":{version},')
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None, StoredEnvelope(revision=0, schema_version=1, payload=source.encode()), fence, now=0
    )
    resolved = resolve_core_resume(project, driver())
    assert resolved.run_id == run_id
    assert resolved.record == saved
    assert resolved.record.envelope.schema_version == ENVELOPE_SCHEMA_VERSION


def test_resume_factory_enforces_legacy_preflight_before_start(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    namespace.write_bytes("state.json", b'{"schema_version":9}')
    before = project.state.portable_run_export(run_id)
    with pytest.raises(CoreResumeError, match="legacy dynamic resume unsupported"):
        CoreRuntime[FlowState].resume(project, driver(), config=CounterConfig())
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
    shell = CoreRuntime[FlowState].resume(
        project,
        driver(),
        config=CounterConfig(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions()),
    )
    shell.start("reader", now_at=1, lease_duration=10)
    assert shell.record.envelope.fence.epoch > candidate.envelope.fence.epoch
    assert shell.record.envelope.core.records.recovery.epoch == shell.record.envelope.fence.epoch
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
        resolve_core_resume(project, driver(), run_id=run_id)
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
        resolve_core_resume(project, driver(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.path.endswith("events.jsonl")
    assert failure.value.diagnostic.source_schema == "4"


def test_unreadable_legacy_state_path_is_a_resume_diagnostic(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    run_directory = next(path for path in tmp_path.rglob(run_id) if path.is_dir())
    (run_directory / "dynamic" / "state.json").mkdir(parents=True)
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.path == namespace.agent_visible_path("state.json")


@pytest.mark.parametrize(
    "source",
    [b"not json", b"\xff\xfe", b"[" * 100_000 + b"]" * 100_000, b'{"schema_version":' * 5_000],
    ids=["not-json", "invalid-utf8", "deep-array", "deep-object"],
)
def test_any_legacy_state_bytes_are_an_unparseable_diagnostic(
    tmp_path: Path, source: bytes
) -> None:
    project, run_id = project_run(tmp_path)
    project.state.portable_namespace(run_id, "dynamic").write_bytes("state.json", source)
    with pytest.raises(CoreResumeError) as failure:
        reject_legacy_resume(project, run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.source_schema == "unparseable"


def test_legacy_state_file_rejects_even_beside_a_valid_new_envelope(tmp_path: Path) -> None:
    # Pinned policy: the dynamic/state.json name is reserved for legacy runs, so a
    # strategy must never write it in the dynamic namespace.
    project, run_id = project_run(tmp_path)
    commit_record(project, run_id)
    namespace = project.state.portable_namespace(run_id, "dynamic")
    namespace.write_bytes("state.json", b"not json")
    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver(), run_id=run_id)
    assert failure.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert failure.value.diagnostic.source_schema == "unparseable"


class _Run(NamedTuple):
    project: Project
    run_id: str
    directory: Path


def _legacy_state(run: _Run) -> None:
    run.project.state.portable_namespace(run.run_id, "dynamic").write_bytes("state.json", b"{}")


def _legacy_journal(run: _Run) -> None:
    run.project.state.portable_namespace(run.run_id, "agent").write_bytes(
        "invocations.jsonl", b"{}\n"
    )


def _legacy_top_level(run: _Run) -> None:
    (run.directory / "events.jsonl").write_bytes(b"{}\n")


def _legacy_unreadable(run: _Run) -> None:
    (run.directory / "dynamic" / "state.json").mkdir(parents=True)


@pytest.mark.parametrize(
    "mark", [_legacy_state, _legacy_journal, _legacy_top_level, _legacy_unreadable]
)
def test_early_rejection_agrees_with_resume_resolution_on_every_legacy_marker(
    tmp_path: Path, mark: Callable[[_Run], None]
) -> None:
    project, run_id = project_run(tmp_path)
    directory = next(path for path in tmp_path.rglob(run_id) if path.is_dir())
    mark(_Run(project, run_id, directory))
    before = project.state.portable_run_export(run_id)

    with pytest.raises(CoreResumeError) as early:
        reject_legacy_resume(project, run_id)
    with pytest.raises(CoreResumeError) as resolved:
        resolve_core_resume(project, driver(), run_id=run_id)

    assert early.value.diagnostic == resolved.value.diagnostic
    assert early.value.diagnostic.code == "dynamic_legacy_resume_unsupported"
    assert project.state.portable_run_export(run_id) == before


def test_early_rejection_leaves_core_runs_and_unknown_runs_to_the_resume(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    reject_legacy_resume(project, run_id)  # never committed: not legacy
    commit_record(project, run_id)
    reject_legacy_resume(project, run_id)  # a committed core record: not legacy
    reject_legacy_resume(project, "no-such-run")  # the resume reports a missing run


@pytest.mark.parametrize("damage", ["truncated", "newer_store_version", "unknown_key"])
def test_a_damaged_store_document_is_a_resume_diagnostic(tmp_path: Path, damage: str) -> None:
    """Mechanism: the store document itself was decoded outside every typed error path.

    A damaged envelope payload is a ``core_resume_invalid`` diagnostic; damage one layer
    out, in the store document that holds the envelope, raised pydantic's raw
    ``ValidationError`` (with excerpts of the file) through the resume instead.
    """
    project, run_id = project_run(tmp_path)
    commit_record(project, run_id)
    document = project.state.state_store_namespace(run_id).external_directory() / "store.json"
    source = document.read_text(encoding="utf-8")
    if damage == "truncated":
        source = source[: len(source) // 2]
    elif damage == "newer_store_version":
        source = source.replace('"version":2', '"version":3', 1)
    else:
        source = source[:-1] + ',"written_by_a_newer_release":1}'
    document.write_text(source, encoding="utf-8")

    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver(), run_id=run_id)
    assert failure.value.diagnostic.code == "core_resume_invalid"


def _commit(project: Project, run_id: str, candidate: RuntimeRecord[FlowState]) -> None:
    store = project.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(revision=0, schema_version=1, payload=candidate.model_dump_json().encode()),
        fence,
        now=0,
    )


def _ended(candidate: RuntimeRecord[FlowState]) -> RuntimeRecord[FlowState]:
    """``candidate`` after its run ended: a stop, a deadline or a result made it terminal."""
    envelope = candidate.envelope
    core = envelope.core
    run = core.run.model_copy(
        update={
            "status": CoreRunStatus.TERMINAL,
            "result": RunResultProposal(outcome="cancelled", reason="stopped: by request"),
        }
    )
    ended = envelope.model_copy(update={"core": core.model_copy(update={"run": run})})
    return RuntimeRecord[FlowState].fresh(ended)


def test_resuming_an_ended_run_is_refused_naming_the_run(tmp_path: Path) -> None:
    """Regression: resuming a stopped run reported it stopped again, with no error."""
    project, run_id = project_run(tmp_path)
    _commit(project, run_id, _ended(record(run_id)))
    before = project.state.portable_run_export(run_id)

    with pytest.raises(CoreResumeError) as failure:
        resolve_core_resume(project, driver(), run_id=run_id)

    diagnostic = failure.value.diagnostic
    assert diagnostic.code == "run_ended"
    assert diagnostic.message == (
        f"cannot resume run {run_id!r}: it is already terminal (cancelled: stopped: by request); "
        "start a new run"
    )
    assert project.state.portable_run_export(run_id) == before


def test_the_cheap_resume_check_refuses_an_ended_run_with_the_same_words(tmp_path: Path) -> None:
    """Request validation refuses before anything boots, as the full resolution would."""
    project, run_id = project_run(tmp_path)
    _commit(project, run_id, _ended(record(run_id)))

    with pytest.raises(CoreResumeError) as cheap:
        reject_ended_resume(project, run_id)
    with pytest.raises(CoreResumeError) as full:
        resolve_core_resume(project, driver(), run_id=run_id)

    assert cheap.value.diagnostic.code == "run_ended"
    assert cheap.value.diagnostic.message == full.value.diagnostic.message


def test_the_cheap_resume_check_leaves_an_open_or_missing_run_to_the_resume(
    tmp_path: Path,
) -> None:
    project, run_id = project_run(tmp_path)
    reject_ended_resume(project, run_id)  # nothing committed yet
    reject_ended_resume(project, "no-such-run")
    _commit(project, run_id, record(run_id))
    reject_ended_resume(project, run_id)


def test_an_open_run_still_resumes(tmp_path: Path) -> None:
    project, run_id = project_run(tmp_path)
    _commit(project, run_id, record(run_id))

    assert resolve_core_resume(project, driver(), run_id=run_id).run_id == run_id


@pytest.mark.parametrize("recorded", [(), ("old-role",), ("judge", "worker")])
def test_a_run_resumes_under_code_that_changed_its_roles(
    tmp_path: Path, recorded: tuple[str, ...]
) -> None:
    """Regression: a code change that added or renamed a role made every run unresumable.

    The run keeps the roles it recorded; every other part of the declaration must match.
    """
    project, run_id = project_run(tmp_path)
    candidate = record(run_id)
    run = candidate.envelope.core.run
    declaration = run.declaration.model_copy(
        update={"roles": tuple(RoleId(root=role) for role in recorded)}
    )
    core = candidate.envelope.core.model_copy(
        update={"run": run.model_copy(update={"declaration": declaration})}
    )
    _commit(
        project,
        run_id,
        RuntimeRecord[FlowState].fresh(candidate.envelope.model_copy(update={"core": core})),
    )

    resolved = resolve_core_resume(project, driver())
    shell = CoreRuntime[FlowState].resume(
        project,
        driver(),
        config=CounterConfig(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions()),
    )
    shell.start("reader", now_at=1, lease_duration=10)

    assert resolved.record.envelope.core.run.declaration.roles == declaration.roles
    assert shell.record.envelope.core.run.declaration.roles == declaration.roles


def test_the_run_s_end_is_read_from_its_core_record(tmp_path: Path) -> None:
    """``vibesys control`` judges a run ended by core's record alone, in the record's shape."""
    project, run_id = project_run(tmp_path)
    assert recorded_ending(project, run_id) is None  # no record yet: the run may still start

    _commit(project, run_id, record(run_id))
    assert recorded_ending(project, run_id) is None

    (tmp_path / "ended").mkdir()
    ended = project_run(tmp_path / "ended")
    _commit(ended[0], ended[1], _ended(record(ended[1])))
    assert recorded_ending(*ended) == "the run ended: stopped: by request"
