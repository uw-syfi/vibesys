"""Builders shared by the sidecar's tests: commands, launches and scripted agentshim sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentshim import Agent, ApprovalPolicy, NativePermissions, TransportKind
from agentshim.testing import (
    ClaudeFramePeer,
    ClaudeStreamPeers,
    CodexScript,
    FakeClock,
    FakeExecutor,
    ReplayGates,
    SequentialIds,
)

from vs_sidecar.driver import AgentshimDriver, Driver, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim import RawFrame
    from google.protobuf.timestamp_pb2 import Timestamp

REF = fx.SessionRef(agent_id="agent-1", session=1)


def launch(harness: int = orch.HARNESS_CODEX, **fields: Any) -> fx.LaunchSession:
    """A valid ``LaunchSession``."""
    base: dict[str, Any] = {
        "session": REF,
        "run_id": "run-1",
        "repository": "repo",
        "role": orch.AGENT_ROLE_WORKER,
        "harness": harness,
        "worktree": "/work",
    }
    base.update(fields)
    return fx.LaunchSession(**base)


def message(message_id: str = "m1", text: str = "do the thing") -> orch.Message:
    """A text message addressed to the agent."""
    stamp: Timestamp = orch.Message().at.__class__()
    stamp.FromSeconds(1)
    return orch.Message(
        id=message_id,
        run_id="run-1",
        to_agent_id="agent-1",
        delivery=orch.DELIVERY_QUEUED,
        at=stamp,
        text=text,
    )


def start_turn(effect_id: str, turn_id: str, text: str = "do the thing") -> sc.SidecarCommand:
    """A ``StartTurn`` command."""
    return sc.SidecarCommand(
        effect_id=effect_id,
        start_turn=fx.StartTurn(session=REF, turn_id=turn_id, message=message(f"msg-{turn_id}", text)),
    )


def steer(effect_id: str, turn_id: str, message_id: str, text: str = "also this") -> sc.SidecarCommand:
    """A ``Steer`` command."""
    return sc.SidecarCommand(
        effect_id=effect_id,
        steer=fx.Steer(session=REF, turn_id=turn_id, message=message(message_id, text)),
    )


def interrupt(effect_id: str, turn_id: str) -> sc.SidecarCommand:
    """An ``Interrupt`` command."""
    return sc.SidecarCommand(effect_id=effect_id, interrupt=fx.Interrupt(session=REF, turn_id=turn_id))


def close(effect_id: str, *, force: bool = False) -> sc.SidecarCommand:
    """A ``CloseSession`` command."""
    return sc.SidecarCommand(effect_id=effect_id, close=fx.CloseSession(session=REF, force=force))


def kinds(events: Sequence[sc.SidecarEvent]) -> list[str]:
    """The body kind of each event, for readable assertions."""
    return [event.WhichOneof("body") or "" for event in events]


def codex_factory(script: CodexScript, **options: Any) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over a scripted Codex app-server."""
    return _factory("codex", FakeExecutor([], peers=script.peer), options)


def claude_factory(
    turns: Sequence[Sequence[Any]], gates: ReplayGates | None = None, **options: Any
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over hand-written Claude stream-json frames."""
    peer = ClaudeFramePeer(turns)
    return _factory("claude", FakeExecutor([], peers=lambda _request: peer, gates=gates), options)


def claude_replay_factory(peers: ClaudeStreamPeers) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over agentshim's scripted Claude peers."""
    return _factory("claude", FakeExecutor([], peers=peers.peer), {})


def _factory(
    provider: str, executor: FakeExecutor, options: dict[str, Any]
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    agent = Agent(
        provider,
        transport=TransportKind.STREAM,
        model="fake-model",
        executor=executor,
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/tester"},
        permissions=NativePermissions.bypass(),
        approvals=ApprovalPolicy.DENY,
        clock=FakeClock(),
        ids=SequentialIds(),
        **options,
    )

    def make(raw_sink: Callable[[RawFrame], None]) -> Driver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                launch(), agent=agent, raw_sink=raw_sink, new_conversation_id=conversation_id
            ),
            preferred_id="11111111-1111-4111-8111-111111111111",
        )

    return make
