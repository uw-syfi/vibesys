"""The agents of an issue-queue run, each declared once as a `Role`.

A role says everything about how its agent is asked: the template, the reply model, the
workspace and what the turn may write, how long a turn may take, how often core asks the agent
to correct a reply or asks again after a lost turn, and what a turn costs. Core holds none of
it. The host supplies what it alone can (system prompts, tools, capabilities: see
`vibesys.issue_queue_core`); the operator may change only a role's model (`agent.toml`).

The implementer and the judge may end a turn waiting for the evaluations they submitted; core
suspends the turn and resumes it, and holds an implementer's reply until it has recorded the
checkpoint of the workspace the turn left. The performance evaluator measures in its own
checkout and never waits.
"""

from vibesys.orchestration.issue_queue.models import (
    IssueImplementerResult,
    IssueJudgeResult,
    IssuePerfEvalResult,
)
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

CORRECTION = Exchanges(correction="reply_correction.j2")
"""The only exchange core starts with an agent that never waits: asking it to fix its reply."""

IMPLEMENTER = Role(
    name="implementer",
    title="Builder",
    prompt="implement.j2",
    reply=IssueImplementerResult,
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
"""Works one issue in an isolated candidate workspace, once for each attempt."""

JUDGE = Role(
    name="judge",
    title="Reviewer",
    prompt="review.j2",
    reply=IssueJudgeResult,
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
"""Reviews an attempt's candidate, reading it and the evaluations the implementer took of it."""

PERF_EVALUATOR = Role(
    name="perf-evaluator",
    title="Perf evaluator",
    prompt="performance.j2",
    reply=IssuePerfEvalResult,
    workspace=WorkspaceMode.ISOLATED_CHILD,
    access=Access.WRITE_CANDIDATE,
    turn_time=hours(2),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    exchanges=CORRECTION,
)
"""Measures the tip in its own checkout (discarded afterwards) and files the issues it finds."""

ROLES = (IMPLEMENTER, JUDGE, PERF_EVALUATOR)
