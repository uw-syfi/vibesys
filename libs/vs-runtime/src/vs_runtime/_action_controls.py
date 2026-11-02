"""Operator actions as core controls: the control each request becomes, and when to refuse it.

An operator action (``CancelAttemptRequest``, ``CancelItemRequest``, ``SetMaxParallelRequest``)
becomes one core control (``ControlInput``) of the same action. Core applies it and delivers
its notice, a text rendered from ``prompts/operator_action.j2`` and stored like a steer's text,
to the run's agents at their next turn. Core's ``control_refusal`` is the one statement of
when an action is refused; this module only asks it at submission, and the run loop's bridge
asks it again when it takes the action.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from vs_core.api import (
    ActionRefusal,
    ArtifactId,
    ArtifactRef,
    AttemptId,
    ControlId,
    ControlInput,
    ItemId,
    control_refusal,
    item_cancellation,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime._operator_control import (
    CancelAttemptRequest,
    CancelItemRequest,
    RefusalReason,
    SetMaxParallelRequest,
    action_subject,
)

if TYPE_CHECKING:
    from vs_core.api import CoreState
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._operator_control import ActionRequest

_RENDERER = TemplateRenderer(Path(__file__).with_name("prompts"))


def notice_text(request: ActionRequest, core: CoreState) -> RenderedPrompt:
    """What the run's agents are told at their next turn once the run took ``request``.

    ``core`` is the state the run takes it in: a cancelled item's notice says what became
    of each of its attempts there (``item_cancellation``).
    """
    attempts: dict[str, tuple[str, ...]] = {
        "stopped": (),
        "parked": (),
        "settling": (),
    }
    if isinstance(request, CancelItemRequest):
        done = item_cancellation(core, ItemId(root=request.item))
        attempts = {
            "stopped": tuple(a.root for a in done.stopped),
            "parked": tuple(a.root for a in done.parked),
            "settling": tuple(a.root for a in done.settling),
        }
    return _RENDERER.render_template(
        "operator_action.j2",
        action=request.action.value,
        subject=action_subject(request),
        **attempts,
    )


def action_control(identity: ControlId, request: ActionRequest, notice: str) -> ControlInput:
    """The control core records for ``request``; it carries the digest of its ``notice``."""
    notice_ref = ArtifactRef(
        artifact_id=ArtifactId(root=f"notice-{identity.root}"),
        digest=hashlib.sha256(notice.encode()).hexdigest(),
    )
    match request:
        case CancelAttemptRequest(attempt=attempt):
            subject: dict[str, object] = {"attempt": AttemptId(root=attempt)}
        case CancelItemRequest(item=item):
            subject = {"item": ItemId(root=item)}
        case SetMaxParallelRequest(max_parallel=limit):
            subject = {"max_parallel": limit}
    return ControlInput.model_validate(
        {"control_id": identity, "action": request.action.value, "artifact": notice_ref, **subject}
    )


def records_action(control: ControlInput, request: ActionRequest) -> bool:
    """Whether ``control`` records ``request``: the same action on the same subject."""
    probe = action_control(control.control_id, request, "")
    return (control.action, control.attempt, control.item, control.max_parallel) == (
        probe.action,
        probe.attempt,
        probe.item,
        probe.max_parallel,
    )


_FINAL: dict[ActionRefusal, RefusalReason] = {
    ActionRefusal.ENDED: RefusalReason.ATTEMPT_ENDED,
    ActionRefusal.ALREADY_CANCELLED: RefusalReason.ALREADY_CANCELLED,
    ActionRefusal.ABOVE_LAUNCH_LIMIT: RefusalReason.ABOVE_LAUNCH_LIMIT,
}
"""Core's refusals of an action that no later state of the run could take."""


def submit_refusal(core: CoreState, request: ActionRequest) -> RefusalReason | None:
    """Why no later state of ``core`` could take ``request``, or None.

    ``core`` may be a poll behind, so only what cannot change is refused: an attempt that has
    ended, an item already cancelled, or a limit above the launch limit. A subject core does
    not hold yet may appear before the run takes the request; the run asks core again then.
    """
    refusal = control_refusal(core, action_control(ControlId(root="probe"), request, ""))
    kind = None if refusal is None else refusal.action_refusal
    return None if kind is None else _FINAL.get(kind)


__all__ = ["action_control", "notice_text", "records_action", "submit_refusal"]
