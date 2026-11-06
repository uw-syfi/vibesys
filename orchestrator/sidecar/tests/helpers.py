"""Builders shared by the sidecar's tests: commands, launches and scripted agentshim sessions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agentshim import Agent, ApprovalPolicy, NativePermissions, StdioMcpServer, TransportKind
from agentshim.testing import (
    ClaudeFramePeer,
    ClaudeStreamPeers,
    CodexScript,
    FakeClock,
    FakeExecutor,
    ReplayGates,
    SequentialIds,
)

from vs_sidecar.driver import AgentFactory, AgentshimDriver, Driver, Identity, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from agentshim import RawFrame
    from google.protobuf.timestamp_pb2 import Timestamp

REF = fx.SessionRef(agent_id="agent-1", session=1)

#: Where this test's plugin marketplace and plugin home live; the autouse fixture in conftest sets it.
_root: Path | None = None


def use_plugin_root(root: Path | None) -> None:
    """Make launches built from now on load a plugin from a marketplace written under *root*."""
    # lint-waiver: LW-997303 [PLW0603]; The fixture's per-test directory is read by the plain builder functions below.
    # > A class or closure would force every builder in this module to become a method or take the directory as a parameter.
    global _root  # noqa: PLW0603
    _root = root


def write_marketplace(root: Path, name: str = "vibesys") -> Path:
    """A local marketplace checkout under *root* with one skill-only plugin *name*; returns its root."""
    manifest = root / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "vibesys",
                "plugins": [{"name": name, "source": "./skills", "strict": False, "skills": []}],
            }
        )
    )
    return root


def session_plugin(root: Path, *, name: str = "vibesys") -> fx.SessionPlugin:
    """The plugin a launch names: a marketplace under *root* and a plugin home that does not exist."""
    return fx.SessionPlugin(
        marketplace=str(write_marketplace(root / "marketplace", name)),
        name=name,
        home=str(root / "plugin-home"),
    )


#: The environment a scripted session's identity is laid over, standing for the login shell's.
BASE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/tester"}


def scripted_agents(
    provider: str,
    executor: FakeExecutor,
    base_env: Mapping[str, str] = BASE_ENV,
    **options: Any,
) -> AgentFactory:
    """An ``AgentFactory`` whose agents run *provider* over *executor*, on a fixed clock.

    Each agent's environment is the session's identity laid over *base_env*, as it is over the
    login shell's in production.
    """

    def make(_provider: str, identity: Identity) -> Agent:
        return Agent(
            provider,
            transport=TransportKind.STREAM,
            model="fake-model",
            executor=executor,
            env=identity.over(base_env),
            permissions=NativePermissions.bypass(),
            approvals=ApprovalPolicy.DENY,
            clock=FakeClock(),
            ids=SequentialIds(),
            **options,
        )

    return make


def launch(harness: int = orch.HARNESS_CODEX, **fields: Any) -> fx.LaunchSession:
    """A valid ``LaunchSession``."""
    base: dict[str, Any] = {
        "session": REF,
        "run_id": "run-1",
        "repository": "repo",
        "task": "task",
        "role": orch.AGENT_ROLE_WORKER,
        "harness": harness,
        "worktree": "/work",
    }
    if _root is not None:
        base["plugin"] = session_plugin(_root)
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
        start_turn=fx.StartTurn(
            session=REF, turn_id=turn_id, message=message(f"msg-{turn_id}", text)
        ),
    )


def steer(
    effect_id: str, turn_id: str, message_id: str, text: str = "also this"
) -> sc.SidecarCommand:
    """A ``Steer`` command."""
    return sc.SidecarCommand(
        effect_id=effect_id,
        steer=fx.Steer(session=REF, turn_id=turn_id, message=message(message_id, text)),
    )


def interrupt(effect_id: str, turn_id: str) -> sc.SidecarCommand:
    """An ``Interrupt`` command."""
    return sc.SidecarCommand(
        effect_id=effect_id, interrupt=fx.Interrupt(session=REF, turn_id=turn_id)
    )


def close(effect_id: str, *, force: bool = False) -> sc.SidecarCommand:
    """A ``CloseSession`` command."""
    return sc.SidecarCommand(effect_id=effect_id, close=fx.CloseSession(session=REF, force=force))


def kinds(events: Sequence[sc.SidecarEvent]) -> list[str]:
    """The body kind of each event, for readable assertions."""
    return [event.WhichOneof("body") or "" for event in events]


def codex_factory(
    script: CodexScript, **options: Any
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over a scripted Codex app-server."""
    return _factory("codex", FakeExecutor([], peers=script.peer), options)


def claude_factory(
    turns: Sequence[Sequence[Any]], gates: ReplayGates | None = None, **options: Any
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over hand-written Claude stream-json frames."""
    peer = ClaudeFramePeer(turns)
    return _factory("claude", FakeExecutor([], peers=lambda _request: peer, gates=gates), options)


def claude_replay_factory(
    peers: ClaudeStreamPeers,
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over agentshim's scripted Claude peers."""
    return _factory("claude", FakeExecutor([], peers=peers.peer), {})


def driver_factory_over(
    provider: str,
    executor: FakeExecutor,
    *,
    mcp_servers: Sequence[StdioMcpServer] = (),
    launch_fields: Mapping[str, Any] | None = None,
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    """A driver factory over *executor*, for a session given *mcp_servers*."""
    return _factory(provider, executor, {}, mcp_servers, launch_fields or {})


def _factory(
    provider: str,
    executor: FakeExecutor,
    options: dict[str, Any],
    mcp_servers: Sequence[StdioMcpServer] = (),
    launch_fields: Mapping[str, Any] | None = None,
) -> Callable[[Callable[[RawFrame], None]], Driver]:
    agent = scripted_agents(provider, executor, **options)
    fields = dict(launch_fields or {})

    def make(raw_sink: Callable[[RawFrame], None]) -> Driver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                launch(**fields),
                agent=agent,
                mcp_servers=mcp_servers,
                raw_sink=raw_sink,
                new_conversation_id=conversation_id,
            ),
            preferred_id="11111111-1111-4111-8111-111111111111",
            workdir=launch(**fields).worktree,
        )

    return make
