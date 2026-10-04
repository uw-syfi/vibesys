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

Rows live in the same machine-local ``StateNamespace`` as the executor's
receipts. Callers serialize per request (the executors hold a per-request
lock); an unreadable row raises ``ObservationLedgerCorruptError`` because a
sequence cannot be chosen without it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vs_core.api import EventId, Observation, ObservationStatus, ResourceId
from vs_project.api import ProjectStateError

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_project.api import StateNamespace


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


class ObservationFactory:
    """Issue observations whose sequence core accepts across retries and restarts."""

    def __init__(self, namespace: StateNamespace) -> None:
        self._namespace = namespace

    def observe(
        self,
        request: Request,
        facts: ObservationFacts,
        *,
        observed_at: float,
        subject: RequestId | None = None,
    ) -> Observation:
        """Return the observation of *request* reporting *facts*.

        The same facts as the latest issued observation return it unchanged;
        different facts return a new observation with the next sequence.
        *subject* names another request that *request* reports on (an inspection
        of its target): the observation is of the subject, in *request*'s scope
        and episode, and continues the subject's own sequence.
        """
        request_id = subject or request.request_id
        if request_id is None:
            message = "request_id: an observation requires a canonical request identity"
            raise ValueError(message)
        path = f"observations/{hashlib.sha256(request_id.root.encode()).hexdigest()}.json"
        latest = self._load(path)
        sequence = 0 if latest is None else latest.sequence + 1

        def build(number: int) -> Observation:
            return Observation(
                event_id=EventId(root=f"{request_id.root}:observation:{number}"),
                request_id=request_id,
                scope=request.scope,
                admission_id=request.admission_id,
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
        if latest is not None and _same_facts(latest, candidate):
            return latest
        self._namespace.write_bytes(path, candidate.model_dump_json().encode())
        return candidate

    def _load(self, path: str) -> Observation | None:
        try:
            raw = self._namespace.read_bytes(path)
            return None if raw is None else Observation.model_validate_json(raw)
        except (ValidationError, ProjectStateError) as error:
            message = f"observation row {path} is unreadable"
            raise ObservationLedgerCorruptError(message) from error


def _same_facts(stored: Observation, candidate: Observation) -> bool:
    """Equal in everything except the sequence, its event id and the observation time."""
    ignored = {"event_id": stored.event_id, "sequence": stored.sequence}
    return stored.model_copy(update={"observed_at": candidate.observed_at}) == candidate.model_copy(
        update=ignored
    )
