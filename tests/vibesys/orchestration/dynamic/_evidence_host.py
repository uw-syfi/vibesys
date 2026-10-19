"""A `Host` whose evaluator says what failed and why, for stories about measured evidence.

The ordinary `Host` decodes every failed check with no text and fails only the input's benchmark.
These stories need the evaluator's own words (what an input or a crashed candidate printed) and a
task whose accuracy check a candidate can fail, so this subclass changes only what the decoded
readings carry. Nothing about how the flow is driven differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Unpack

from tests.vibesys.orchestration.dynamic._host import FACTS, PLANNER_LABEL, Host, World
from tests.vibesys.orchestration.dynamic._replies import implemented, plan_reply, reviewed
from tests.vibesys.vibesys_flows import start_flow

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import dynamic
from vs_core.api import EvidenceKind, ObservationStatus, Retained, RevisionRef, StartAttempt

if TYPE_CHECKING:
    from vibesys.metrics import EvidenceReading, PartialRow
    from vibesys.orchestration.dynamic.search.api import DynamicConfig
    from vs_core.api import EvidenceRef, RunFacts


@dataclass
class EvidenceHost(Host):
    """The host of a dynamic flow whose failed benchmarks print ``crash_text``."""

    crash_text: str = ""
    """What a failed check printed; the decoded reading carries it as its feedback.

    A failed benchmark carries it, and so does the input's own failed accuracy check."""
    candidates_crash: bool = False
    """Whether every candidate's benchmark fails (the input's is set by ``baseline``)."""
    candidates_fail_accuracy: bool = False
    """Whether every candidate fails the task's accuracy check."""
    partial: PartialRow | None = None
    """What a failed benchmark measured before it stopped, as its reading reports it."""

    def _status_of(self, stage: str, *, is_input: bool) -> ObservationStatus:
        if not is_input and self.candidates_fail_accuracy and stage == "accuracy":
            return ObservationStatus.FAILED
        if not is_input and self.candidates_crash and stage == "benchmark":
            return ObservationStatus.FAILED
        return super()._status_of(stage, is_input=is_input)

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        reading = super()._reading_of(ref)
        if ref.kind is EvidenceKind.BENCHMARK and not reading.passed:
            return reading.model_copy(update={"feedback": self.crash_text, "partial": self.partial})
        if (
            ref.kind is EvidenceKind.CORRECTNESS
            and ref.purpose == "baseline"
            and not reading.passed
        ):
            return reading.model_copy(update={"feedback": self.crash_text})
        return reading


class EvidenceWorld(World, total=False):
    """How the far side differs: everything of `World`, and what the failed checks print."""

    crash_text: str
    candidates_crash: bool
    candidates_fail_accuracy: bool
    partial: PartialRow | None


def begin_with_evidence(
    cfg: DynamicConfig,
    *,
    restarting: bool,
    facts: RunFacts = FACTS,
    **world: Unpack[EvidenceWorld],
) -> EvidenceHost:
    """Start the dynamic search of ``cfg`` under an `EvidenceHost` over a task with ``facts``.

    Like `begin`, the host answers the input's measurement first, so a story starts from a
    measured input with the first planner turn open.
    """
    run = start_flow(dynamic, cfg, facts=facts, restarting=restarting)
    host = EvidenceHost(run, cfg, **world)
    host.measure_input()
    return host


def plan(host: Host, *entries: dict[str, object]) -> None:
    """The planner answers its open turn with ``entries``."""
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*entries))


def finish_workstream(
    host: Host, hypothesis: str, candidate: str, *, outcome: str = "supported"
) -> None:
    """Admit the workstream of ``hypothesis``; its implementer leaves ``candidate`` and the judge passes it."""
    for asked in host.run.open:
        request = asked.request
        if isinstance(request, StartAttempt) and f":{hypothesis}:" in request.attempt_id.root:
            host.answer(asked)
            break
    else:
        message = f"no start for {hypothesis}: {host.run.timeline()}"
        raise AssertionError(message)
    result = ImplementerResult.model_validate_json(implemented(outcome))
    implementing = next(
        item
        for item in host.run.open
        if item.label == "implementer"
        and getattr(item.context, "hypothesis_id", None) == hypothesis
    )
    host.run.reply(
        implementing, result, checkpoint=Retained(revision=RevisionRef.of_git_commit(candidate))
    )
    judging = next(
        item
        for item in host.run.open
        if item.label == "judge" and getattr(item.context, "hypothesis_id", None) == hypothesis
    )
    host.run.reply(judging, ReviewResult.model_validate_json(reviewed(passed=True)))
