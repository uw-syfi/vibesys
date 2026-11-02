"""Contract of the ``ControlInbox`` interface, run against every implementation.

Each case applies one generated sequence of operations (post, settle, reject, the run's end,
a post the run ends during, planting an entry the inbox cannot read, and reopening the inbox
as a new reader would) to every implementation and to a plain model, and checks that each
implementation lists exactly what the model holds, in posting order whatever the ids, and
reports each request's status as the model does. The run's end is what the poster reads from
the run's record (``ended``); the inbox keeps no copy of it. ``FileControlInbox`` also survives being reopened, as a run reads its inbox again
after a restart.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    INBOX_FORMAT,
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


def _live() -> str | None:
    """A poster's read of a run that has not ended."""
    return None


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
        st.tuples(st.just("end"), st.sampled_from(["completed", "stopped"])),
        st.tuples(st.just("post-as-it-ends"), _IDS, _PAYLOADS),
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
    ended: str | None = None

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
        if self.ended is not None:
            return RequestStatus(RequestState.REJECTED, ended_unapplied(self.ended))
        return RequestStatus(RequestState.PENDING)

    def rejected_entries(self) -> tuple[RejectedEntry, ...]:
        return tuple(
            RejectedEntry(key, self.posted.get(key) or b"", reason)
            for key, reason in sorted(self.rejected.items())
        )

    def refuses(self, request_id: str, payload: bytes) -> bool:
        """Whether posting ``payload`` under ``request_id`` must raise."""
        return request_id in self.posted and self.posted[request_id] != payload

    def refusal(self, request_id: str) -> str:
        """The message of the refusal of a post under ``request_id``.

        An entry the inbox cannot read cannot be compared, so the post fails with whatever
        says why it cannot be read; a readable one is named as a different request.
        """
        return "different request" if self.posted.get(request_id) is not None else ""

    def apply(self, operation: tuple[object, ...]) -> None:
        match operation:
            case ("post", str(request_id), bytes(payload)) if self.ended is None and not (
                self.refuses(request_id, payload)
            ):
                self._place(request_id, payload)
            case ("post-as-it-ends", str(request_id), bytes(payload)) if self.ended is None:
                if not self.refuses(request_id, payload):
                    self._place(request_id, payload)
                self.ended = "completed"
            case ("unreadable", str(request_id), str()):
                self.posted.setdefault(request_id, None)
            case ("settle", str(request_id)):
                self.settled.add(request_id)
            case ("reject", str(request_id), str(reason)):
                self.rejected.setdefault(request_id, reason)
            case ("end", str(reason)):
                self.ended = self.ended or reason

    def _place(self, request_id: str, payload: bytes) -> None:
        self.posted.setdefault(request_id, payload)
        if request_id not in self.order:
            self.order.append(request_id)


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
@example(operations=[("post", "z", b"1"), ("post", "a", b"2"), ("post", "m", b"3")])
@example(operations=[("end", "completed"), ("post", "late", b"{}")])
@example(
    operations=[
        ("post", "stop-1", b"{}"),
        ("settle", "stop-1"),
        ("end", "completed"),
        ("post", "stop-1", b"{}"),
    ]
)
@example(
    operations=[
        ("post", "r1", b"{}"),
        ("reject", "r1", "no"),
        ("end", "completed"),
        ("post", "r1", b"{}"),
    ]
)
@example(operations=[("post-as-it-ends", "racing", b"{}")])
@example(operations=[("post", "a", b"1"), ("settle", "b"), ("post-as-it-ends", "b", b"2")])
@example(operations=[("post", "a", b"1"), ("end", "completed"), ("post-as-it-ends", "a", b"2")])
@example(operations=[("unreadable", "a", "directory"), ("post", "a", b"1")])
@example(operations=[("unreadable", "a", "symlink"), ("post-as-it-ends", "a", b"1")])
def test_an_inbox_lists_what_was_posted_in_posting_order_and_reports_each_status(
    tmp_path_factory: pytest.TempPathFactory,
    opener: Callable[[Path], _Opened],
    operations: list[tuple[object, ...]],
) -> None:
    """An id holds one request for good, in the place of its first post, whatever the ids.

    An unreadable entry is listed until it is set aside. Once the run has ended, a post of
    a request it applied succeeds, any other post is refused before writing, and a request
    never marked reads as rejected; a post the run ends during is written and refused.
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
        assert inbox.status(request_id, ended=model.ended) == model.status(request_id)


_NAMING = frozenset({"post", "post-as-it-ends", "settle", "reject", "unreadable"})
"""The operations that name one request."""


def _apply(inbox: ControlInbox, model: _Model, operation: tuple[object, ...]) -> None:
    """Apply one operation that goes through the inbox, as the model says it answers."""
    ended = model.ended
    match operation:
        case ("post", str(request_id), bytes(payload)) if model.refuses(request_id, payload):
            with pytest.raises(ProjectStateError, match=model.refusal(request_id)):
                inbox.post(request_id, payload, ended=lambda: ended)
        case ("post", str(request_id), bytes(payload)) if ended is not None:
            _post_to_ended(inbox, model, request_id, payload)
        case ("post", str(request_id), bytes(payload)):
            inbox.post(request_id, payload, ended=lambda: None)
        case ("post-as-it-ends", str(request_id), bytes(payload)) if model.refuses(
            request_id, payload
        ):
            reads = iter([None, "completed"])
            with pytest.raises(ProjectStateError, match=model.refusal(request_id)):
                inbox.post(request_id, payload, ended=lambda: ended or next(reads))
        case ("post-as-it-ends", str(request_id), bytes(payload)) if ended is None:
            reads = iter([None, "completed"])
            # A marker left on the id before its first post applies it once posted.
            if request_id in model.settled:
                inbox.post(request_id, payload, ended=lambda: next(reads))
            else:
                with pytest.raises(InboxClosedError, match="written as the run ended"):
                    inbox.post(request_id, payload, ended=lambda: next(reads))
        case ("post-as-it-ends", str(request_id), bytes(payload)):
            _post_to_ended(inbox, model, request_id, payload)
        case ("settle", str(request_id)):
            inbox.settle(request_id)
        case ("reject", str(request_id), str(reason)):
            assert inbox.reject(request_id, reason) is (request_id not in model.rejected)


def _post_to_ended(inbox: ControlInbox, model: _Model, request_id: str, payload: bytes) -> None:
    """A post after the run ended: what applied stays posted; nothing else is written."""
    status = model.status(request_id)
    if status == RequestStatus(RequestState.APPLIED):
        inbox.post(request_id, payload, ended=lambda: model.ended)
        return
    expected = "was not posted" if status is None else "was rejected"
    with pytest.raises(InboxClosedError, match=expected):
        inbox.post(request_id, payload, ended=lambda: model.ended)


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
def test_a_named_request_applies_in_its_posting_place(
    tmp_path: Path, opener: Callable[[Path], _Opened]
) -> None:
    """``--control-id hold pause`` then a default-id ``resume`` apply as posted.

    The default id sorts before ``hold`` (it starts with a digit), so ordering by id would
    resume a run before pausing it.
    """
    inbox = opener(tmp_path).reopen()
    inbox.post("hold", b"pause", ended=_live)
    inbox.post("1760000000000-k3", b"resume", ended=_live)
    inbox.post("hold", b"pause", ended=_live)  # again after a lost answer: keeps its place

    assert [entry.payload for entry in inbox.pending()] == [b"pause", b"resume"]


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
def test_an_ended_run_refuses_a_post_before_writing_it(
    tmp_path: Path, opener: Callable[[Path], _Opened]
) -> None:
    """Once the run's record says it ended, nothing new is written; a retry of what applied
    succeeds, and a request it never applied reads as rejected, with no marker of the end."""
    inbox = opener(tmp_path).reopen()
    inbox.post("applied", b"{}", ended=_live)
    inbox.settle("applied")
    inbox.post("unread", b"{}", ended=_live)

    def ended() -> str:
        return "completed"

    with pytest.raises(InboxClosedError, match="was not posted"):
        inbox.post("late", b"{}", ended=ended)
    inbox.post("applied", b"{}", ended=ended)
    with pytest.raises(InboxClosedError, match="was rejected: the run ended before applying"):
        inbox.post("unread", b"{}", ended=ended)
    assert inbox.status("late", ended="completed") is None
    assert inbox.status("applied", ended="completed") == RequestStatus(RequestState.APPLIED)
    assert inbox.status("unread", ended="completed") == RequestStatus(
        RequestState.REJECTED, ended_unapplied("completed")
    )
    assert inbox.status("unread", ended=None) == RequestStatus(RequestState.PENDING)


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
@pytest.mark.parametrize("request_id", ["", "../escape", "a/b", ".hidden", "x" * 129, "a b"])
def test_an_id_that_is_not_one_file_name_is_refused(
    tmp_path: Path, opener: Callable[[Path], _Opened], request_id: str
) -> None:
    inbox = opener(tmp_path).reopen()
    with pytest.raises(ProjectStateError, match="control request id"):
        inbox.post(request_id, b"{}", ended=_live)


def test_a_file_inbox_ignores_files_that_are_not_requests(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    inbox = project.state.control_inbox(run_id)
    inbox.post("r1", b"{}", ended=_live)
    namespace = project.state.local_namespace(run_id, "control")
    namespace.write_bytes("inbox/notes.txt", b"not a request")

    assert inbox.pending() == (InboxEntry("r1", b"{}"),)
    assert isinstance(inbox, FileControlInbox)


def test_a_file_inbox_warns_once_about_each_file_its_order_does_not_use(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project, run_id = _project_with_run(tmp_path)
    inbox = project.state.control_inbox(run_id)
    inbox.post("r1", b"{}", ended=_live)
    namespace = project.state.local_namespace(run_id, "control")
    namespace.write_bytes("order/stray.txt", b"r1")
    namespace.write_bytes("order/000000000099", b"../escape")

    with caplog.at_level(logging.WARNING):
        inbox.pending()
        inbox.pending()

    assert sorted(r.getMessage() for r in caplog.records) == [
        "the control inbox ignores order/000000000099: it names no valid request id",
        "the control inbox ignores order/stray.txt: it is not a place in the posting order",
    ]
    assert inbox.pending() == (InboxEntry("r1", b"{}"),)


@pytest.mark.parametrize("opener", [_file_inbox, _fake_inbox], ids=["file", "fake"])
def test_an_inbox_says_which_format_its_reader_declared(
    tmp_path: Path, opener: Callable[[Path], _Opened]
) -> None:
    opened = opener(tmp_path)
    assert opened.reopen().served_format() is None
    opened.reopen().serve()
    opened.reopen().serve()
    assert opened.reopen().served_format() == INBOX_FORMAT


def test_creating_a_state_file_never_replaces_one(tmp_path: Path) -> None:
    """Exclusive creation is what keeps two racing posts of one id from both succeeding."""
    project, run_id = _project_with_run(tmp_path)
    namespace = project.state.local_namespace(run_id, "control")

    assert namespace.create_bytes("inbox/r1.json", b"first")
    assert not namespace.create_bytes("inbox/r1.json", b"second")
    assert namespace.read_bytes("inbox/r1.json") == b"first"
