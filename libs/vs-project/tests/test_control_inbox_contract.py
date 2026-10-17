"""Contract of the ``ControlInbox`` interface, run against every implementation.

Each case applies one generated sequence of operations (post, settle, reject, close, planting
an entry the inbox cannot read, and reopening the inbox as a new reader would) to every
implementation and to a plain model, and checks that each implementation lists exactly what
the model holds, in posting order whatever the ids, and reports each request's status as the
model does. ``FileControlInbox`` also survives being reopened, as a run reads its inbox again
after a restart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    FileControlInbox,
    InboxClosedError,
    InboxEntry,
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RejectedEntry,
    RequestState,
    RequestStatus,
    RunEnvironmentRecord,
    ended_unapplied,
)
from vs_project.api.testing import FakeControlInbox, run_execution_record

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import ControlInbox

_IDS = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,8}", fullmatch=True)
_PAYLOADS = st.binary(max_size=40)

_ROOT = os.geteuid() == 0
_UNREADABLE = ("directory", "symlink") if _ROOT else ("directory", "symlink", "mode-000")
"""Entries a file inbox cannot read; root reads a mode-000 file, so it is left out then."""

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("post"), _IDS, _PAYLOADS),
        st.tuples(st.just("post"), st.just("same"), st.sampled_from([b"one", b"two"])),
        st.tuples(st.just("settle"), _IDS),
        st.tuples(st.just("reject"), _IDS, st.text(max_size=20)),
        st.tuples(st.just("unreadable"), _IDS, st.sampled_from(_UNREADABLE)),
        st.tuples(st.just("reopen")),
        st.tuples(st.just("close"), st.sampled_from(["completed", "stopped"])),
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
    posted: dict[str, bytes | None] = field(default_factory=dict)
    """Each posted id's payload; None for an entry the inbox cannot read."""
    order: list[str] = field(default_factory=list)
    """The ids posted through the inbox, in the order of their first post."""
    settled: set[str] = field(default_factory=set)
    rejected: dict[str, str] = field(default_factory=dict)
    closed: str | None = None

    def pending(self) -> tuple[tuple[str, bytes, bool], ...]:
        """What is neither settled nor rejected: posted ones in order, then the rest by id."""
        unplaced = sorted(key for key in self.posted if key not in self.order)
        return tuple(
            (key, self.posted[key] or b"", self.posted[key] is None)
            for key in (*self.order, *unplaced)
            if key not in self.settled and key not in self.rejected
        )

    def status(self, request_id: str) -> RequestStatus | None:
        if request_id not in self.posted:
            return None
        if request_id in self.settled:
            return RequestStatus(RequestState.APPLIED)
        if request_id in self.rejected:
            return RequestStatus(RequestState.REJECTED, self.rejected[request_id])
        if self.closed is not None:
            return RequestStatus(RequestState.REJECTED, ended_unapplied(self.closed))
        return RequestStatus(RequestState.PENDING)

    def rejected_entries(self) -> tuple[RejectedEntry, ...]:
        return tuple(
            RejectedEntry(key, self.posted.get(key) or b"", reason)
            for key, reason in sorted(self.rejected.items())
        )

    def refuses(self, request_id: str, payload: bytes) -> bool:
        """Whether posting ``payload`` under ``request_id`` must raise."""
        return request_id in self.posted and self.posted[request_id] != payload

    def apply(self, operation: tuple[object, ...]) -> None:
        match operation:
            case ("post", str(request_id), bytes(payload)) if self.closed is None:
                self.posted.setdefault(request_id, payload)
                if request_id not in self.order:
                    self.order.append(request_id)
            case ("unreadable", str(request_id), str()):
                self.posted.setdefault(request_id, None)
            case ("settle", str(request_id)):
                self.settled.add(request_id)
            case ("reject", str(request_id), str(reason)):
                self.rejected.setdefault(request_id, reason)
            case ("close", str(reason)):
                self.closed = self.closed or reason


@dataclass(frozen=True)
class _Opened:
    """An implementation under test: reopen it, and plant an entry it cannot read."""

    reopen: Callable[[], ControlInbox]
    plant_unreadable: Callable[[str, str], None]


def _file_inbox(tmp_path: Path) -> _Opened:
    project, run_id = _project_with_run(tmp_path)

    def plant(request_id: str, kind: str) -> None:
        directory = project.state.local_namespace(run_id, "control").external_directory("inbox")
        path = directory / f"{request_id}.json"
        if kind == "directory":
            path.mkdir()
        elif kind == "symlink":
            path.symlink_to(tmp_path)
        else:
            path.write_bytes(b"{}")
            path.chmod(0)

    return _Opened(lambda: Project.open(project.root).state.control_inbox(run_id), plant)


def _fake_inbox(_: Path) -> _Opened:
    inbox = FakeControlInbox()
    return _Opened(lambda: inbox, inbox.hold_unreadable)


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(operations=_OPERATIONS)
def test_an_inbox_lists_what_was_posted_in_posting_order_and_reports_each_status(
    tmp_path_factory: pytest.TempPathFactory,
    opener: Callable[[Path], _Opened],
    operations: list[tuple[object, ...]],
) -> None:
    """An id holds one request for good, in the place of its first post, whatever the ids.

    An unreadable entry is listed until it is set aside; a closed inbox refuses every post
    before writing it, and a request never marked then reads as rejected.
    """
    opened = opener(tmp_path_factory.mktemp("inbox"))
    inbox = opened.reopen()
    model = _Model()
    for operation in operations:
        match operation:
            case ("reopen",):
                inbox = opened.reopen()
            case ("unreadable", str(request_id), str(kind)) if request_id not in model.posted:
                opened.plant_unreadable(request_id, kind)
            case _:
                _apply(inbox, model, operation)
        model.apply(operation)
    listed = tuple(
        (entry.request_id, entry.payload, entry.unreadable is not None) for entry in inbox.pending()
    )
    assert listed == model.pending()
    assert inbox.rejected() == model.rejected_entries()
    named = {str(operation[1]) for operation in operations if operation[0] in _NAMING}
    for request_id in sorted(named):
        assert inbox.status(request_id) == model.status(request_id)


_NAMING = frozenset({"post", "settle", "reject", "unreadable"})
"""The operations that name one request."""


def _apply(inbox: ControlInbox, model: _Model, operation: tuple[object, ...]) -> None:
    """Apply one operation that goes through the inbox, as the model says it answers."""
    match operation:
        case ("post", str(request_id), bytes(payload)) if model.closed is not None:
            with pytest.raises(InboxClosedError, match="was not posted"):
                inbox.post(request_id, payload)
        case ("post", str(request_id), bytes(payload)) if model.refuses(request_id, payload):
            with pytest.raises(ProjectStateError):
                inbox.post(request_id, payload)
        case ("post", str(request_id), bytes(payload)):
            inbox.post(request_id, payload)
        case ("settle", str(request_id)):
            inbox.settle(request_id)
        case ("reject", str(request_id), str(reason)):
            inbox.reject(request_id, reason)
        case ("close", str(reason)):
            inbox.close(reason)


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
def test_a_named_request_applies_in_its_posting_place(
    tmp_path: Path, opener: Callable[[Path], _Opened]
) -> None:
    """``--control-id hold pause`` then a default-id ``resume`` apply as posted.

    The default id sorts before ``hold`` (it starts with a digit), so ordering by id would
    resume a run before pausing it.
    """
    inbox = opener(tmp_path).reopen()
    inbox.post("hold", b"pause")
    inbox.post("1760000000000-k3", b"resume")
    inbox.post("hold", b"pause")  # posted again after a lost answer: keeps its first place

    assert [entry.payload for entry in inbox.pending()] == [b"pause", b"resume"]


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
def test_an_ended_run_refuses_a_post_before_writing_it(
    tmp_path: Path, opener: Callable[[Path], _Opened]
) -> None:
    """Once the run closed its inbox, nothing posted is written, and what applied says so."""
    inbox = opener(tmp_path).reopen()
    inbox.post("applied", b"{}")
    inbox.settle("applied")
    inbox.post("unread", b"{}")
    inbox.close("completed")

    with pytest.raises(InboxClosedError, match="was not posted"):
        inbox.post("late", b"{}")
    assert inbox.status("late") is None
    assert inbox.status("applied") == RequestStatus(RequestState.APPLIED)
    assert inbox.status("unread") == RequestStatus(
        RequestState.REJECTED, ended_unapplied("completed")
    )


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
@pytest.mark.parametrize("request_id", ["", "../escape", "a/b", ".hidden", "x" * 129, "a b"])
def test_an_id_that_is_not_one_file_name_is_refused(
    tmp_path: Path, opener: Callable[[Path], _Opened], request_id: str
) -> None:
    inbox = opener(tmp_path).reopen()
    with pytest.raises(ProjectStateError, match="control request id"):
        inbox.post(request_id, b"{}")


def test_a_file_inbox_ignores_files_that_are_not_requests(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    inbox = project.state.control_inbox(run_id)
    inbox.post("r1", b"{}")
    namespace = project.state.local_namespace(run_id, "control")
    namespace.write_bytes("inbox/notes.txt", b"not a request")

    assert inbox.pending() == (InboxEntry("r1", b"{}"),)
    assert isinstance(inbox, FileControlInbox)


def test_creating_a_state_file_never_replaces_one(tmp_path: Path) -> None:
    """Exclusive creation is what keeps two racing posts of one id from both succeeding."""
    project, run_id = _project_with_run(tmp_path)
    namespace = project.state.local_namespace(run_id, "control")

    assert namespace.create_bytes("inbox/r1.json", b"first")
    assert not namespace.create_bytes("inbox/r1.json", b"second")
    assert namespace.read_bytes("inbox/r1.json") == b"first"
