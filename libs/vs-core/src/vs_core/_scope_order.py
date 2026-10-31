"""Order the open steps of one scope by what they do to its files.

A snapshot reads the files of a scope to retain them, so it must not see a half-written tree: it
runs only when every writer of its scope has a result, and a writer starts only while no snapshot
of its scope runs. A writer that was interrupted has no result either, so a snapshot waits for
it to run again and end; its partial writes are reset by ``clear`` before that attempt, never
retained. A writer that depends on the snapshot cannot end before it, so it does not hold it back.

Closing a scope reads the registry of what the scope holds (its jobs, its sessions) and fences the
rest, so the close runs only when every other step of its scope has a result: a step that began
before the close and reports its job after it would leave that job running with no one to cancel it.
A step that waits for the close itself cannot end before it, and does not hold it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._request_recovery import is_open
from .types.records import StepEffect, request_effect

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import CoreState
    from .types.records import RequestRecord


def holds_back(state: CoreState, record: RequestRecord) -> bool:
    """Whether another open step of ``record``'s scope must end, or stop running, first."""
    effect = request_effect(record.request, state.registry)
    if effect is StepEffect.NEITHER:
        return False
    siblings = [
        row
        for row in state.records.records
        if is_open(row)
        and row.request_id != record.request_id
        and row.request.scope == record.request.scope
    ]
    if effect is StepEffect.CLOSES:
        return any(not _follows(state, row, record.request_id) for row in siblings)
    if effect is StepEffect.SNAPSHOTS:
        return any(
            request_effect(row.request, state.registry) is StepEffect.WRITES
            and not _follows(state, row, record.request_id)
            for row in siblings
        )
    return any(
        request_effect(row.request, state.registry) is StepEffect.SNAPSHOTS
        and _has_running_attempt(state, row)
        for row in siblings
    )


def _has_running_attempt(state: CoreState, record: RequestRecord) -> bool:
    attempts = state.steps.of(record.request_id)
    return attempts is not None and not attempts.ended


def _follows(state: CoreState, record: RequestRecord, tag: RequestId) -> bool:
    """Whether ``record`` waits, directly or through other steps, for the step ``tag``."""
    pending = list(record.request.depends_on)
    seen: set[RequestId] = set()
    while pending:
        current = pending.pop()
        if current == tag:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(
            dependency
            for row in state.records.find(current)
            for dependency in row.request.depends_on
        )
    return False
