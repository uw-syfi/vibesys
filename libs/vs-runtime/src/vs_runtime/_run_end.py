"""Read the current epoch's ending and writer identity from the durable core record.

An ended epoch accepts no new inbox requests. Resume opens a successor epoch regardless
of the previous result; queued closing controls retain their original writer identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from vs_core.api import RunStatus
from vs_decode.api import Unreadable, decode_json
from vs_project.api import ProjectStateError, StoredEnvelope

if TYPE_CHECKING:
    from vs_core.api import CoreState
    from vs_project.api import Project

_STORE_DOCUMENT = "store.json"


def ending(core: CoreState) -> str | None:
    """Why the run ended, or None while core may still apply a request."""
    if core.run.status != RunStatus.TERMINAL:
        return None
    return _ending(None if core.run.result is None else core.run.result.reason)


def _ending(reason: str | None) -> str:
    return "the run ended" if reason is None else f"the run ended: {reason}"


_RECORD_MAX_BYTES = 256 * 1024 * 1024
"""The largest state document the project's stores read; a core record is one."""

_ANY_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
"""The record as plain JSON: only a few fields of it are read, in any envelope version.

A typed model of the record would refuse the record of another VibeSys version, whose
envelope this version cannot decode; ``status``, ``result.outcome`` and ``result.reason`` of
core's run state have kept their place and meaning across versions.
"""


def _member(value: JsonValue, *path: str) -> JsonValue:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


@dataclass(frozen=True)
class RecordedEnd:
    """An epoch's recorded ending, independent of successor launch eligibility."""

    outcome: str | None
    reason: str | None


def _core_record(project: Project, run_id: str) -> JsonValue:
    if project.state.state_store_namespace(run_id).read_bytes(_STORE_DOCUMENT) is None:
        # Probe first, as resume does, so a read never creates the store's lock or directory.
        return None
    stored = project.state_store(run_id).observe()
    if not isinstance(stored, StoredEnvelope):
        return None
    record = decode_json(
        stored.payload, _ANY_JSON, source=f"run {run_id!r} core record", max_bytes=_RECORD_MAX_BYTES
    )
    return None if isinstance(record, Unreadable) else _member(record, "envelope", "core")


def recorded_epoch(project: Project, run_id: str) -> int:
    """Current durable host epoch, or zero before the first writer starts."""
    epoch = _member(_core_record(project, run_id), "records", "recovery", "epoch")
    if epoch is None:
        return 0
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        message = "the core record has an invalid recovery epoch"
        raise ProjectStateError(message)
    return epoch


def recorded_end(project: Project, run_id: str) -> RecordedEnd | None:
    """How ``run_id`` ended, as its durable core record says, or None while it may apply more.

    A run whose core has not committed its first record has not ended. A quarantined record
    has not ended either: the run cannot go on until an operator repairs it, and then it may.

    Raises ``ProjectStateError`` when the record cannot be read.
    """
    run = _member(_core_record(project, run_id), "run")
    if run is None:
        return None
    status = _member(run, "status")
    if status not in {member.value for member in RunStatus}:
        message = f"run {run_id!r}: its core record does not say whether it ended"
        raise ProjectStateError(message)
    if status != RunStatus.TERMINAL.value:
        return None
    outcome = _member(run, "result", "outcome")
    reason = _member(run, "result", "reason")
    return RecordedEnd(
        outcome=outcome if isinstance(outcome, str) else None,
        reason=reason if isinstance(reason, str) else None,
    )


def recorded_ending(project: Project, run_id: str) -> str | None:
    """Why ``run_id`` ended, as its durable core record says, or None while it may apply more.

    Raises ``ProjectStateError`` when the record cannot be read (see `recorded_end`).
    """
    end = recorded_end(project, run_id)
    return None if end is None else _ending(end.reason)


__all__ = ["RecordedEnd", "ending", "recorded_end", "recorded_ending", "recorded_epoch"]
