"""The dynamic search's own knobs: how wide, how long, how often to review, when to give up.

Everything else a run needs belongs to someone else. How candidates are measured and compared is
the task's (`ctx.task`); how an agent is asked is its role's (`_roles`); how a backend retries
its infrastructure is the backend's. A config is an input of the search, never persisted state:
running the search again with the same config and the same events reproduces every decision.
"""

from typing import Annotated, Self

from pydantic import Field, model_validator

from vibesys.orchestration.dynamic.search._queue import PickPolicy
from vibesys.orchestration.dynamic.search._steps import Outline, PlanMode
from vs_core.api.search import Value

type Positive = Annotated[int, Field(gt=0, le=1000)]
type NonNegative = Annotated[int, Field(ge=0, le=1000)]
MAX_WIDTH = 32
"""The most workstreams, or trusted evaluations, a run holds at once."""
type Width = Annotated[int, Field(gt=0, le=MAX_WIDTH)]
type Wait = Annotated[float, Field(gt=0, le=7 * 24 * 3600)]


class DynamicConfig(Value):
    """Budget, width, review cadence and give-up rules of one dynamic run."""

    max_rounds: Positive
    """Rounds of agent work per slot: the run schedules at most ``max_rounds * max_in_flight``."""
    max_in_flight: Width = 2
    """Workstreams whose agents work at once. A workstream whose candidate waits for its trusted
    measurement no longer counts: its slot goes to new work."""
    evaluation_slots: Width | None = None
    """Trusted measurements the run holds at once, the input's included. A candidate is chosen
    for a slot only when the slot is free (see `pick`). None: as many as the run environment's
    evaluation executor admits at once, which the launch fills in (`DynamicLaunch.search_config`);
    1 when no launch did. More slots than the executor admits would dispatch a measurement that
    waits in the executor's queue while its deadline runs."""
    max_backlog: Annotated[int, Field(ge=0, le=MAX_WIDTH)] | None = None
    """Workstreams whose candidate waits for, or holds, an evaluation slot without holding one of
    the ``max_in_flight`` agent slots, so new work starts while measurements wait. Past it, a
    waiting workstream keeps its agent slot, so no new work starts until a measurement ends.
    None, the default: every waiting workstream parks, up to `MAX_WIDTH` (see `backlog`). 0 keeps
    every agent slot held until its workstream ends, so the run serializes on the measurements."""
    pick: PickPolicy = "newest"
    """Which waiting candidate a free evaluation slot measures: ``newest`` (the latest submitted),
    or ``ranked`` (the orchestrator's order, then the newest)."""
    input_wait_seconds: Wait | None = 3600.0
    """How long a workstream waits for the input's measurement before the orchestrator is told,
    on its next turn, that the input is overdue; None: never told."""
    judge_every: Positive = 1
    """A candidate is reviewed by the judge every this many rounds."""
    max_unmeasured_turns: Width = 2
    """Turns of one workstream that may end without a candidate reaching measurement."""
    max_continuation_rounds: NonNegative = 2
    """Times one hypothesis may be continued after its first round."""
    profiling: bool = False
    """Whether the planner may schedule profile workstreams. Launch fails without the capability."""
    mode: PlanMode = "explore"
    """``explore``: the planner proposes hypotheses. ``follow_plan``: the run implements the
    task's plan step by step, and the planner is asked only to repair a failed step and, once
    every step is done, to explore from there."""
    plan: Outline | None = None
    """The task's plan (its ``[plan]`` table); read only when ``mode`` is ``follow_plan``."""

    @model_validator(mode="after")
    def _a_plan_to_follow(self) -> Self:
        if self.mode == "follow_plan" and self.plan is None:
            message = (
                "mode 'follow_plan' needs the task's plan, and the task declares none: add a "
                "[plan] table to its vibesys.input.toml, or launch with mode 'explore'"
            )
            raise ValueError(message)
        return self

    @property
    def follows_plan(self) -> bool:
        """Whether the run follows the task's plan."""
        return self.mode == "follow_plan"

    @property
    def slots(self) -> int:
        """Trusted measurements the run holds at once."""
        return self.evaluation_slots or 1

    @property
    def backlog(self) -> int:
        """Workstreams that may park on their trusted measurement at once."""
        return MAX_WIDTH if self.max_backlog is None else self.max_backlog

    @property
    def start_budget(self) -> int:
        """Workstreams the run may schedule: every one is one round of agent work."""
        return self.max_rounds * self.max_in_flight
