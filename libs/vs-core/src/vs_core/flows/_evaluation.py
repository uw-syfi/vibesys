"""``ctx.evaluation``: measure a revision, decode its evidence, read what agents measured.

A measurement is a request core owns end to end: it is submitted, polled and collected within
the bound the caller states, and its evidence is recorded in core's ledger. What the ledger
holds are identities; ``readings`` asks the search's evidence owner to decode them.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from vs_core._comparison import SETTLE_RUNS
from vs_core._registry import ContractError
from vs_core._strategy_operations import (
    CommandCapture,
    EvidenceReadingsBase,
    LocalValidationReport,
)
from vs_core.types.common import THIS_RUN, DecisionId, ObservationStatus, RevisionRef, Value
from vs_core.types.evaluation import EvidenceRef, MeasurementPlan, MeasurementResult
from vs_core.types.measurement import Comparison, ComparisonSettled, StatedComparison
from vs_core.types.search import MeasurementFinished, OperationFinished
from vs_core.types.strategy import Measure, Operation
from vs_core.types.until import Failed, Within

from ._sources import First

if TYPE_CHECKING:
    from vs_core.types.common import AttemptRef, Scope, Seconds
    from vs_core.types.search import RunView

    from ._kit import Kit
    from ._labels import Label
    from ._workspaces import Workspace

_REFS = TypeAdapter(tuple[EvidenceRef, ...])
_REVISION = TypeAdapter(RevisionRef | None)
_STATED = TypeAdapter(StatedComparison | None)


class CaptureSpec(Value):
    """A command to run at a revision and the file it prints its result to."""

    revision: RevisionRef
    argv: tuple[str, ...]
    output_argument: str
    """The argument the runtime appends, followed by the temporary path the command writes."""
    timeout_seconds: int


class Compared(Value):
    """A finished measurement and its candidate's comparison with the base, when it has one."""

    result: MeasurementResult
    comparison: Comparison | None = None


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

        ``label`` labels the measurement's span. The candidate is not compared with its base:
        `measure_compared` is for a search that reads the comparison.
        """
        found = await self._measure(plan, workspace, within, label, compared=False)
        return found if isinstance(found, Failed) else found.result

    async def measure_compared(
        self,
        plan: MeasurementPlan,
        *,
        workspace: Workspace | None = None,
        within: Seconds | Within,
        label: Label | None = None,
    ) -> Compared | Failed:
        """Measure ``plan`` as `measure` does, with the candidate's comparison with its base.

        The comparison is with the base of the workspace's attempt, over every sample of the
        candidate's measured input, including runs the agent asked for; None when it has none.
        A close call earns core more runs of either side (`vs_core._comparison`), and they land
        after this measurement ends, so the comparison is awaited until no run that feeds it is
        under way, at most ``SETTLE_RUNS`` times the measurement's limit: a verdict taken on it
        rests on the runs a close call earns, never on the first one. Those runs restate the
        official record's headline (`vs_core._comparison.restated_answers`) until core closes
        it, when the comparison settles or at the bound core states for it
        (``StatedComparison.answer_until``), which is when this flow stops waiting. The
        result's records are handed back as the ledger states them then, and the ledger keeps
        that figure: the round's headline and the record cannot part.
        """
        found = await self._measure(plan, workspace, within, label, compared=True)
        if isinstance(found, Failed) or found.comparison is None:
            return found
        settled = await self._settled(found.comparison, _within(within).limit * SETTLE_RUNS)
        evidence = self._kit.read(partial(_as_ledger_states, held=found.result.evidence), _REFS)
        result = found.result.model_copy(update={"evidence": evidence})
        return Compared(result=result, comparison=settled)

    async def _measure(
        self,
        plan: MeasurementPlan,
        workspace: Workspace | None,
        within: Seconds | Within,
        label: Label | None,
        *,
        compared: bool,
    ) -> Compared | Failed:
        scope = THIS_RUN if workspace is None else workspace.scope
        request = Measure(
            decision_id=DecisionId(root="measure"), scope=scope, plan=plan, compared=compared
        )
        match await self._kit.wait(request, _within(within), label):
            case MeasurementFinished(result=result, comparison=comparison):
                return Compared(result=result, comparison=comparison)
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"a measurement ended with {unexpected.kind}"
                raise ContractError(("evaluation", "measure"), message)

    async def _settled(self, comparison: Comparison, bound: Seconds) -> Comparison:
        """``comparison`` as it stands once core's runs for it have ended, or when ``bound`` is spent.

        The run's view holds the comparison of each candidate input and whether runs that feed it
        are still under way. Until they have ended the flow waits for core to report that they
        have (``ComparisonSettled``), and reads the view once more then. A report that never
        comes is bounded by the moment core stops restating the official record
        (``StatedComparison.answer_until``), so the flow reads the record once it no longer
        moves; ``bound`` from now only when no official record of it is held. A comparison that
        went from the view (its base samples went stale) is the last one seen. The wait journals
        no step of its own while it lasts.
        """
        subject = (comparison.scope, comparison.candidate_input, comparison.base_input)
        settled = self._kit.signal(
            f"settling of {comparison.candidate_input}",
            lambda event: (
                isinstance(event, ComparisonSettled)
                and (event.scope, event.candidate_input, event.base_input) == subject
            ),
        )
        try:
            stated = self._kit.read(partial(_stated, comparison=comparison), _STATED)
            if stated is None or not stated.settling:
                return comparison if stated is None else stated.comparison
            seen = stated.comparison
            until = stated.answer_until
            await First(
                (settled, self._kit.timer(self._kit.now() + bound if until is None else until))
            )
        finally:
            self._kit.run.release(settled)
        stated = self._kit.read(partial(_stated, comparison=seen), _STATED)
        return seen if stated is None else stated.comparison

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

    def last_evaluated(self, workspace: Workspace) -> RevisionRef | None:
        """The revision of the workspace's newest conclusive trusted evaluation, if any.

        It is where an agent's own evaluations left off, whether or not the agent ever reported
        a checkpoint of it, so a flow can ask the trusted gates about work the agent measured
        and then abandoned (`taken` lists the evaluations of one revision).
        """
        return self._kit.read(
            lambda view: _last_evaluated(view.measurements, workspace.ref), _REVISION
        )


def _attempt_evaluations(
    measurements: tuple[EvidenceRef, ...], attempt: AttemptRef
) -> tuple[EvidenceRef, ...]:
    """The trusted evaluations the attempt's agents took of any revision, oldest first."""
    held = (
        item
        for item in measurements
        if item.provenance == "trusted"
        and item.purpose == "local-validation"
        and item.scope.owner == attempt.attempt_id
        and item.scope.generation == attempt.generation
    )
    return tuple(sorted(held, key=lambda item: item.observation_sequence))


def _taken(
    measurements: tuple[EvidenceRef, ...], attempt: AttemptRef, revision: RevisionRef
) -> tuple[EvidenceRef, ...]:
    return tuple(
        item for item in _attempt_evaluations(measurements, attempt) if item.candidate == revision
    )


def _last_evaluated(
    measurements: tuple[EvidenceRef, ...], attempt: AttemptRef
) -> RevisionRef | None:
    conclusive = tuple(
        item
        for item in _attempt_evaluations(measurements, attempt)
        if item.status == ObservationStatus.SUCCEEDED
    )
    return conclusive[-1].candidate if conclusive else None


def _as_ledger_states(view: RunView, held: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    """``held`` as the ledger states each record now (a record not in it is kept as it is)."""
    ledger = {item.key: item for item in view.measurements}
    return tuple(ledger.get(item.key, item) for item in held)


def _stated(view: RunView, comparison: Comparison) -> StatedComparison | None:
    """The view's current comparison of the same candidate input against the same base input."""
    return next(
        (
            held
            for held in view.comparisons
            if (held.comparison.scope, held.comparison.candidate_input, held.comparison.base_input)
            == (comparison.scope, comparison.candidate_input, comparison.base_input)
        ),
        None,
    )
