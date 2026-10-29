"""A fake core for the multi search: the shared serial world plus its five agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Unpack

from tests.vibesys.serial_search._host import SerialHost, World
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
from vibesys.orchestration.multi.search.api import MultiConfig, multi
from vibesys.orchestration.profilers import ProfilerSummary
from vs_core.api import (
    Capabilities,
    CheckpointDecline,
    Declined,
    FailureReason,
    Retained,
    TurnFailureKind,
)
from vs_core.api.testing import FakeView

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

PLANNER = "orchestrator"
ROUTER = "router"
PROFILER = "profiler"
IMPLEMENTER = "implementer"
JUDGE = "judge"
RECIPE = "validation/recipes.json"
"""A validation recipe an implementer reply can name."""


def config(**overrides: object) -> MultiConfig:
    """A one-round config, overridden by the test."""
    return MultiConfig.model_validate({"max_rounds": 1, **overrides})


def implemented(outcome: str = "implemented", **extra: object) -> ImplementerResponse:
    """An implementer reply."""
    return ImplementerResponse.model_validate_json(
        json.dumps(
            {
                "summary": "did it",
                "expected_behavior": "faster",
                "status": outcome,
                **extra,
            }
        )
    )


def reviewed(*, passed: bool = True, **extra: object) -> JudgeResponse:
    """A judge reply."""
    return JudgeResponse.model_validate_json(
        json.dumps(
            {
                "analysis": "checked",
                "feedback": "" if passed else "fix it",
                "verdict": "pass" if passed else "fail",
                **extra,
            }
        )
    )


def routed(*, need_profile: bool = False, focus: str = "") -> PreRoundDecision:
    """The routing decision: whether fresh profile evidence is wanted."""
    return PreRoundDecision(need_profile=need_profile, profile_focus=focus, reasoning="because")


def profiled(**extra: object) -> ProfilerSummary:
    """The profiler's findings."""
    return ProfilerSummary.model_validate(
        {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it", **extra}
    )


@dataclass
class MultiHost(SerialHost):
    """The world around a multi flow."""

    designs: int = 0

    @classmethod
    def start(
        cls,
        selected: MultiConfig,
        *,
        restarting: bool,
        validating: bool = False,
        suspending: bool = False,
        **world: Unpack[World],
    ) -> MultiHost:
        """Start the multi search of ``selected`` under a host.

        ``validating`` offers the trusted run of validation recipes, which a core may not offer;
        ``suspending`` offers suspending a turn that waits for an evaluation.
        """
        view = None
        if validating or suspending:
            operations: tuple[object, ...] = ()
            if validating:
                assert multi.operations is not None
                operations = multi.operations.registry().descriptors
            offered = Capabilities(
                operations=operations,
                lifecycle=frozenset({"suspend"}) if suspending else frozenset(),
            )
            view = FakeView(changes={"capabilities": offered})
        return cls.begin(multi, selected, restarting=restarting, view=view, **world)

    def answer_agent(self, asked: Asked, variant: int) -> None:
        """Answer a turn of any agent; ``variant`` chooses how it goes."""
        match asked.label:
            case "orchestrator":
                self._planner(asked, variant)
            case "router":
                self._advisor(asked, variant, routed(need_profile=variant % 2 == 0))
            case "profiler":
                self._advisor(asked, variant, profiled())
            case "implementer":
                self._implementer(asked, variant)
            case _:
                self._judge(asked, variant)

    def _planner(self, asked: Asked, variant: int) -> None:
        match variant % 5:
            case 0 | 1:
                self.designs += 1
                self.run.reply_raw(asked, plan_reply(f"h{self.designs}"))
            case 2:
                self.run.reply_raw(asked, "not json")
            case 3:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                self.run.lose(asked)

    def _advisor(self, asked: Asked, variant: int, reply: object) -> None:
        match variant % 3:
            case 0:
                self.run.reply(asked, reply)  # ty: ignore[invalid-argument-type]
            case 1:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                self.run.lose(asked)

    def _implementer(self, asked: Asked, variant: int) -> None:
        match variant % 7:
            case 0 | 1:
                outcome = "implemented" if variant % 7 == 0 else "continue"
                reply = implemented(outcome, next_step="more" if outcome == "continue" else "")
                self.run.reply(asked, reply, checkpoint=Retained(revision=self.retained()))
            case 2:
                self.run.fail_turn(asked, TurnFinishedFailure)
            case 3:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case 4:
                self.run.reply(asked, implemented("blocked"))
            case 5:
                declined = Declined(reason=next(iter(CheckpointDecline)))
                self.run.reply(asked, implemented(), checkpoint=declined)
            case _:
                # Waiting for an evaluation: core records the wait, then resumes the turn.
                self.run.resume_ready(self.run.suspend(asked))

    def _judge(self, asked: Asked, variant: int) -> None:
        if variant % 3 == 2:
            self.run.fail_turn(asked, TurnFinishedFailure)
            return
        self.run.reply(asked, reviewed(passed=variant % 3 == 0))


TurnFinishedFailure = TurnFailureKind.PROVIDER_FAILED
