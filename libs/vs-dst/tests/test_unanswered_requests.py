"""A world that gives a request no final answer is asked within the retry bound, then the run halts.

An executor that cannot conclude (an answer that is not final) leaves its step with no result: the
attempt raised, it is charged as a crash is, and the step runs again until its
``request_retries`` are spent. Nothing it left is sealed, so the next attempt asks the world
afresh. The run then ends or halts on a typed stop: it never waits on the world, and never asks it
forever.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_core.api import ResourceId
from vs_core.api.testing import Running, Unknown
from vs_dst.api import StepLoopHosting, simulate

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer

KINDS = (
    "ensure_workspace",
    "snapshot_and_retain",
    "retain_revision",
    "close_attempt_scope",
    "discard_workspace",
)
"""The kinds of request the tiny retaining search makes."""

NOT_FINAL = (Unknown(), Running(resource_id=ResourceId(root="job:unanswered")))
"""Answers that leave a request neither succeeded nor failed."""


class Unanswered(TinyWorld):
    """A tiny retaining world that answers every request of one kind with an answer that is not final."""

    def __init__(self, kind: str, answer: Answer, items: int) -> None:
        super().__init__(items, retain=True)
        self.kind = kind
        self.answer = answer
        self.asked_of_kind: Counter[str] = Counter()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        assert request.request_id is not None
        if request.kind == self.kind:
            self.asked_of_kind[request.request_id.root] += 1
            return self.answer
        return super().__call__(request, core)


@pytest.mark.regression
@settings(deadline=None, max_examples=5, derandomize=True, database=None)
@given(
    kind=st.sampled_from(KINDS),
    answer=st.sampled_from(NOT_FINAL),
    items=st.integers(min_value=1, max_value=2),
)
@example(kind="ensure_workspace", answer=NOT_FINAL[0], items=1)
def test_a_request_the_world_never_answers_finally_is_asked_within_the_retry_bound_and_the_run_halts(
    kind: str, answer: Answer, items: int
) -> None:
    world = Unanswered(kind, answer, items)
    retries = world.setup.harness.limits.request_retries.limit

    run = simulate(world, StepLoopHosting())

    assert run.finished or run.halted is not None
    assert max(world.asked_of_kind.values()) == 1 + retries
