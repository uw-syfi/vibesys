"""A failed turn among an attempt's concurrent peer turns never keeps the run from ending.

An attempt may ask several write turns at once, each in a session of its own, all in the
attempt's one workspace. Core takes the checkpoint of a write turn only when no other writer is
running (a snapshot taken then would be torn), so a turn that ends first is declined and leaves
its work to the later writer's terminal turn. That later turn can fail (crash, bad reply). These
tests play every mix of ok and failed peers, in either delivery order, against the real core and
scripted executors, and check the two things that went wrong:

- the work of a peer that finished stays retained, whichever peer ends last;
- a search that keeps a revision its workspace never held (it ignored the checkpoint it was
  given) is told so, and the run still ends with the workspace released.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import (
    Budget,
    End,
    Failed,
    Faults,
    Harness,
    Journal,
    Succeeded,
    assert_live,
    drive,
    sample_limits,
    sample_retries,
)
from vs_sim.api.testing import examples

from .toy_agent_search import EXCHANGES, FACTS, OPERATIONS, Prompt, Result, ToyHost, revision_of

if TYPE_CHECKING:
    from vs_core.api.testing import Answer, Trace

type Fault = Literal["ok", "crash", "malformed"]
FAULTS: tuple[Fault, ...] = ("ok", "crash", "malformed")
type Keeps = Literal["checkpoint", "reply"]

PEER = core.Role(
    name="peer",
    title="Peer",
    prompt="toy/peer.j2",
    reply=Result,
    workspace=core.WorkspaceMode.ISOLATED_CHILD,
    access=core.Access.WRITE_CANDIDATE,
    turn_time=10_000.0,
    corrections=0,
    lost_turns=0,
    charge=core.Charge.ATTEMPT,
    exchanges=EXCHANGES,
)
UNHELD = revision_of("a revision the workspace never held")
"""A commit an agent reports that no snapshot of the workspace produced."""
LIMITS = sample_limits(
    max_attempts=4, max_turns=20, max_parallel=4, request_retries=sample_retries(2)
)


class Config(core.Value):
    """How many peers the attempt asks at once, and which revision its search keeps."""

    peers: int = 2
    keeps: Keeps = "checkpoint"


class Peer(core.Value):
    """What one peer's turn ended with."""

    finished: bool = False
    retained: str | None = None
    """The revision of the checkpoint core handed back with the reply, if it recorded one."""


async def _peer(ctx: core.Flow, workspace: core.Workspace, number: int) -> Peer:
    session = ctx.agents.session(PEER, workspace=workspace, revision=workspace.base)
    match await session.turn(Prompt(text=f"peer {number}")):
        case core.TurnFinished(checkpoint=core.Retained(revision=revision)):
            return Peer(finished=True, retained=revision.revision_id.root)
        case core.TurnFinished():
            return Peer(finished=True)
    return Peer()


@search("peers", config=Config, operations=OPERATIONS, roles=(PEER,))
async def peers(ctx: core.Flow, config: Config) -> None:
    """Ask ``config.peers`` write turns at once in one workspace, then keep a revision."""
    async with ctx.workspaces.open(
        attempt=core.AttemptId(root="attempt"),
        item=core.ItemId(root="item"),
        plan=core.WorkspacePlan(mode=core.WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline),
        budget=core.AttemptBudget(admission_charge=1, paid_invocation_limit=config.peers),
    ) as workspace:
        if isinstance(workspace, core.NotOpened):
            return
        ended: list[Peer] = []
        async with ctx.group() as group:
            for number in range(config.peers):
                group.start(f"peer-{number}", _peer, workspace, number)
            ended.extend([result async for result in group if isinstance(result, Peer)])
        held = [item.retained for item in ended if item.retained is not None]
        if config.keeps == "reply" and any(item.finished for item in ended):
            candidate = UNHELD
        elif held:
            candidate = core.RevisionRef.of_git_commit(held[-1])
        else:
            return
        ctx.workspaces.keep(workspace, candidate, core.Assessment(outcome="succeeded"))


@dataclass
class PeerHost(ToyHost):
    """The toy executors, with peers that end as scripted and a workspace that keeps what it holds.

    Like the production workspace owner, it retains only a revision it snapshotted or was given.
    """

    script: tuple[Fault, ...] = ()
    held: frozenset[str] = frozenset({FACTS.baseline.revision_id.root})

    def __call__(self, request: core.Request, state: core.CoreState) -> Answer:
        """What the executor of ``request`` reports."""
        match request:
            case core.SnapshotAndRetain():
                held = revision_of(request.attempt.attempt_id.root).revision_id.root
                self.held = self.held | {held}
            case core.RetainRevision() if request.revision.revision_id.root not in self.held:
                return Failed()
            case core.DispatchTurn():
                lease = core.ResourceId(root=f"lease:{request.turn.session.session_id.root}")
                match self.script[_peer_number(request)]:
                    case "crash":
                        return Failed()
                    case "malformed":
                        return Succeeded(output_json="not json", resource_id=lease)
                    case "ok":
                        return Succeeded(output_json='{"summary": "done"}', resource_id=lease)
        return super().__call__(request, state)


def _peer_number(request: core.DispatchTurn) -> int:
    return int(request.turn.session.session_id.root.rsplit("peer", 1)[-1].strip("-:"))


def run_peers(
    script: tuple[Fault, ...],
    keeps: Keeps = "checkpoint",
    *,
    reorder: bool = False,
    host: PeerHost | None = None,
) -> Trace:
    """Drive the peers search on the real core; ``script[n]`` is how peer ``n`` ends."""
    host = host or PeerHost(script=script)
    config = Config(peers=len(script), keeps=keeps)
    harness = Harness(
        registry=OPERATIONS.registry(),
        facts=FACTS,
        limits=LIMITS,
        lifecycle=frozenset({"suspend"}),
        during_turn=host.during_turn,
        shell_events=True,
        schemas=peers.reply_schemas,
        envelope_type=core.RunEnvelope[core.FlowState],
    )
    return drive(
        peers.driver(config, OPERATIONS.registry()), config, host, harness, Faults(reorder=reorder)
    )


def assert_released(trace: Trace) -> None:
    """The run ended on its own, live, and left no attempt, session or workspace behind."""
    assert trace.finished
    journal = Journal.from_log(trace.log)
    assert_live(journal, trace.core, Budget(retries=LIMITS.request_retries.limit), End.TERMINAL)
    assert all(a.phase == core.AttemptPhase.TERMINAL for a in trace.core.attempts.attempts)
    assert all(s.phase == core.SessionPhase.TERMINAL for s in trace.core.sessions.sessions)


def checkpointed(trace: Trace) -> set[str]:
    """The sessions whose own invocation has a recorded checkpoint."""
    return {
        row.invocation.session_id.root
        for attempt in trace.core.attempts.attempts
        for row in attempt.checkpoints
        if row.invocation is not None
    }


def test_the_work_of_a_peer_that_ended_first_is_retained_when_the_later_peer_crashes() -> None:
    trace = run_peers(("ok", "crash"))

    assert_released(trace)
    assert checkpointed(trace)


def test_a_revision_the_workspace_never_held_does_not_keep_the_attempt_closing() -> None:
    trace = run_peers(("ok", "crash"), keeps="reply")

    assert_released(trace)


_SETTINGS = settings(
    max_examples=examples(pr=3, nightly=60), deadline=None, derandomize=True, database=None
)


@_SETTINGS
@given(
    script=st.lists(st.sampled_from(FAULTS), min_size=1, max_size=4).map(tuple),
    keeps=st.sampled_from(("checkpoint", "reply")),
    reorder=st.booleans(),
)
def test_a_run_ends_whichever_peers_fail_and_whichever_revision_the_search_keeps(
    script: tuple[Fault, ...], keeps: Keeps, *, reorder: bool
) -> None:
    trace = run_peers(script, keeps, reorder=reorder)

    assert_released(trace)
    if "ok" in script:
        # Some peer finished, so some checkpoint covers its work, whichever peer ended last.
        assert checkpointed(trace)
