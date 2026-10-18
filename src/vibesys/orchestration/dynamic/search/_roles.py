"""The agents of a dynamic run, each declared once as a `Role`.

A role says everything about how its agent is asked: the template, the reply model, the
workspace and what the turn may write, how long a turn may take, how often core asks the agent
to correct a reply or asks again after a lost turn, and what a turn costs. Core holds none of
it. The host supplies what it alone can (system prompts, tools, capabilities: see
`vibesys.dynamic_roles`); the operator may change only a role's model (`agent.toml`).

The planner is asked again by its planning call itself, with a prompt built from the run as it
is then, so core's own corrections and re-asks are off for it.
"""

from vibesys.orchestration.dynamic.models import (
    ImplementerResult,
    MergeResult,
    PlanOutline,
    PortfolioPlan,
    ProfilerResult,
    ReviewResult,
)
from vibesys.orchestration.dynamic.search._values import AgentName
from vs_core.api.search import (
    Access,
    Backoff,
    Charge,
    Exchanges,
    Role,
    WorkspaceMode,
    hours,
    minutes,
)

LOST_TURN_BACKOFF = Backoff(first=5.0, cap=120.0)
"""A lost turn is asked again after this wait on the run clock, doubling per loss.

Asking at once would spend every ask inside one provider outage.
"""

EXCHANGES = Exchanges(
    correction="reply_correction.j2", resume="resume.j2", wait_unrecorded="wait_unrecorded.j2"
)
"""The templates of the exchanges core starts with an agent that may wait for evaluations."""

PLANNER = Role(
    name=AgentName.PLANNER.value,
    title="Planner",
    prompt="portfolio.j2",
    reply=PortfolioPlan,
    workspace=None,
    access=Access.READ_ONLY,
    turn_time=minutes(30),
    corrections=0,
    lost_turns=0,
    charge=Charge.TURN,
)
"""Plans the free slots of the run; works in no workspace."""

OUTLINER = Role(
    name=AgentName.OUTLINER.value,
    title="Outliner",
    prompt="outline.j2",
    reply=PlanOutline,
    workspace=None,
    access=Access.READ_ONLY,
    turn_time=minutes(30),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    exchanges=Exchanges(correction=EXCHANGES.correction),
)
"""Outlines the steps of the task's plan from its documents, when the task declares none.

Asked once, at the start of a run that follows the plan; core corrects a reply whose steps do
not form a plan, and asks again after a lost turn.
"""

IMPLEMENTER = Role(
    name=AgentName.IMPLEMENTER.value,
    title="Implementer",
    prompt="implement.j2",
    reply=ImplementerResult,
    workspace=WorkspaceMode.ISOLATED_CHILD,
    access=Access.WRITE_CANDIDATE,
    turn_time=hours(2),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.ATTEMPT,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
"""Implements a hypothesis in an isolated candidate workspace."""

JUDGE = Role(
    name=AgentName.JUDGE.value,
    title="Judge",
    prompt="review.j2",
    reply=ReviewResult,
    workspace=WorkspaceMode.READ_ONLY_REVISION,
    access=Access.READ_ONLY,
    turn_time=minutes(30),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
"""Reviews a candidate, reading it and the evaluations the implementer took of it."""

PROFILER = Role(
    name=AgentName.PROFILER.value,
    title="Profiler",
    prompt="profile_request.j2",
    reply=ProfilerResult,
    workspace=WorkspaceMode.READ_ONLY_REVISION,
    access=Access.WRITE_ARTIFACTS,
    turn_time=hours(1),
    corrections=0,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.ATTEMPT,
)
"""Looks at a revision to inform later plans; produces no candidate."""

CORRECTION = Exchanges(correction="reply_correction.j2")
"""The only exchange core starts with an agent that never waits: asking it to fix its reply."""

MERGER = Role(
    name=AgentName.MERGER.value,
    title="Merger",
    prompt="merge.j2",
    reply=MergeResult,
    workspace=WorkspaceMode.ISOLATED_CHILD,
    access=Access.WRITE_CANDIDATE,
    turn_time=hours(1),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.ATTEMPT,
    exchanges=CORRECTION,
)
"""Merges candidates into main on a checkout of main: a new session for every merge."""

ROLES = (PLANNER, IMPLEMENTER, JUDGE, PROFILER, MERGER, OUTLINER)
