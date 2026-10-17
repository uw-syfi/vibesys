"""The operator's control inbox: requests written from outside a run, read by the run.

An operator (or an orchestrating agent, over ssh) controls a run that has no socket by
writing one file per request into the run's state directory. The run reads the inbox at
each control poll, applies each request once, and only then marks it settled, so a request
written while the run is down, or one the run read but had not yet committed when it
crashed, is applied after the run resumes. The request's own id names its file, and the file
is never removed: posting the same request again is one request, and an id, once posted,
never names another request.

Requests apply in the order they were posted, whatever their ids: each post claims the next
free number of a sequence, which only an exclusive file creation hands out, so two posters
on any processes never share one and a post that returned is ordered before any post that
starts after it.

The inbox stores opaque bytes; what a request says is the run's business
(``vs_runtime``'s ``ControlRequest``). A request the run cannot apply is marked rejected with
the reason, never silently dropped. When the run ends it closes its inbox: a post is then
refused, and a request it never applied reads as rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from vs_project._state import StateNamespace

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_POSTED = PurePosixPath("inbox")
_SETTLED = PurePosixPath("settled")
_REJECTED = PurePosixPath("rejected")
_ORDER = PurePosixPath("order")
_CLOSED = PurePosixPath("closed")
_SUFFIX = ".json"
_REASON_SUFFIX = ".reason"
_SEQUENCE = re.compile(r"[0-9]{12}")


def validate_request_id(request_id: str) -> str:
    """Return ``request_id`` if it can name an inbox entry, else raise ``ProjectStateError``.

    One to 128 characters of letters, digits, ``.``, ``_`` and ``-``, starting with a letter
    or digit, so an id is one file name and never a path.
    """
    if not _REQUEST_ID.fullmatch(request_id):
        message = (
            f"control request id {request_id!r} must be 1-128 letters, digits, '.', '_' or "
            "'-', starting with a letter or digit"
        )
        raise ProjectStateError(message)
    return request_id


class InboxClosedError(ProjectStateError):
    """The run has ended and closed its inbox: no request posted now can apply."""


class RequestState(StrEnum):
    """What became of one posted request."""

    PENDING = "pending"
    """The run has not applied it yet; it applies at the run's next control poll."""
    APPLIED = "applied"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RequestStatus:
    """One request's state, and the reason for a rejected one."""

    state: RequestState
    reason: str | None = None


def ended_unapplied(reason: str) -> str:
    """Why a request the run never applied before it ended is rejected."""
    return f"the run ended before applying it: {reason}"


@dataclass(frozen=True)
class InboxEntry:
    """One pending request: its id and the bytes the operator wrote.

    ``unreadable`` says why the entry could not be read (a directory, a symlink, a file the
    run may not read), and ``payload`` is then empty. The run rejects such an entry with
    that reason instead of failing; rejecting it never reads it.
    """

    request_id: str
    payload: bytes
    unreadable: str | None = None


@dataclass(frozen=True)
class RejectedEntry:
    """One request the run could not apply, with the reason it gave."""

    request_id: str
    payload: bytes
    reason: str


class ControlInbox(Protocol):
    """Durable requests for one run, in the order they were posted.

    ``post`` is idempotent per id and refuses a different payload under an id ever posted,
    pending or not. ``pending`` lists what has been neither settled nor rejected, in posting
    order; it never fails on one bad entry. ``settle`` marks a request the run applied;
    ``reject`` marks one it could not apply, with the reason. Both are idempotent, neither
    touches another request, and a settled or rejected request is never pending again.
    ``close`` records that the run ended; ``status`` says what became of one request.
    """

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold ``payload`` as the request ``request_id``, ordered after every earlier post.

        Posting the same payload again is one request, in its first place; a different
        payload under an id already posted raises ``ProjectStateError``, also when two posts
        race. Raises ``InboxClosedError`` when the run has ended: before writing anything
        when it had ended already, and after writing when it ended during the post (the
        request then reads as rejected).
        """
        ...

    def pending(self) -> tuple[InboxEntry, ...]:
        """Every request neither settled nor rejected, in posting order.

        An entry no post ordered (one whose post died before claiming its place, or one the
        inbox cannot read) comes after every ordered one, by id.
        """
        ...

    def status(self, request_id: str) -> RequestStatus | None:
        """What became of ``request_id``, or None when it was never posted.

        After the run closed the inbox, a request it never marked reads as rejected.
        """
        ...

    def close(self, reason: str) -> None:
        """Record that the run ended (``reason``); the first reason stays."""
        ...

    def settle(self, request_id: str) -> None:
        """Mark ``request_id`` settled: the run applied it."""
        ...

    def reject(self, request_id: str, reason: str) -> None:
        """Mark ``request_id`` rejected with ``reason``: the run could not apply it."""
        ...

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """Every rejected request with its reason, ordered by id.

        A request rejected because it could not be read has an empty payload.
        """
        ...


class FileControlInbox:
    """The inbox as files in the run's control state.

    ``inbox/<id>.json`` holds each posted request, created exclusively and never removed;
    ``settled/<id>`` and ``rejected/<id>.reason`` mark what became of it. Each write is
    durable, so a reader never sees half a request and a crash never loses one that
    ``post`` returned for. Settling and rejecting write only markers, so an entry the run
    cannot read (a directory, a symlink, a file it may not open) is still set aside.
    """

    def __init__(self, namespace: StateNamespace) -> None:
        """Keep the requests in ``namespace``, the run's control state."""
        self._namespace = namespace

    def post(self, request_id: str, payload: bytes) -> None:
        """Create the request's file and claim its place, unless the run has ended.

        The run's end is checked before the write and again after it. An ending run settles
        or rejects every request it read, writes its closed marker, then sets aside what was
        posted meanwhile; so a post that sees no marker afterwards is read by the run, now or
        when it resumes, and one that sees it was applied before the end or never will be.
        """
        name = validate_request_id(request_id)
        _refuse_closed(self._closed(), name, written=False)
        path = _POSTED / f"{name}{_SUFFIX}"
        if not self._namespace.create_bytes(path, payload) and (
            self._namespace.read_bytes(path) != payload
        ):
            message = f"control request {request_id!r} was already posted with a different request"
            raise ProjectStateError(message)
        self._claim_place(name)
        closed = self._closed()
        if closed is not None and self.status(name) != RequestStatus(RequestState.APPLIED):
            _refuse_closed(closed, name, written=True)

    def pending(self) -> tuple[InboxEntry, ...]:
        """Read every unmarked request file, in posting order.

        An entry that cannot be read is listed with the reason, so one bad entry never hides
        the others or fails the run.
        """
        done = {
            *self._namespace.entries(_SETTLED),
            *(
                name.removesuffix(_REASON_SUFFIX)
                for name in self._namespace.entries(_REJECTED)
                if name.endswith(_REASON_SUFFIX)
            ),
        }
        entries = []
        for name in self._namespace.entries(_POSTED, list_symlinks=True):
            request_id = _request_id_of(name)
            if request_id is None or request_id in done:
                continue
            try:
                payload = self._namespace.read_bytes(_POSTED / name)
            except ProjectStateError as error:
                entries.append(InboxEntry(request_id, b"", unreadable=str(error)))
                continue
            if payload is not None:
                entries.append(InboxEntry(request_id, payload))
        places = self._places()
        return tuple(sorted(entries, key=lambda entry: _posting_key(places, entry.request_id)))

    def status(self, request_id: str) -> RequestStatus | None:
        """Read the request's markers; an unmarked one is pending until the run ends."""
        name = validate_request_id(request_id)
        if name not in {
            _request_id_of(entry) for entry in self._namespace.entries(_POSTED, list_symlinks=True)
        }:
            return None
        if name in self._namespace.entries(_SETTLED):
            return RequestStatus(RequestState.APPLIED)
        reason = self._namespace.read_bytes(_REJECTED / f"{name}{_REASON_SUFFIX}")
        if reason is not None:
            return RequestStatus(RequestState.REJECTED, reason.decode(errors="replace"))
        closed = self._closed()
        if closed is not None:
            return RequestStatus(RequestState.REJECTED, ended_unapplied(closed))
        return RequestStatus(RequestState.PENDING)

    def close(self, reason: str) -> None:
        """Write the closed marker with ``reason``; the first reason stays."""
        self._namespace.create_bytes(_CLOSED, reason.encode())

    def _closed(self) -> str | None:
        reason = self._namespace.read_bytes(_CLOSED)
        return None if reason is None else reason.decode(errors="replace")

    def _places(self) -> dict[str, int]:
        """Each ordered request's place: the first number it claimed."""
        places: dict[str, int] = {}
        for entry in sorted(self._namespace.entries(_ORDER)):
            if not _SEQUENCE.fullmatch(entry):
                continue
            held = self._namespace.read_bytes(_ORDER / entry)
            if held is not None:
                places.setdefault(held.decode(errors="replace"), int(entry))
        return places

    def _claim_place(self, name: str) -> None:
        """Claim the next free number for ``name``, unless it holds one already.

        Each failed claim means another post took that number first, so the loop ends after
        at most one step per concurrent post.
        """
        places = self._places()
        if name in places:
            return
        number = max(places.values(), default=0) + 1
        while not self._namespace.create_bytes(_ORDER / f"{number:012d}", name.encode()):
            number += 1

    def settle(self, request_id: str) -> None:
        """Write the request's settled marker."""
        self._namespace.create_bytes(_SETTLED / validate_request_id(request_id), b"")

    def reject(self, request_id: str, reason: str) -> None:
        """Write the request's reason under ``rejected/``; the first reason stays.

        The request itself is not read, so an entry the run cannot read is set aside too.
        """
        name = validate_request_id(request_id)
        self._namespace.create_bytes(_REJECTED / f"{name}{_REASON_SUFFIX}", reason.encode())

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """Read every rejected request and its reason, ordered by id."""
        found = []
        for name in self._namespace.entries(_REJECTED):
            if not name.endswith(_REASON_SUFFIX):
                continue
            request_id = name.removesuffix(_REASON_SUFFIX)
            if not _REQUEST_ID.fullmatch(request_id):
                continue
            reason = self._namespace.read_bytes(_REJECTED / name)
            try:
                payload = self._namespace.read_bytes(_POSTED / f"{request_id}{_SUFFIX}")
            except ProjectStateError:
                payload = None
            if reason is not None:
                text = reason.decode(errors="replace")
                found.append(RejectedEntry(request_id, payload or b"", text))
        return tuple(sorted(found, key=lambda entry: entry.request_id))


class FakeControlInbox:
    """An in-memory ``ControlInbox``, as strict about ids as ``FileControlInbox``."""

    def __init__(self) -> None:
        """Start empty."""
        self._posted: dict[str, bytes | InboxEntry] = {}
        self._places: dict[str, int] = {}
        self._settled: set[str] = set()
        self._rejected: dict[str, str] = {}
        self._closed: str | None = None

    def hold_unreadable(self, request_id: str, reason: str) -> None:
        """Hold an entry the run cannot read, as a directory in a file inbox would be."""
        self._posted[validate_request_id(request_id)] = InboxEntry(
            request_id, b"", unreadable=reason
        )

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold the request in its place, unless its id holds a different one or the run ended."""
        name = validate_request_id(request_id)
        _refuse_closed(self._closed, name, written=False)
        held = self._posted.setdefault(name, payload)
        if isinstance(held, InboxEntry):
            raise ProjectStateError(held.unreadable or "unreadable")
        if held != payload:
            message = f"control request {request_id!r} was already posted with a different request"
            raise ProjectStateError(message)
        self._places.setdefault(name, len(self._places) + 1)

    def pending(self) -> tuple[InboxEntry, ...]:
        """The unmarked requests, in posting order."""
        found = [
            held if isinstance(held, InboxEntry) else InboxEntry(key, held)
            for key, held in self._posted.items()
            if key not in self._settled and key not in self._rejected
        ]
        return tuple(sorted(found, key=lambda entry: _posting_key(self._places, entry.request_id)))

    def status(self, request_id: str) -> RequestStatus | None:
        """The request's state; an unmarked one is pending until the run ends."""
        name = validate_request_id(request_id)
        if name not in self._posted:
            return None
        if name in self._settled:
            return RequestStatus(RequestState.APPLIED)
        if name in self._rejected:
            return RequestStatus(RequestState.REJECTED, self._rejected[name])
        if self._closed is not None:
            return RequestStatus(RequestState.REJECTED, ended_unapplied(self._closed))
        return RequestStatus(RequestState.PENDING)

    def close(self, reason: str) -> None:
        """Record the run's end; the first reason stays."""
        if self._closed is None:
            self._closed = reason

    def settle(self, request_id: str) -> None:
        """Mark the request settled."""
        self._settled.add(validate_request_id(request_id))

    def reject(self, request_id: str, reason: str) -> None:
        """Mark the request rejected with its reason; the first reason stays."""
        self._rejected.setdefault(validate_request_id(request_id), reason)

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """The rejected requests, ordered by id."""
        found = []
        for key in sorted(self._rejected):
            held = self._posted.get(key, b"")
            payload = b"" if isinstance(held, InboxEntry) else held
            found.append(RejectedEntry(key, payload, self._rejected[key]))
        return tuple(found)


def _refuse_closed(reason: str | None, request_id: str, *, written: bool) -> None:
    if reason is None:
        return
    if written:
        message = (
            f"control request {request_id!r} was written as the run ended ({reason}); "
            "it will not apply"
        )
    else:
        message = f"the run has ended ({reason}); control request {request_id!r} was not posted"
    raise InboxClosedError(message)


def _posting_key(places: dict[str, int], request_id: str) -> tuple[int, int, str]:
    """Ordered requests by their place, then any without one, by id."""
    place = places.get(request_id)
    return (0, place, "") if place is not None else (1, 0, request_id)


def _request_id_of(name: str) -> str | None:
    """The id a pending or rejected file holds, or None for any other file."""
    if not name.endswith(_SUFFIX):
        return None
    stem = name.removesuffix(_SUFFIX)
    return stem if _REQUEST_ID.fullmatch(stem) else None


__all__ = [
    "ControlInbox",
    "FakeControlInbox",
    "FileControlInbox",
    "InboxClosedError",
    "InboxEntry",
    "RejectedEntry",
    "RequestState",
    "RequestStatus",
    "ended_unapplied",
    "validate_request_id",
]
