"""The only way runtime code builds a core ``Observation``.

Core accepts a later observation of a request only if it carries a higher
``sequence`` than every earlier one, or is identical to the latest. An executor
that always emits sequence 0 therefore cannot report "Unknown" and later
"Succeeded" for one request: core rejects the second observation.

The factory keeps one durable row per request: the last observation it issued.
Reporting the same facts again returns that row unchanged (a replay, byte for
byte, including the time). Reporting different facts issues the next sequence
and stores it before returning, so a crash between issue and delivery replays
the same observation, never a gap or a reused number. Identity (event id,
request, scope, admission) is derived from the request and never supplied by
the caller.

Rows live in the shared ``ReceiptStore`` beside the executor's receipts. Callers serialize per request (the executors hold a per-request
lock); an unreadable row raises ``ObservationLedgerCorruptError`` because a
sequence cannot be chosen without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vs_core.api import (
    DecisionId,
    EventId,
    Observation,
    ObservationStatus,
    RequestId,
    ResourceId,
    Scope,
)
from vs_project.api import ProjectStateError

if TYPE_CHECKING:
    from vs_core.api import RequestBase
    from vs_runtime._receipt_store import ReceiptStore


_FAMILY = "observations"
_PART = "observation"


class ObservationLedgerCorruptError(Exception):
    """A stored observation row cannot be read back, so no sequence can be chosen."""


@dataclass(frozen=True)
class ObservationFacts:
    """What an executor learned about one request, without any identity fields."""

    status: ObservationStatus
    terminal: bool = True
    accepted: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    children_complete: bool = False
    resource_id: ResourceId | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class ObservationSubject:
    """Whose observation it is: a request, in the scope and episode core knows it under."""

    request_id: RequestId
    scope: Scope
    admission_id: DecisionId | None

    @classmethod
    def of(cls, request: RequestBase, *, request_id: RequestId | None = None) -> ObservationSubject:
        """The request itself, or *request_id* when the request reports on another one.

        An inspection of a target observes the target, in the inspecting
        request's scope and episode, and continues the target's own sequence.
        """
        chosen = request_id or request.request_id
        if chosen is None:
            message = "request_id: an observation requires a canonical request identity"
            raise ValueError(message)
        return cls(chosen, request.scope, request.admission_id)


class ObservationFactory:
    """Issue observations whose sequence core accepts across retries and restarts."""

    def __init__(self, store: ReceiptStore) -> None:
        self._store = store

    def observe(
        self,
        subject: ObservationSubject,
        facts: ObservationFacts,
        *,
        observed_at: float,
        fresh: bool = False,
    ) -> Observation:
        """Return the observation of *subject* reporting *facts*.

        The same facts as the latest issued observation return it unchanged;
        different facts return a new observation with the next sequence.
        ``fresh`` marks a poll of a resource that changes over time: every poll
        is a new observation with the next sequence, even when its facts match
        the last one, because consumers read its time as a new sample.
        """
        key = subject.request_id.root
        latest = self._load(key)
        sequence = 0 if latest is None else latest.sequence + 1

        def build(number: int) -> Observation:
            return Observation(
                event_id=EventId(root=f"{key}:observation:{number}"),
                request_id=subject.request_id,
                scope=subject.scope,
                admission_id=subject.admission_id,
                sequence=number,
                observed_at=observed_at,
                status=facts.status,
                resource_id=facts.resource_id,
                accepted=facts.accepted,
                terminal=facts.terminal,
                released=facts.released,
                children=facts.children,
                children_complete=facts.children_complete,
                diagnostic=facts.diagnostic,
            )

        candidate = build(sequence)
        if not fresh and latest is not None and _same_facts(latest, candidate):
            return latest
        self._store.replace(_FAMILY, _PART, key, candidate)
        return candidate

    def _load(self, key: str) -> Observation | None:
        try:
            return self._store.load(_FAMILY, _PART, key, Observation)
        except (ValidationError, ProjectStateError) as error:
            message = f"observation row for {key} is unreadable"
            raise ObservationLedgerCorruptError(message) from error


def _same_facts(stored: Observation, candidate: Observation) -> bool:
    """Equal in everything except the sequence, its event id and the observation time."""
    ignored = {"event_id": stored.event_id, "sequence": stored.sequence}
    return stored.model_copy(update={"observed_at": candidate.observed_at}) == candidate.model_copy(
        update=ignored
    )
