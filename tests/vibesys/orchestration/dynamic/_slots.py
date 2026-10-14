"""Helpers that play one workstream's slot of the dynamic flow by name, for stories about plans.

They answer the waits of a workstream whose hypothesis the story names: its workspace start, its
implementer turn and its judge turn. Identity is read from the typed requests and contexts, never
from the text of an id, so any canonical hypothesis id can be played.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host
from tests.vibesys.orchestration.dynamic._replies import implemented, reviewed

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import PlannerCorrection, PlannerPrompt
from vs_core.api import Operation, Retained, RevisionRef, StartAttempt

if TYPE_CHECKING:
    from vs_core.api.testing import Asked


def open_next_slot(host: Host) -> Asked:
    """Admit the one workstream whose workspace start is open (core opens its workspace)."""
    (asked,) = host.open(StartAttempt)
    host.answer(asked)
    return asked


def turn_about(host: Host, role: str, hypothesis: str) -> Asked:
    """The open turn of ``role`` that is about ``hypothesis``."""
    for asked in host.run.open:
        if asked.label == role and getattr(asked.context, "hypothesis_id", None) == hypothesis:
            return asked
    message = f"no {role} turn for {hypothesis!r}: {host.run.timeline()}"
    raise AssertionError(message)


def implement_and_judge(
    host: Host,
    hypothesis: str,
    candidate: str,
    *,
    outcome: str = "supported",
    summary: str = "done",
) -> None:
    """The implementer of ``hypothesis`` submits ``candidate`` and the judge accepts it."""
    result = ImplementerResult.model_validate_json(implemented(outcome, summary))
    checkpoint = Retained(revision=RevisionRef.of_git_commit(candidate))
    host.run.reply(turn_about(host, "implementer", hypothesis), result, checkpoint=checkpoint)
    verdict = ReviewResult.model_validate_json(reviewed())
    host.run.reply(turn_about(host, "judge", hypothesis), verdict)


def verify_parents(host: Host) -> None:
    """Core checks that each offered parent revision still reproduces; the world answers."""
    for asked in host.open(Operation):
        host.answer(asked)


def planner_context(host: Host) -> PlannerPrompt | PlannerCorrection:
    """What the open planner turn was asked."""
    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt | PlannerCorrection)
    return context
