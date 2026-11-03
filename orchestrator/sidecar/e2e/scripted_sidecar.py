"""A sidecar process over agentshim's scripted providers, for vibesys-host's end-to-end tests.

It takes the real sidecar's command line and runs the real engine, store and server, with the one
difference that the provider process is scripted, on the harness the launch names. Two modes:

- ``reply`` (the default): each turn answers ``reply N`` with no model behind it.
- ``act`` (``VIBESYS_E2E_SCRIPT=act``): the agent acts. Each message runs its role's script (see
  ``e2e.scripts``): real commands and real calls of the orchestrator's tools, mirrored in the
  frames the provider peer plays back.

In ``act`` mode the sidecar also writes, in its state directory, ``prompts/N.md`` (the N-th message
the agent was sent), ``spawns.json`` (the argv and config home the real driver gave the provider
process, and the plugin commands it ran) and ``peer-failure.txt`` if a turn broke.
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentshim import Agent, ApprovalPolicy, NativePermissions, TransportKind
from agentshim.testing import ClaudeFramePeer, FakeExecutor, SequentialIds

from e2e.actor import Actor
from e2e.host import HostMachine
from e2e.peers import ActingClaudePeers, ActingCodex
from e2e.scripts import Me
from vs_sidecar.driver import PROVIDERS, AgentshimDriver, Identity, SteerRefusedError, build_session
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.main import (
    PIDFILE,
    LaunchError,
    detach,
    load_launch,
    load_mcp,
    parse_args,
    serve_session,
    write_atomically,
)
from vs_sidecar.server import Wakeup
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim import RawFrame, Session, StdioMcpServer
    from agentshim.execution.process import SpawnRequest
    from agentshim.testing import FakePeer

    from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx

CONVERSATION = "11111111-1111-4111-8111-111111111111"
TURNS = 8
#: The environment variable that turns the scripted agent from a replier into an actor.
MODE_VARIABLE = "VIBESYS_E2E_SCRIPT"
#: The provider's config home variable per provider: the one place the plugin must be installed.
HOMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def _turn(number: int) -> list[dict[str, Any]]:
    text = f"reply {number}"
    return [
        {"type": "system", "subtype": "init", "session_id": CONVERSATION, "model": "m"},
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{number}",
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
            "session_id": CONVERSATION,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "num_turns": 1,
            "session_id": CONVERSATION,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    ]


def _without_token(argv: Sequence[str]) -> list[str]:
    """*argv* with the value of ``--mcp-config`` left out: it holds the session's token."""
    return [
        "<mcp config>" if i and argv[i - 1] == "--mcp-config" else arg for i, arg in enumerate(argv)
    ]


def dump_spawns(executor: FakeExecutor, provider: str, path: Path) -> None:
    """Write what the real driver asked the executor to run: the provider's argv and config home, and plugin commands."""
    spawns = [
        {"argv": _without_token(spawn.argv), "home": spawn.env.get(HOMES[provider], "")}
        for spawn in executor.spawns
    ]
    commands = [list(request.argv) for request in executor.requests]
    write_atomically(
        path, json.dumps({"provider": provider, "spawns": spawns, "commands": commands}, indent=2)
    )


def replying_executor() -> tuple[FakeExecutor, str]:
    """The Claude peer that answers ``reply N``."""
    peer = ClaudeFramePeer([_turn(n) for n in range(1, TURNS + 1)])
    return FakeExecutor([], peers=lambda _request: peer), "claude"


def acting_executor(
    launch: fx.LaunchSession,
    servers: list[StdioMcpServer],
    state_dir: Path,
    pending: deque[orch.Message],
) -> tuple[FakeExecutor, str]:
    """The peer for the launch's harness whose turns run its role's script."""
    provider = PROVIDERS[launch.harness]
    identity = Identity.of(launch)
    server = servers[0]
    machine = HostMachine(
        cwd=Path(launch.worktree),
        env=identity.over(os.environ),
        server=replace(server, env=identity.over(server.env)),
    )
    me = Me(
        run_id=launch.run_id,
        agent_id=launch.session.agent_id,
        harness=orch.Harness.Name(launch.harness).removeprefix("HARNESS_").lower(),
    )
    role = orch.AgentRole.Name(launch.role).removeprefix("AGENT_ROLE_").lower()
    actor = Actor(me, role, machine, state_dir, pending=pending)
    holder: list[FakeExecutor] = []

    def on_turn() -> None:
        dump_spawns(holder[0], provider, state_dir / "spawns.json")

    peers: Callable[[SpawnRequest], FakePeer]
    if provider == "claude":
        peers = ActingClaudePeers(actor, on_turn).build
    else:
        peers = ActingCodex(actor, on_turn).peer
    executor = FakeExecutor([], peers=peers)
    holder.append(executor)
    return executor, provider


class ScriptedDriver(AgentshimDriver):
    """Carry typed inputs to the acting peer, including steers during a turn."""

    def __init__(
        self, factory: Callable[[str | None], Session], pending: deque[orch.Message]
    ) -> None:
        """Build the scripted session and share its typed inbox with the actor."""
        super().__init__(factory, preferred_id=CONVERSATION)
        self.pending = pending

    def begin_turn(self, turn_id: str, prompt: str, *, message: orch.Message | None = None) -> None:
        """Announce a turn and discard any input from an interrupted earlier turn."""
        self.pending.clear()
        if message is not None:
            self.pending.append(message)
        super().begin_turn(turn_id, prompt, message=message)

    def steer(self, turn_id: str, text: str, *, message: orch.Message | None = None) -> None:
        """Queue the typed steer before the provider can answer it, undoing a refusal."""
        if message is not None:
            self.pending.append(message)
        try:
            super().steer(turn_id, text, message=message)
        except SteerRefusedError:
            if message is not None and message in self.pending:
                self.pending.remove(message)
            raise


def main() -> int:
    """Run one scripted session with the sidecar's command line."""
    args = parse_args(sys.argv[1:])
    if args.detach:
        detach(args.state_dir / "sidecar.log")
    try:
        launch = load_launch(args.launch, args.worktree)
    except LaunchError as error:
        sys.stderr.write(f"scripted_sidecar: {error}\n")
        return 2
    args.state_dir.mkdir(parents=True, exist_ok=True)
    write_atomically(args.state_dir / PIDFILE, f"{os.getpid()}\n")
    servers = load_mcp(args.mcp_config)
    pending: deque[orch.Message] = deque()
    if os.environ.get(MODE_VARIABLE) == "act":
        executor, _provider = acting_executor(launch, servers, args.state_dir, pending)
    else:
        executor, _provider = replying_executor()

    def agent(provider: str, identity: Identity) -> Agent:
        return Agent(
            provider,
            transport=TransportKind.STREAM,
            env=identity.over(os.environ),
            model="scripted",
            executor=executor,
            permissions=NativePermissions.bypass(),
            approvals=ApprovalPolicy.DENY,
            # This fixture runs real commands on another thread. Virtual time would
            # expire a queued steer while its peer is still executing a command,
            # causing a false rejection and a second delivery. Agent's production
            # clock measures those waits in real time; unit tests inject FakeClock.
            ids=SequentialIds(),
        )

    def make(raw_sink: Callable[[RawFrame], None]) -> AgentshimDriver:
        return ScriptedDriver(
            lambda conversation_id: build_session(
                launch,
                agent=agent,
                mcp_servers=servers,
                raw_sink=raw_sink,
                new_conversation_id=conversation_id,
            ),
            pending,
        )

    wakeup = Wakeup()
    engine = Engine(launch, FileStore.at(args.state_dir), make, wakeup.notify)
    serve_session(engine, wakeup, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
