"""The agents of a multi run, each declared once as a `Role`.

A role says everything about how its agent is asked: the template, the reply model, the
workspace and what the turn may write, how long a turn may take, how often core asks the agent
to correct a reply or asks again after a lost turn, and what a turn costs. Core holds none of
it. The host supplies what it alone can (system prompts, tools, capabilities: see
`vibesys.multi_roles`); the operator may change only a role's model (`agent.toml`).

The designer is shared with the single search (`vibesys.serial_search`). The router and the
profiler are advisory and free: a turn of either that fails costs the design nothing but the
advice. The implementer is paid per attempt; the judge reads the exact retained candidate and
is free.
"""

from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.serial_search.api import LOST_TURN_BACKOFF, ORCHESTRATOR
from vs_core.api.search import (
    Access,
    Charge,
    Exchanges,
    Role,
    WorkspaceMode,
    hours,
    minutes,
)

EXCHANGES = Exchanges(
    correction="reply_correction.j2", resume="resume.j2", wait_unrecorded="wait_unrecorded.j2"
)
"""The templates of the exchanges core starts with an agent that may wait for evaluations."""

ROUTER = Role(
    name="router",
    title="Router",
    agent="orchestrator",
    prompt="pre_round.j2",
    reply=PreRoundDecision,
    workspace=None,
    access=Access.READ_ONLY,
    turn_time=minutes(30),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    exchanges=Exchanges(correction="reply_correction.j2"),
)
"""Decides, before a new hypothesis, whether fresh profile evidence is wanted.

The designer is asked: the router is its agent (`multi-orchestrator`), so its model, system
prompt and recorded runs are the designer's.
"""

PROFILER = Role(
    name="profiler",
    title="Profiler",
    prompt="profiler.j2",
    reply=ProfilerSummary,
    workspace=WorkspaceMode.READ_ONLY_REVISION,
    access=Access.READ_ONLY,
    turn_time=hours(1),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    exchanges=Exchanges(correction="reply_correction.j2"),
)
"""Collects and analyses a profile of the round's base, in a read-only checkout of it, writing
nothing in the project: its reply is the evidence."""

IMPLEMENTER = Role(
    name="implementer",
    title="Implementer",
    prompt="implement.j2",
    reply=ImplementerResponse,
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
    name="judge",
    title="Judge",
    prompt="judge.j2",
    reply=JudgeResponse,
    workspace=WorkspaceMode.READ_ONLY_REVISION,
    access=Access.READ_ONLY,
    turn_time=hours(1),
    corrections=1,
    lost_turns=2,
    lost_turn_backoff=LOST_TURN_BACKOFF,
    charge=Charge.TURN,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
"""Reviews the exact retained candidate, reading it and the evaluations taken of it."""

ROLES = (ORCHESTRATOR, ROUTER, PROFILER, IMPLEMENTER, JUDGE)
