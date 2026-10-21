"""Whether a run has ended, as its core record says: the one fact for "no request applies now".

Core's TERMINAL status is final except for a run the operator's stop ended, which a resume
reopens (``RecordedEnd.reopens``); any other TERMINAL run never resumes (resume refuses it with
``run_ended``). So a request posted to a TERMINAL run's inbox never applies while no process holds
the run, and one it never applied before it ended never will. The run's own end of inbox (``RunControlBridge.end``) and
``vibesys control``, which reads the record from outside the run, both judge the end by this
status; neither keeps a copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

import vs_core.api as core
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
    """How a run's durable core record says it ended: core's result outcome and reason.

    Either is None when the record of an ended run does not carry it. ``reopens`` is whether a
    resume reopens the run (``vs_core.api.reopens_on_resume``): the operator's stop ended it,
    and a stop is a pause.
    """

    outcome: str | None
    reason: str | None
    reopens: bool = False


def recorded_end(project: Project, run_id: str) -> RecordedEnd | None:
    """How ``run_id`` ended, as its durable core record says, or None while it may apply more.

    A run whose core has not committed its first record has not ended. A quarantined record
    has not ended either: the run cannot go on until an operator repairs it, and then it may.

    Raises ``ProjectStateError`` when the record cannot be read.
    """
    if project.state.state_store_namespace(run_id).read_bytes(_STORE_DOCUMENT) is None:
        # Probe first, as resume does, so a read never creates the store's lock or directory.
        return None
    stored = project.state_store(run_id).observe()
    if not isinstance(stored, StoredEnvelope):
        return None
    record = decode_json(
        stored.payload, _ANY_JSON, source=f"run {run_id!r} core record", max_bytes=_RECORD_MAX_BYTES
    )
    run = None if isinstance(record, Unreadable) else _member(record, "envelope", "core", "run")
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
        reopens=_reopens(run),
    )


@cache
def _controls() -> TypeAdapter[tuple[core.ControlInput, ...]]:
    return TypeAdapter(tuple[core.ControlInput, ...])


def _reopens(run: JsonValue) -> bool:
    """Whether a resume reopens the run its record holds: core's own rule over what it reads.

    The rule reads the status, the result and the controls, which keep their shape across
    envelope versions and need no operation registry to read, unlike the rest of the run
    state. A record whose controls or result this version cannot read is not reopened here: the
    resume itself names what it cannot read.
    """
    result_json = _member(run, "result")
    if result_json is None:
        return False
    result = decode_json(
        _ANY_JSON.dump_json(result_json),
        core.RunResultProposal,
        source="run result",
        max_bytes=_RECORD_MAX_BYTES,
    )
    controls = decode_json(
        _ANY_JSON.dump_json(_member(run, "controls") or []),
        _controls(),
        source="run controls",
        max_bytes=_RECORD_MAX_BYTES,
    )
    status = _member(run, "status")
    if isinstance(result, Unreadable) or isinstance(controls, Unreadable):
        return False
    return status in {member.value for member in RunStatus} and core.reopens_after(
        RunStatus(status), result, controls
    )


def recorded_ending(project: Project, run_id: str) -> str | None:
    """Why ``run_id`` ended, as its durable core record says, or None while it may apply more.

    Raises ``ProjectStateError`` when the record cannot be read (see `recorded_end`).
    """
    end = recorded_end(project, run_id)
    return None if end is None else _ending(end.reason)


__all__ = ["RecordedEnd", "ending", "recorded_end", "recorded_ending"]
