"""The operator's control inbox: requests written from outside a run, read by the run.

An operator (or an orchestrating agent, over ssh) controls a run that has no socket by
writing one file per request into the run's state directory. The run reads the inbox at
each control poll, applies each request once, and only then removes it, so a request
written while the run is down, or one the run read but had not yet committed when it
crashed, is applied after the run resumes. The request's own id names its file: writing the
same request twice is one request, and the run recognises one it already applied by that id.

The inbox stores opaque bytes; what a request says is the run's business
(``vs_runtime``'s ``ControlRequest``). A request the run cannot apply is moved aside with
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
_PENDING = PurePosixPath("inbox")
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
    """One pending request: its id and the bytes the operator wrote."""

    request_id: str
    payload: bytes


@dataclass(frozen=True)
class RejectedEntry:
    """One request the run could not apply, with the reason it gave."""

    request_id: str
    payload: bytes
    reason: str


class ControlInbox(Protocol):
    """Durable requests for one run, in the order of their ids.

    ``post`` is idempotent per id. ``pending`` lists what has not been settled or rejected,
    ordered by id. ``settle`` removes a request the run applied; ``reject`` moves one it
    could not apply aside, with the reason. Both are idempotent, and neither touches another
    request.
    """

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold ``payload`` as the request ``request_id``, replacing an identical post."""
        ...

    def pending(self) -> tuple[InboxEntry, ...]:
        """Every request neither settled nor rejected, ordered by id."""
        ...

    def settle(self, request_id: str) -> None:
        """Remove ``request_id``: the run applied it."""
        ...

    def reject(self, request_id: str, reason: str) -> None:
        """Move ``request_id`` aside with ``reason``: the run could not apply it."""
        ...

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """Every rejected request with its reason, ordered by id."""
        ...


class FileControlInbox:
    """The inbox as files in the run's control state: ``inbox/<id>.json``.

    Each write is atomic and durable (the namespace's write), so a reader never sees half
    a request and a crash never loses one that ``post`` returned for.
    """

    def __init__(self, namespace: StateNamespace) -> None:
        """Keep the requests in ``namespace``, the run's portable control state."""
        self._namespace = namespace

    def post(self, request_id: str, payload: bytes) -> None:
        """Write the request's file atomically."""
        self._namespace.write_bytes(
            _PENDING / f"{validate_request_id(request_id)}{_SUFFIX}", payload
        )

    def pending(self) -> tuple[InboxEntry, ...]:
        """Read every request file, ordered by id; a file that vanished meanwhile is skipped."""
        entries = []
        for name in self._namespace.entries(_PENDING):
            request_id = _request_id_of(name)
            if request_id is None:
                continue
            payload = self._namespace.read_bytes(_PENDING / name)
            if payload is not None:
                entries.append(InboxEntry(request_id, payload))
        return tuple(sorted(entries, key=lambda entry: entry.request_id))

    def settle(self, request_id: str) -> None:
        """Delete the request's file, if it is still there."""
        self._namespace.delete(_PENDING / f"{validate_request_id(request_id)}{_SUFFIX}")

    def reject(self, request_id: str, reason: str) -> None:
        """Write the request and its reason under ``rejected/``, then delete the pending file."""
        name = validate_request_id(request_id)
        payload = self._namespace.read_bytes(_PENDING / f"{name}{_SUFFIX}")
        if payload is None:
            return
        self._namespace.write_bytes(_REJECTED / f"{name}{_SUFFIX}", payload)
        self._namespace.write_bytes(_REJECTED / f"{name}{_REASON_SUFFIX}", reason.encode())
        self._namespace.delete(_PENDING / f"{name}{_SUFFIX}")

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """Read every rejected request and its reason, ordered by id."""
        found = []
        for name in self._namespace.entries(_REJECTED):
            request_id = _request_id_of(name)
            if request_id is None:
                continue
            payload = self._namespace.read_bytes(_REJECTED / name)
            reason = self._namespace.read_bytes(_REJECTED / f"{request_id}{_REASON_SUFFIX}")
            if payload is not None:
                text = "" if reason is None else reason.decode(errors="replace")
                found.append(RejectedEntry(request_id, payload, text))
        return tuple(sorted(found, key=lambda entry: entry.request_id))


class FakeControlInbox:
    """An in-memory ``ControlInbox``, as strict about ids as ``FileControlInbox``."""

    def __init__(self) -> None:
        """Start empty."""
        self._pending: dict[str, bytes] = {}
        self._rejected: dict[str, RejectedEntry] = {}

    def post(self, request_id: str, payload: bytes) -> None:
        """Hold the request."""
        self._pending[validate_request_id(request_id)] = payload

    def pending(self) -> tuple[InboxEntry, ...]:
        """The held requests, ordered by id."""
        return tuple(InboxEntry(key, self._pending[key]) for key in sorted(self._pending))

    def settle(self, request_id: str) -> None:
        """Forget the request."""
        self._pending.pop(validate_request_id(request_id), None)

    def reject(self, request_id: str, reason: str) -> None:
        """Move the request aside with its reason."""
        payload = self._pending.pop(validate_request_id(request_id), None)
        if payload is not None:
            self._rejected[request_id] = RejectedEntry(request_id, payload, reason)

    def rejected(self) -> tuple[RejectedEntry, ...]:
        """The rejected requests, ordered by id."""
        return tuple(self._rejected[key] for key in sorted(self._rejected))


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
