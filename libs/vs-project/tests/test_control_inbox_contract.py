"""Contract of the ``ControlInbox`` interface, run against every implementation.

Each case applies one generated sequence of operations (post, settle, reject, and reopening
the inbox as a new reader would) to every implementation and to a plain model, and checks
that each implementation lists exactly what the model holds. ``FileControlInbox`` also
survives being reopened, as a run reads its inbox again after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    FileControlInbox,
    InboxEntry,
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RejectedEntry,
    RunEnvironmentRecord,
)
from vs_project.api.testing import FakeControlInbox, run_execution_record

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import ControlInbox

_IDS = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,8}", fullmatch=True)
_PAYLOADS = st.binary(max_size=40)

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("post"), _IDS, _PAYLOADS),
        st.tuples(st.just("post"), st.just("same"), st.sampled_from([b"one", b"two"])),
        st.tuples(st.just("settle"), _IDS),
        st.tuples(st.just("reject"), _IDS, st.text(max_size=20)),
        st.tuples(st.just("reopen")),
    ),
    max_size=20,
)


def _project_with_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("inbox", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "inbox",
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


@dataclass
class _Model:
    pending: dict[str, bytes] = field(default_factory=dict)
    rejected: dict[str, tuple[bytes, str]] = field(default_factory=dict)


def _apply(model: _Model, operation: tuple[object, ...]) -> bool:
    """Apply ``operation`` to the model; False when the inbox must refuse it."""
    match operation:
        case ("post", str(request_id), bytes(payload)):
            if model.pending.get(request_id, payload) != payload:
                return False
            model.pending[request_id] = payload
        case ("settle", str(request_id)):
            model.pending.pop(request_id, None)
        case ("reject", str(request_id), str(reason)) if request_id in model.pending:
            model.rejected[request_id] = (model.pending.pop(request_id), reason)
    return True


def _file_inbox(tmp_path: Path) -> Callable[[], ControlInbox]:
    project, run_id = _project_with_run(tmp_path)
    return lambda: Project.open(project.root).state.control_inbox(run_id)


def _fake_inbox(_: Path) -> Callable[[], ControlInbox]:
    inbox = FakeControlInbox()
    return lambda: inbox


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(operations=_OPERATIONS)
def test_an_inbox_lists_what_was_posted_and_neither_settled_nor_rejected(
    tmp_path_factory: pytest.TempPathFactory,
    opener: Callable[[Path], Callable[[], ControlInbox]],
    operations: list[tuple[object, ...]],
) -> None:
    reopen = opener(tmp_path_factory.mktemp("inbox"))
    inbox = reopen()
    model = _Model()
    for operation in operations:
        if operation == ("reopen",):
            inbox = reopen()
            continue
        if not _apply(model, operation):
            _, request_id, payload = operation
            assert isinstance(request_id, str)
            assert isinstance(payload, bytes)
            with pytest.raises(ProjectStateError, match="pending with a different request"):
                inbox.post(request_id, payload)
            continue
        match operation:
            case ("post", str(request_id), bytes(payload)):
                inbox.post(request_id, payload)
            case ("settle", str(request_id)):
                inbox.settle(request_id)
            case ("reject", str(request_id), str(reason)):
                inbox.reject(request_id, reason)
    assert inbox.pending() == tuple(
        InboxEntry(key, model.pending[key]) for key in sorted(model.pending)
    )
    assert inbox.rejected() == tuple(
        RejectedEntry(key, *model.rejected[key]) for key in sorted(model.rejected)
    )


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
@pytest.mark.parametrize("request_id", ["", "../escape", "a/b", ".hidden", "x" * 129, "a b"])
def test_an_id_that_is_not_one_file_name_is_refused(
    tmp_path: Path, opener: Callable[[Path], Callable[[], ControlInbox]], request_id: str
) -> None:
    inbox = opener(tmp_path)()
    with pytest.raises(ProjectStateError, match="control request id"):
        inbox.post(request_id, b"{}")


def test_a_file_inbox_ignores_files_that_are_not_requests(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    inbox = project.state.control_inbox(run_id)
    inbox.post("r1", b"{}")
    namespace = project.state.portable_namespace(run_id, "control")
    namespace.write_bytes("inbox/notes.txt", b"not a request")

    assert inbox.pending() == (InboxEntry("r1", b"{}"),)
    assert isinstance(inbox, FileControlInbox)


@pytest.mark.parametrize("bad", ["symlink", "directory"])
def test_a_file_inbox_lists_an_unreadable_entry_with_its_reason(tmp_path: Path, bad: str) -> None:
    """One bad entry never hides the others or fails the run; rejecting it records why."""
    project, run_id = _project_with_run(tmp_path)
    inbox = project.state.control_inbox(run_id)
    inbox.post("good", b"{}")
    inbox.post("target", b"{}")
    pending = next(tmp_path.rglob("good.json")).parent
    if bad == "symlink":
        (pending / "bad.json").symlink_to(pending / "target.json")
    else:
        (pending / "bad.json").mkdir()

    listed = {entry.request_id: entry for entry in inbox.pending()}
    assert listed["good"] == InboxEntry("good", b"{}")
    assert listed["bad"].payload == b""
    assert listed["bad"].unreadable

    inbox.reject("bad", "unreadable")
    assert inbox.rejected() == (RejectedEntry("bad", b"", "unreadable"),)
    assert {entry.request_id for entry in inbox.pending()} >= {"good", "target"}
