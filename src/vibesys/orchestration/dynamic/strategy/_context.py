"""Budget arithmetic and prompt data derived from state and the run view.

Everything here is a pure function of `DynamicStrategyState`, `RunView` and
`DynamicConfig`. Prompt contexts are typed values; the strategy never writes
prompt text.
"""

import hashlib
import re

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._parents import ParentOption, options
from vibesys.orchestration.dynamic.strategy._prompts import (
    BuildableRow,
    EvidenceCitation,
    HistoryRow,
    ImplementPrompt,
    PlannerPrompt,
    ProfilePrompt,
    ReviewPrompt,
)
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    BaselineStage,
    BlockerKind,
    DynamicStrategyState,
    HypothesisRecord,
    RoundRecord,
    WorkPhase,
)
from vs_core.api import RunView

_HISTORY_ROWS = 20
_HISTORY_TITLE_CHARS = 200
_HISTORY_SUMMARY_CHARS = 600
_CUT = " [cut]"
_HISTORY_FAILURE_CHARS = 1200


def remaining(state: DynamicStrategyState, config: DynamicConfig) -> int:
    """Workstreams the run may still schedule; a refund returns its slot to the budget."""
    return max(config.start_budget - len(state.attempts) + state.refunded, 0)


def active(state: DynamicStrategyState) -> tuple[AttemptRecord, ...]:
    """Workstreams that have not finished."""
    return tuple(item for item in state.attempts if item.phase is not WorkPhase.DONE)


def capacity(state: DynamicStrategyState, config: DynamicConfig) -> int:
    """Free slots the next plan may fill, bounded by the remaining budget."""
    return max(min(config.max_in_flight - len(active(state)), remaining(state, config)), 0)


def baseline_resolved(state: DynamicStrategyState) -> bool:
    """Whether the input measurement has reached a terminal stage."""
    return state.baseline.stage in {
        BaselineStage.MEASURED,
        BaselineStage.UNMEASURABLE,
        BaselineStage.NOT_CONFIGURED,
    }


def input_trusted(state: DynamicStrategyState) -> bool:
    """Whether the input revision has a trusted reading, or no gate is configured to give one."""
    return (
        state.baseline.stage in {BaselineStage.MEASURED, BaselineStage.NOT_CONFIGURED}
        and state.baseline.accuracy_passed is not False
    )


def offered(state: DynamicStrategyState, view: RunView) -> tuple[ParentOption, ...]:
    """Parents the planner may name: provable by core and not withheld."""
    return tuple(
        item
        for item in options(state.parents, view)
        if item.snapshot.revision.revision_id.root not in state.withheld
    )


def offer_snapshot(state: DynamicStrategyState, view: RunView) -> str:
    """Name the immutable offer so a reply cannot cite a parent that was not shown."""
    facts = sorted(item.option_id for item in offered(state, view))
    digest = hashlib.sha256("\n".join((view.facts.baseline.digest, *facts)).encode())
    return digest.hexdigest()


def _status(record: HypothesisRecord, running: frozenset[str]) -> str:
    if record.hypothesis_id in running:
        return "running"
    return record.strategy.value


def _bounded(text: str, limit: int) -> str:
    """Cut free agent text to ``limit`` characters for the planner's compact history.

    The record keeps the whole text; only this rendered view is bounded, and a cut is
    marked so the planner does not read a prefix as the whole.
    """
    return text if len(text) <= limit else f"{text[:limit]}{_CUT}"


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of a failure's output, where its cause usually is."""
    return text if len(text) <= limit else f"{_CUT.strip()} {text[-limit:]}"


def _fence(text: str) -> str:
    """A Markdown code fence that no line of ``text`` can close.

    It is longer than the longest run of backticks in the text, and at least three.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _history_row(record: HypothesisRecord, running: frozenset[str]) -> HistoryRow:
    last = record.rounds[-1] if record.rounds else None
    tail = "" if last is None else _tail(last.failure_tail, _HISTORY_FAILURE_CHARS)
    return HistoryRow(
        hypothesis_id=record.hypothesis_id,
        sequence=record.first_sequence,
        title=_bounded(record.title, _HISTORY_TITLE_CHARS),
        status=_status(record, running),
        strategy=record.strategy.value,
        outcome=None if last is None or last.outcome is None else last.outcome.value,
        summary="" if last is None else _bounded(last.summary, _HISTORY_SUMMARY_CHARS),
        review_passed=None if last is None else last.review_passed,
        accuracy_passed=None if last is None else last.accuracy_passed,
        benchmark_passed=None if last is None else last.benchmark_passed,
        candidate=None if last is None else last.candidate,
        metrics=() if last is None else last.metrics,
        partial=None if last is None else last.partial,
        failure_tail=tail,
        failure_fence=_fence(tail),
        failure=_unexplained(last),
    )


def _unexplained(last: RoundRecord | None) -> str:
    """The reason a round ended, unless its failure tail already says it."""
    if last is None or not last.failure or last.failure == last.failure_tail:
        return ""
    return _bounded(last.failure, _HISTORY_SUMMARY_CHARS)


def _buildable(item: ParentOption) -> BuildableRow:
    snapshot = item.snapshot
    benchmark = snapshot.benchmark
    return BuildableRow(
        option_id=item.option_id,
        hypothesis_id=snapshot.hypothesis_id,
        revision=snapshot.revision,
        accuracy_evidence=snapshot.accuracy.evidence_id,
        benchmark_evidence=None if benchmark is None else benchmark.evidence_id,
        submission_index=snapshot.submission_index,
        latest_verified=item.latest_verified,
        best_partial=item.best_partial,
        metric=None if benchmark is None else benchmark.headline(),
        partial=None if benchmark is None else benchmark.partial,
    )


def planner_prompt(
    state: DynamicStrategyState, config: DynamicConfig, view: RunView
) -> PlannerPrompt:
    """The free-slot planning request: budget, offered parents and compact history."""
    running = frozenset(item.plan.work_id for item in active(state))
    rows = tuple(_history_row(item, running) for item in state.hypotheses)
    shown = rows[-_HISTORY_ROWS:]
    return PlannerPrompt(
        capacity=capacity(state, config),
        in_flight=len(running),
        remaining=remaining(state, config),
        base_revision=view.facts.baseline,
        base_accuracy_passed=state.baseline.accuracy_passed,
        offer_snapshot=offer_snapshot(state, view),
        buildable=tuple(_buildable(item) for item in offered(state, view)),
        history=shown,
        older_ids=tuple(item.hypothesis_id for item in rows[:-_HISTORY_ROWS]),
        baseline=state.baseline.metrics,
        input_failure=state.baseline.failure,
        profiling=config.profiling,
    )


def implement_prompt(record: AttemptRecord, state: DynamicStrategyState) -> ImplementPrompt:
    """One isolated implementation request for a scheduled workstream."""
    prior = next(
        (item for item in state.hypotheses if item.hypothesis_id == record.plan.work_id), None
    )
    last = prior.rounds[-1] if prior is not None and prior.rounds else None
    failed = next(
        (item for item in reversed(record.blockers) if item.kind is BlockerKind.FAILED), None
    )
    tail = "" if last is None else _tail(last.failure_tail, _HISTORY_FAILURE_CHARS)
    return ImplementPrompt(
        hypothesis_id=record.plan.work_id,
        hypothesis=record.plan.hypothesis,
        task=record.plan.task,
        pass_criteria=record.plan.pass_criteria,
        parent_revision=record.parent,
        evidence=tuple(EvidenceCitation(location=item) for item in record.plan.evidence),
        worktree_revision=None if last is None else last.candidate,
        prior_revision=None if last is None else last.candidate,
        feedback=record.feedback,
        failure_tail=tail,
        failure_fence=_fence(tail),
        failure=_unexplained(last),
    )


def review_prompt(record: AttemptRecord) -> ReviewPrompt:
    """Independent review of the exact candidate the implementer retained."""
    if record.candidate is None:
        message = "review requires a retained candidate"
        raise ValueError(message)
    return ReviewPrompt(
        hypothesis_id=record.plan.work_id,
        hypothesis=record.plan.hypothesis,
        pass_criteria=record.plan.pass_criteria,
        candidate=record.candidate,
        summary=record.summary,
        evidence=tuple(EvidenceCitation(location=item) for item in record.plan.evidence),
    )


def profile_prompt(record: AttemptRecord) -> ProfilePrompt:
    """Measurement-only request against the frozen target revision."""
    return ProfilePrompt(
        question=record.plan.question,
        required_fields=record.plan.required_fields,
        target=record.parent,
    )
