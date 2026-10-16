"""``ctx.evaluation``: measure a revision, decode its evidence, read what agents measured.

A measurement is a request core owns end to end: it is submitted, polled and collected within
the bound the caller states, and its evidence is recorded in core's ledger. What the ledger
holds are identities; ``readings`` asks the search's evidence owner to decode them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from vs_core._registry import ContractError
from vs_core._strategy_operations import (
    CommandCapture,
    EvidenceReadingsBase,
    LocalValidationReport,
)
from vs_core.types.common import THIS_RUN, DecisionId, RevisionRef, Value
from vs_core.types.evaluation import EvidenceRef, MeasurementPlan, MeasurementResult
from vs_core.types.search import MeasurementFinished, OperationFinished
from vs_core.types.strategy import Measure, Operation
from vs_core.types.until import Failed, Within

if TYPE_CHECKING:
    from vs_core.types.common import AttemptRef, Scope, Seconds

    from ._kit import Kit
    from ._labels import Label
    from ._workspaces import Workspace

_REFS = TypeAdapter(tuple[EvidenceRef, ...])


class CaptureSpec(Value):
    """A command to run at a revision and the file it prints its result to."""

    revision: RevisionRef
    argv: tuple[str, ...]
    output_argument: str
    """The argument the runtime appends, followed by the temporary path the command writes."""
    timeout_seconds: int


def _within(within: Seconds | Within) -> Within:
    return within if isinstance(within, Within) else Within(limit=within)


class Evaluation:
    """The evaluation aspect of ``ctx``."""

    def __init__(self, kit: Kit) -> None:
        """Bind the aspect to its flow."""
        self._kit = kit

    async def measure(
        self,
        plan: MeasurementPlan,
        *,
        workspace: Workspace | None = None,
        within: Seconds | Within,
        label: Label | None = None,
    ) -> MeasurementResult | Failed:
        """Measure ``plan`` (in the workspace's scope, else the run's): its result, or the failure.

        ``label`` labels the measurement's span.
        """
        scope = THIS_RUN if workspace is None else workspace.scope
        request = Measure(decision_id=DecisionId(root="measure"), scope=scope, plan=plan)
        match await self._kit.wait(request, _within(within), label):
            case MeasurementFinished(result=result):
                return result
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"a measurement ended with {unexpected.kind}"
                raise ContractError(("evaluation", "measure"), message)

    async def readings(
        self,
        refs: tuple[EvidenceRef, ...],
        *,
        workspace: Workspace | None = None,
        within: Seconds | Within,
    ) -> EvidenceReadingsBase[Any] | None | Failed:
        """What the evidence owner decoded of ``refs``; None when it returned no readings."""
        operations = self._kit.run.wiring.operations
        if operations is None:
            message = "this search was registered without operations"
            raise ContractError(("evaluation", "readings"), message)
        scope: Scope = THIS_RUN if workspace is None else workspace.scope
        request = Operation(
            decision_id=DecisionId(root="interpret"),
            scope=scope,
            request=operations.interpret(evidence=refs),  # ty: ignore[missing-argument]
        )
        match await self._kit.wait(request, _within(within)):
            case OperationFinished(result=finished):
                outcome = finished.outcome
                return outcome if isinstance(outcome, EvidenceReadingsBase) else None
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"an operation ended with {unexpected.kind}"
                raise ContractError(("evaluation", "readings"), message)

    async def validate_local(
        self,
        revision: RevisionRef,
        recipe_artifact: str,
        *,
        workspace: Workspace,
        within: Seconds | Within,
    ) -> LocalValidationReport | None | Failed:
        """The trusted run of the validation recipes an agent wrote, against ``revision``.

        ``recipe_artifact`` is the workspace-relative path of the recipe file the agent
        reported. The result is None when the owner returned another outcome; a search checks
        ``ctx.capabilities`` for the operation first.
        """
        operations = self._kit.run.wiring.operations
        if operations is None or operations.validate_local is None:
            message = "this search declares no validate_local operation"
            raise ContractError(("evaluation", "validate_local"), message)
        request = Operation(
            decision_id=DecisionId(root="validate"),
            scope=workspace.scope,
            request=operations.validate_local(  # ty: ignore[missing-argument]
                revision=revision, recipe_artifact=recipe_artifact
            ),
        )
        match await self._kit.wait(request, _within(within)):
            case OperationFinished(result=finished):
                outcome = finished.outcome
                return outcome if isinstance(outcome, LocalValidationReport) else None
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"an operation ended with {unexpected.kind}"
                raise ContractError(("evaluation", "validate_local"), message)

    async def capture(
        self, command: CaptureSpec, *, within: Seconds | Within
    ) -> CommandCapture | None | Failed:
        """What the task's attribution command prints when run at a revision.

        The command runs in a throwaway workspace holding exactly the revision; nothing durable
        changes. The result is None when the owner returned another outcome.
        """
        operations = self._kit.run.wiring.operations
        if operations is None or operations.capture is None:
            message = "this search declares no capture_command operation"
            raise ContractError(("evaluation", "capture"), message)
        request = Operation(
            decision_id=DecisionId(root="capture"),
            scope=THIS_RUN,
            request=operations.capture(  # ty: ignore[missing-argument]
                revision=command.revision,
                argv=command.argv,
                output_argument=command.output_argument,
                timeout_seconds=command.timeout_seconds,
            ),
        )
        match await self._kit.wait(request, _within(within)):
            case OperationFinished(result=finished):
                outcome = finished.outcome
                return outcome if isinstance(outcome, CommandCapture) else None
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"an operation ended with {unexpected.kind}"
                raise ContractError(("evaluation", "capture"), message)

    def taken(self, workspace: Workspace, revision: RevisionRef) -> tuple[EvidenceRef, ...]:
        """The trusted evaluations the workspace's agents took of ``revision`` themselves."""
        return self._kit.read(
            lambda view: _taken(view.measurements, workspace.ref, revision), _REFS
        )


def _taken(
    measurements: tuple[EvidenceRef, ...], attempt: AttemptRef, revision: RevisionRef
) -> tuple[EvidenceRef, ...]:
    held = (
        item
        for item in measurements
        if item.provenance == "trusted"
        and item.purpose == "local-validation"
        and item.candidate == revision
        and item.scope.owner == attempt.attempt_id
        and item.scope.generation == attempt.generation
    )
    return tuple(sorted(held, key=lambda item: item.observation_sequence))
