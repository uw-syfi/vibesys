"""The operator's control inbox: requests written from outside a run, read by the run.

An operator (or an orchestrating agent, over ssh) controls a run that has no socket by
writing one file per request into the run's state directory. The run reads the inbox at
each control poll, applies each request once, and only then marks it settled, so a request
written while the run is down, or one the run read but had not yet committed when it
crashed, is applied after the run resumes. The request's own id names its file, and the file
is never removed: posting the same request again is one request, and an id, once posted,
never names another request.

The inbox stores opaque bytes; what a request says is the run's business
(``vs_runtime``'s ``ControlRequest``). A request the run cannot apply is marked rejected with
the reason, never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from vs_project._state import StateNamespace

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_POSTED = PurePosixPath("inbox")
_SETTLED = PurePosixPath("settled")
_REJECTED = PurePosixPath("rejected")
_SUFFIX = ".json"
_REASON_SUFFIX = ".reason"


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
    """Durable requests for one run, in the order of their ids.

    ``post`` is idempotent per id and refuses a different payload under an id ever posted,
    pending or not. ``pending`` lists what has been neither settled nor rejected, ordered by
    id; it never fails on one bad entry. ``settle`` marks a request the run applied;
    ``reject`` marks one it could not apply, with the reason. Both are idempotent, neither
    touches another request, and a settled or rejected request is never pending again.
    """

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold ``payload`` as the request ``request_id``.

        Posting the same payload again is one request; a different payload under an id
        already posted raises ``ProjectStateError``, also when two posts race.
        """
        ...

    def pending(self) -> tuple[InboxEntry, ...]:
        """Every request neither settled nor rejected, ordered by id."""
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
        """Create the request's file, unless its id already holds a different request."""
        path = _POSTED / f"{validate_request_id(request_id)}{_SUFFIX}"
        if self._namespace.create_bytes(path, payload):
            return
        if self._namespace.read_bytes(path) != payload:
            message = f"control request {request_id!r} was already posted with a different request"
            raise ProjectStateError(message)

    def pending(self) -> tuple[InboxEntry, ...]:
        """Read every unmarked request file, ordered by id.

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
        return tuple(sorted(entries, key=lambda entry: entry.request_id))

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
        self._settled: set[str] = set()
        self._rejected: dict[str, str] = {}

    def hold_unreadable(self, request_id: str, reason: str) -> None:
        """Hold an entry the run cannot read, as a directory in a file inbox would be."""
        self._posted[validate_request_id(request_id)] = InboxEntry(
            request_id, b"", unreadable=reason
        )

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold the request, unless its id already holds a different one."""
        held = self._posted.setdefault(validate_request_id(request_id), payload)
        if isinstance(held, InboxEntry):
            raise ProjectStateError(held.unreadable or "unreadable")
        if held != payload:
            message = f"control request {request_id!r} was already posted with a different request"
            raise ProjectStateError(message)

    def pending(self) -> tuple[InboxEntry, ...]:
        """The unmarked requests, ordered by id."""
        return tuple(
            held if isinstance(held, InboxEntry) else InboxEntry(key, held)
            for key, held in sorted(self._posted.items())
            if key not in self._settled and key not in self._rejected
        )

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
    "InboxEntry",
    "RejectedEntry",
    "validate_request_id",
]
