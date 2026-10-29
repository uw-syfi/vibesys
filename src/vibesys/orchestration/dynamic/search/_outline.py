"""The outline call: the planner's first turn in a run that follows a plan declaring no steps.

The task's ``[plan]`` table may name only its design documents. The run then asks for the steps
once, before anything is scheduled: the reply is a typed `PlanOutline`, checked by the table's
own rules, and becomes the run's outline (`RunRecord.planned`). Core corrects a reply whose steps
break a rule and asks again after a lost turn; a call that still ends without an outline leaves
the run with nothing to follow, so it ends planning for good.
"""

from vibesys.orchestration.dynamic.models import PlanOutline
from vibesys.orchestration.dynamic.search._prompts import OutlinePrompt
from vibesys.orchestration.dynamic.search._roles import OUTLINER
from vibesys.orchestration.dynamic.search._run_state import NoPlan, Outlined, RunRecord
from vibesys.orchestration.dynamic.search._values import PlanProblem, ProblemKind, Step
from vibesys.search_spans.api import turn_label
from vs_core.api.search import Failed, FailureReason, Flow, TurnFinished


async def outline(ctx: Flow, run: RunRecord) -> Outlined | NoPlan:
    """Ask for the steps of the plan run follows; what the reply outlines, or why there is none."""
    plan = run.config.plan
    if plan is None:
        message = "only a run that follows a plan outlines one"
        raise ValueError(message)
    outliner = ctx.agents.session(OUTLINER)
    prompt = OutlinePrompt(documents=plan.documents)
    match await outliner.turn(prompt, label=turn_label(outliner)):
        case TurnFinished(reply=PlanOutline() as given):
            steps = tuple(Step.model_validate(item.model_dump()) for item in given.steps)
            return Outlined(steps=steps, outliner=outliner.agent)
        case Failed(reason=FailureReason.INVALID_REPLY, errors=errors):
            problem = PlanProblem(kind=ProblemKind.UNPARSEABLE, errors=errors)
        case Failed() as failed:
            problem = PlanProblem(
                kind=ProblemKind.NO_REPLY, ended=failed.reason, turn_failure=failed.turn_failure
            )
        case TurnFinished() as unexpected:
            message = f"the outliner answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)
    return NoPlan(problem=problem, stuck=True, blocked_at_done=len(run.finished_work))
