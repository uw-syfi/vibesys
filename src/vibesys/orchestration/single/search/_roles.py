"""The agents of a single run, each declared once as a `Role`.

The designer is shared with the multi search (`vibesys.serial_search`). The implementer is the
one agent that implements the round's plan, reviews its own work and captures only the profile
it needs; it asks for core's checkpoint of what it changed and may wait for evaluations it
submitted. The host supplies what it alone can (system prompts, tools, capabilities: see
`vibesys.single_roles`); the operator may change only a role's model (`agent.toml`).
"""

from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.serial_search.api import LOST_TURN_BACKOFF, ORCHESTRATOR
from vs_core.api.search import Access, Charge, Exchanges, Role, WorkspaceMode, hours

EXCHANGES = Exchanges(
    correction="reply_correction.j2", resume="resume.j2", wait_unrecorded="wait_unrecorded.j2"
)
"""The templates of the exchanges core starts with an agent that may wait for evaluations."""

IMPLEMENTER = Role(
    name="implementer",
    title="Implementer",
    prompt="round.j2",
    reply=SingleAgentRoundResponse,
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
"""Implements, self-reviews and profiles one round in an isolated candidate workspace."""

ROLES = (ORCHESTRATOR, IMPLEMENTER)
