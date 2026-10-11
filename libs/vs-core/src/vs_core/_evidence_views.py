"""Pure views over core's evidence ledger and attempt checkpoints.

Core's evidence ledger is the authority. A strategy keeps only `EvidenceKey`s and
accepts a measurement result's evidence only when it is trusted and belongs to exactly the
measurement the strategy asked for: another scope, generation, candidate, purpose or request,
or an untrusted record, is never offered to a decision. These functions are total and
read only core values, so every strategy applies one rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_core.types.common import (
        AttemptId,
        EvidenceKey,
        InvocationRef,
        RevisionRef,
        Scope,
    )
    from vs_core.types.evaluation import EvidenceRef, MeasurementResult
    from vs_core.types.kernel import RunView


def trusted_keys(
    event: MeasurementResult, *, scope: Scope, candidate: RevisionRef, purpose: str
) -> tuple[EvidenceKey, ...]:
    """Keys of the result's evidence that is trusted and belongs to this measurement.

    The record must carry the measured scope (owner and generation), the measured
    candidate and the requested purpose, and, when the result names its submission
    request, come from that request.
    """
    if event.scope != scope:
        return ()
    return tuple(
        item.key
        for item in event.evidence
        if item.provenance == "trusted"
        and item.scope == scope
        and item.candidate == candidate
        and item.purpose == purpose
        and (event.source_request is None or item.source_request == event.source_request)
    )


def ledger_refs(view: RunView, keys: tuple[EvidenceKey, ...]) -> tuple[EvidenceRef, ...] | None:
    """The ledger records for ``keys`` in order, or None when core holds any of them not."""
    ledger = {item.key: item for item in view.measurements}
    refs = tuple(ledger.get(key) for key in keys)
    if any(item is None for item in refs):
        return None
    return tuple(item for item in refs if item is not None)


def turn_candidate(
    view: RunView,
    attempt: AttemptId,
    invocation: InvocationRef,
    *,
    predecessors: tuple[InvocationRef, ...],
) -> RevisionRef | None:
    """The latest revision retained by this turn or by a turn it continues.

    ``predecessors`` are the earlier turns of the same logical answer (the suspended turn a
    resume continues, an invalid or lost turn a correction replaces); `TurnChain.candidate`
    supplies them. A turn that only reads a verdict edits nothing, so its candidate is what the
    turn it resumed checkpointed. A different logical turn, such as a paid retry, is not a
    predecessor: that retained nothing new yields None even when the attempt holds older
    checkpoints, so it reports "retained no changed candidate".
    """
    live = next((item for item in view.attempts if item.attempt_id == attempt), None)
    if live is None:
        return None
    chain = {invocation, *predecessors}
    mine = tuple(item for item in live.checkpoints if item.invocation in chain)
    return mine[-1].revision if mine else None
