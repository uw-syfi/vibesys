"""A tiny search that asks an agent, and its world, for the cases a turn decides.

The search opens one run-scoped session and asks it a turn, then asks it again: the second turn
is the successor of the first. The world answers each request the run makes (the prompt's render,
the session, the turn) with a reply that parses and counts the turns it answered, so a run that
lost one of them shows in the world, whatever the search made of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import (
    Access,
    ArtifactId,
    ArtifactRef,
    Charge,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    Exchanges,
    ExecuteRegisteredOperation,
    Flow,
    FlowState,
    NoWinner,
    ResourceId,
    ResumeSessionTurn,
    Role,
    RunEnvelope,
    SessionId,
    Value,
)
from vs_core.api.testing import (
    FAKE_REGISTRY,
    Harness,
    Prompt,
    Succeeded,
    fake_search,
    sample_limits,
    sample_retries,
)
from vs_dst.api import RunSetup
from vs_runtime.api.testing import Launch

from ._tiny import FACTS

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer


class Plan(Value):
    """What the agent answers."""

    n: int


ASKER = Role(
    name="asker",
    title="Asker",
    prompt="tiny/asker.j2",
    reply=Plan,
    workspace=None,
    access=Access.READ_ONLY,
    turn_time=600.0,
    corrections=0,
    lost_turns=0,
    charge=Charge.TURN,
    exchanges=Exchanges(
        correction="tiny/correction.j2",
        resume="tiny/resume.j2",
        wait_unrecorded="tiny/wait_unrecorded.j2",
    ),
)
"""The one agent the tiny search asks."""

REPLY = '{"n": 1}'
WITHIN = 100.0
"""How long the search gives each turn."""


class TurnsConfig(Value):
    """How many turns the search asks, and how many it is done with (its checkpoint)."""

    turns: int
    done: int = 0


@fake_search("tiny-turns", TurnsConfig, (ASKER,), snapshot=TurnsConfig)
async def tiny_turns(ctx: Flow, config: TurnsConfig) -> NoWinner:
    """Ask one session its turns, checkpointing after each, and end the run.

    It settles no work, so the run cannot claim success: it ends with no winner, whatever the
    far side answered. What the far side answered is the world's to say (``turns_answered``).
    """
    ctx.checkpoint(config)
    session = ctx.agents.session(ASKER)
    for number in range(config.done, config.turns):
        await session.turn(Prompt(text=f"turn {number}"), within=WITHIN)
        ctx.checkpoint(config.model_copy(update={"done": number + 1}))
    return NoWinner(reason="asked")


def turns_setup(turns: int) -> RunSetup:
    """The tiny search over ``turns`` turns, hosted with the fake operations."""
    return RunSetup(
        new_launch=lambda: Launch(tiny_turns, TurnsConfig(turns=turns)),
        harness=Harness(
            registry=FAKE_REGISTRY,
            facts=FACTS,
            limits=sample_limits(
                max_attempts=8,
                max_turns=8,
                max_parallel=1,
                request_retries=sample_retries(2),
            ),
            envelope_type=RunEnvelope[FlowState],
            schemas=tiny_turns.reply_schemas,
        ),
        schemas=tiny_turns.reply_schemas,
    )


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def _rendered(request: ExecuteRegisteredOperation) -> BaseModel:
    """The prompt of a render: an artifact named by the operation that asked for it."""
    asked = FAKE_REGISTRY.decode(request.operation)
    prompt = ArtifactRef(
        artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"), digest="prompt"
    )
    return asked.outcome_model.model_validate({"status": "succeeded", "prompts": (prompt,)})


class TurnsWorld:
    """Executors that render a prompt, make a session and answer each turn with a plan."""

    def __init__(self, turns: int = 2) -> None:
        self.setup = turns_setup(turns)
        self.turns_answered = 0
        """How many turns the far side has answered, whichever run asked them."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        del core
        match request:
            case DispatchTurn() | ResumeSessionTurn():
                self.turns_answered += 1
                return Succeeded(
                    output_json=REPLY, resource_id=_lease(request.turn.session.session_id)
                )
            case EnsureSession():
                return Succeeded(resource_id=_lease(request.spec.session_id))
            case CloseSession():
                return Succeeded(resource_id=_lease(request.session_id))
            case ExecuteRegisteredOperation():
                return Succeeded(outcome=_rendered(request))
            case _:
                return Succeeded()
