"""The agents of an evolve run, each declared once as a `Role`.

A role says everything about how its agent is asked: the template, the reply model, the workspace
and what the turn may write, how long a turn may take, how often core asks the agent to correct a
reply or asks again after a lost turn, and what a turn costs. Core holds none of it. The host
supplies what it alone can (system prompts, tools, capabilities: see `vibesys.evolve_roles`); the
operator may change only a role's model (`agent.toml`).

A child is one mutation: the mutator is the only agent that costs an attempt, and a mutation that
fails is a failed individual, never asked again. The judge and the profiler read the candidate
the mutator left in the child's own workspace.
"""

from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.profilers import ProfilerSummary
from vs_core.api.search import (
    Access,
    Backoff,
    Charge,
    Exchanges,
    Role,
    WorkspaceMode,
    hours,
)

LOST_TURN_BACKOFF = Backoff(first=5.0, cap=120.0)
"""A lost turn is asked again after this wait on the run clock, doubling per loss."""

EXCHANGES = Exchanges(
    correction="reply_correction.j2", resume="resume.j2", wait_unrecorded="wait_unrecorded.j2"
)
"""The templates of the exchanges core starts with an agent that may wait for evaluations."""

MUTATOR = Role(
    name="mutator",
    title="Mutator",
    prompt="mutate.j2",
    reply=MutatorResponse,
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
"""Edits the child's workspace into one offspring: a cold-start seed, a repair or a mutation."""

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
"""Reviews the offspring as it stands, reading it and the evaluations the mutator took of it."""

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
    exchanges=EXCHANGES,
)
"""Advises on a passing offspring: its self-reported fitness is used only where nothing is trusted."""

ROLES = (MUTATOR, JUDGE, PROFILER)
