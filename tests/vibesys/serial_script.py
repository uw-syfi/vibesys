"""Scripted far side of a serial run: what the single and multi agents reply and what they measure.

`SerialExecutors` is the dynamic script (`dynamic_script.Executors`) with the roles and the
operations of a serial search: it answers the requests core makes of the host through the real
core, whether by the pure driver, the production shell or a hand-built shell. An executor reports
what it saw and never core's conclusions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from tests.support.dynamic_script import FACTS, TERMS, Executors, _Bounds, lease

from vibesys.candidates.api import limits_for
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    CommandCapture,
    ExecuteRegisteredOperation,
    FlowState,
    LocalValidationReport,
    ParentVerification,
    RenderedArtifacts,
    RunEnvelope,
)
from vs_core.api.search import (
    CaptureCommandBase,
    InterpretEvidenceBase,
    RenderRoleArtifactsBase,
    ValidateLocalBase,
    VerifyParentRevisionBase,
)
from vs_core.testing.drive import Answer, Harness, Succeeded, Unknown

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vibesys.candidates.api import RunBudget
    from vs_core.api import DispatchTurn, EvidenceRequirements, Limits, ResumeSessionTurn, SearchDef


def harness_of[C](
    search: SearchDef[C],
    budget: RunBudget,
    requirements: EvidenceRequirements,
    **overrides: object,
) -> Harness:
    """What hosts ``search`` on the real core, with the limits its ``budget`` asks for."""
    limits: Limits = limits_for(budget, _Bounds())
    assert search.operations is not None
    hosting = Harness(
        registry=search.operations.registry(),
        facts=FACTS,
        limits=limits,
        envelope_type=RunEnvelope[FlowState],
        requirements=requirements,
        schemas=search.reply_schemas,
    )
    return replace(hosting, **overrides)


@dataclass
class SerialExecutors(Executors):
    """Scripted answers for one run of a serial search, popped as requests are authorized.

    ``roles`` holds the replies of the n-th turn of each role, by its short name (``orchestrator``,
    ``implementer``, ``judge``, ``router``, ``profiler``). A role past its replies is answered with
    ``exhausted`` when that is given, else never.
    """

    roles: dict[str, deque[str | Answer]] = field(default_factory=dict)
    registry: Callable[[], object] | None = None
    prefix: str = ""

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix(self.prefix)
        queue = self.roles.setdefault(role, deque())
        if not queue and self.exhausted is None:
            return Unknown()
        reply = queue.popleft() if queue else self.exhausted
        assert reply is not None
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=lease(request.turn.session.session_id))

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        assert self.registry is not None
        typed = self.registry().decode(request.operation)  # ty: ignore[unresolved-attribute]
        outcome: BaseModel
        match typed:
            case InterpretEvidenceBase():
                outcome = typed.outcome_model(
                    status="succeeded",
                    readings=tuple(self.readings[item.evidence_id] for item in typed.evidence),
                )
            case CaptureCommandBase():
                outcome = CommandCapture(status="succeeded", exit_code=0, output="")
            case ValidateLocalBase():
                outcome = LocalValidationReport(status="succeeded", passed=True)
            case VerifyParentRevisionBase():
                outcome = ParentVerification(status="succeeded", verified=True)
            case _:
                assert isinstance(typed, RenderRoleArtifactsBase)
                outcome = RenderedArtifacts(
                    status="succeeded",
                    prompts=(
                        ArtifactRef(
                            artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                            digest="prompt",
                        ),
                    ),
                )
        return Succeeded(outcome=outcome)


__all__ = ["FACTS", "TERMS", "SerialExecutors", "harness_of"]
