"""A role that writes works in its workspace as core admitted it; one that reads reads as declared."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import FAKE_OPERATIONS, FlowRun, Prompt

from .flow_support import EXCHANGES, Config, open_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


class Edit(core.RevisionReply):
    """What the writer answers."""

    summary: str


class Look(core.Value):
    """What the reader answers."""

    seen: bool


WRITER = core.Role(
    name="writer",
    title="Writer",
    prompt="fake/writer.j2",
    reply=Edit,
    workspace=core.WorkspaceMode.ISOLATED_CHILD,
    access=core.Access.WRITE_CANDIDATE,
    turn_time=600.0,
    corrections=0,
    lost_turns=0,
    charge=core.Charge.ATTEMPT,
    exchanges=EXCHANGES,
)
READER = core.Role(
    name="reader",
    title="Reader",
    prompt="fake/reader.j2",
    reply=Look,
    workspace=core.WorkspaceMode.READ_ONLY_REVISION,
    access=core.Access.READ_ONLY,
    turn_time=600.0,
    corrections=0,
    lost_turns=0,
    charge=core.Charge.TURN,
    exchanges=EXCHANGES,
)


def asking(
    role: core.Role,
) -> Callable[[core.Flow, Config], Coroutine[Any, Any, core.NoWinner | None]]:
    """A flow that asks ``role`` once in an exclusive-root workspace."""

    async def works(ctx: core.Flow, _config: Config) -> core.NoWinner | None:
        async with open_workspace(ctx, "w") as workspace:
            if isinstance(workspace, core.Workspace):
                session = ctx.agents.session(role, workspace=workspace, revision=ctx.facts.baseline)
                await session.turn(Prompt(text="go"))
        return core.NoWinner(reason="done")

    return works


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
@pytest.mark.parametrize(
    ("role", "mode"),
    [
        (WRITER, core.WorkspaceMode.EXCLUSIVE_ROOT),
        (READER, core.WorkspaceMode.READ_ONLY_REVISION),
    ],
    ids=["writer", "reader"],
)
def test_a_writing_role_takes_the_mode_of_its_workspace_and_a_reading_role_keeps_its_own(
    role: core.Role, mode: core.WorkspaceMode, *, restarting: bool
) -> None:
    definition = search("fake", config=Config, operations=FAKE_OPERATIONS, roles=(role,))(
        asking(role)
    )
    run = FlowRun.start(definition, Config(), restarting=restarting)
    run.open_workspace(run.expect(core.StartAttempt))

    asked = run.expect_turn(role=role.name).request
    assert isinstance(asked, core.RequestTurn)
    assert isinstance(asked.turn.workspace, core.WorkspaceRef)
    assert asked.turn.workspace.mode is mode
