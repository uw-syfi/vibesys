"""The steer outbox: orchestrator notes waiting for a workstream's next worker turn.

A steer is a short note the orchestrator agent sends to one implement
workstream. It waits in ``DynamicState.agent.steers`` until the host renders
the workstream's next worker turn (implementer or judge), which marks it
delivered to that turn's invocation id in the commit that records the turn
start. A note still pending when its workstream settles is dropped, and the
drop is journaled. Steers exist only in agent mode (``state.agent`` is set): in
planner mode nothing is pending and every operation here is a no-op.

Every function mutates ``state`` in memory only; the caller commits it, so a
delivery or drop is durable exactly when the transition it belongs to is.

Guarantees, for any sequence of calls:

- at most :data:`MAX_PENDING` notes per workstream are pending (neither
  delivered nor dropped); a further note is refused ``rate_limited``;
- a note is delivered at most once and dropped at most once, never both;
- a delivered note records the invocation id of the turn that received it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import (
    JournalEntry,
    SteerNote,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.prompts import render_steer_dropped

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.models import AgentLoopState, DynamicState

# Section 3 of docs/contributing/dynamic-orchestrator-agent.md.
MAX_PENDING = 3
MAX_TEXT_CHARS = 2000


class SteerRefusal(StrEnum):
    """Why the outbox did not accept a steer."""

    RATE_LIMITED = "rate_limited"  # MAX_PENDING notes already wait for this workstream.
    TEXT_LENGTH = "text_length"  # Empty, or longer than MAX_TEXT_CHARS.
    UNKNOWN_WORKSTREAM = "unknown_workstream"  # No implement workstream has this id.
    WORKSTREAM_SETTLED = "workstream_settled"  # Its round is recorded; no turn follows.


@dataclass(frozen=True, slots=True)
class SteerAccepted:
    """The note was queued for the workstream's next worker turn."""

    note: SteerNote


@dataclass(frozen=True, slots=True)
class SteerRefused:
    """The note was not queued; nothing changed."""

    code: SteerRefusal


class SteerOutsideAgentModeError(RuntimeError):
    """A steer was sent in planner mode, which has no orchestrator agent to send one."""


def enqueue(
    state: DynamicState, agent_id: str, text: str, *, at_s: float, interrupt: bool
) -> SteerAccepted | SteerRefused:
    """Queue ``text`` for ``agent_id``'s next worker turn, or say why not.

    ``at_s`` is run-elapsed seconds. Raises :class:`SteerOutsideAgentModeError`
    in planner mode.
    """
    loop = _agent(state)
    if not 1 <= len(text) <= MAX_TEXT_CHARS:
        return SteerRefused(SteerRefusal.TEXT_LENGTH)
    item = next((item for item in state.workstreams if item.hypothesis_id == agent_id), None)
    if item is None:
        return SteerRefused(SteerRefusal.UNKNOWN_WORKSTREAM)
    if item.phase is WorkstreamPhase.CANCELLED or _round_recorded(state, item.sequence):
        return SteerRefused(SteerRefusal.WORKSTREAM_SETTLED)
    if len(pending(state, agent_id)) >= MAX_PENDING:
        return SteerRefused(SteerRefusal.RATE_LIMITED)
    note = SteerNote(
        note_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        sent_at_s=at_s,
        interrupt=interrupt,
    )
    loop.steers[agent_id] = [*loop.steers.get(agent_id, []), note]
    return SteerAccepted(note)


def pending(state: DynamicState, agent_id: str) -> tuple[SteerNote, ...]:
    """Return the notes for ``agent_id`` neither delivered nor dropped, oldest first."""
    return tuple(note for note in _notes(state, agent_id) if _is_pending(note))


def mark_delivered(state: DynamicState, agent_id: str, invocation_id: str) -> tuple[SteerNote, ...]:
    """Deliver every pending note of ``agent_id`` to ``invocation_id``; return what it renders.

    The result is every note delivered to ``invocation_id``, including notes an
    earlier call delivered to the same id. A turn that a crash cut off after
    its start was committed is redone under the same id, so its notes render
    again instead of being lost.
    """
    if state.agent is None or agent_id not in state.agent.steers:
        return ()
    notes = [
        note.model_copy(update={"delivered_to": invocation_id}) if _is_pending(note) else note
        for note in state.agent.steers[agent_id]
    ]
    state.agent.steers[agent_id] = notes
    return tuple(note for note in notes if note.delivered_to == invocation_id)


def drop_pending(state: DynamicState, agent_id: str, *, at_s: float) -> tuple[SteerNote, ...]:
    """Drop the pending notes of a settled workstream, journal each drop, and return them."""
    if state.agent is None or agent_id not in state.agent.steers:
        return ()
    loop = state.agent
    before = loop.steers[agent_id]
    loop.steers[agent_id] = [
        note.model_copy(update={"dropped": "workstream_settled"}) if _is_pending(note) else note
        for note in before
    ]
    dropped = tuple(
        note for old, note in zip(before, loop.steers[agent_id], strict=True) if _is_pending(old)
    )
    loop.journal.extend(
        JournalEntry(
            at_s=at_s,
            turn=loop.turns,
            kind="steer",
            subject=agent_id,
            text=render_steer_dropped(note_sha256=note.note_sha256, sent_at_s=note.sent_at_s),
        )
        for note in dropped
    )
    return dropped


def _agent(state: DynamicState) -> AgentLoopState:
    if state.agent is None:
        message = "steers exist only in agent mode; planner mode has no orchestrator agent"
        raise SteerOutsideAgentModeError(message)
    return state.agent


def _notes(state: DynamicState, agent_id: str) -> list[SteerNote]:
    if state.agent is None:
        return []
    return state.agent.steers.get(agent_id, [])


def _is_pending(note: SteerNote) -> bool:
    return note.delivered_to is None and note.dropped is None


def _round_recorded(state: DynamicState, sequence: int) -> bool:
    return any(record.round_number == sequence for record in state.search.rounds)


__all__ = [
    "MAX_PENDING",
    "MAX_TEXT_CHARS",
    "SteerAccepted",
    "SteerOutsideAgentModeError",
    "SteerRefusal",
    "SteerRefused",
    "drop_pending",
    "enqueue",
    "mark_delivered",
    "pending",
]
