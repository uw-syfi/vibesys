"""A sidecar process over agentshim's scripted Claude peer, for vibesysd's end-to-end test.

It takes the real sidecar's command line and runs the real engine, store and server, with the one
difference that the provider is scripted: each turn answers ``reply N`` with no model behind it.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from agentshim import Agent, ApprovalPolicy, NativePermissions, TransportKind
from agentshim.testing import ClaudeFramePeer, FakeClock, FakeExecutor, SequentialIds
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.engine import Engine
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
    from collections.abc import Callable

    from agentshim import RawFrame

CONVERSATION = "11111111-1111-4111-8111-111111111111"
TURNS = 8


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
    peer = ClaudeFramePeer([_turn(n) for n in range(1, TURNS + 1)])
    agent = Agent(
        "claude",
        transport=TransportKind.STREAM,
        model="scripted",
        executor=FakeExecutor([], peers=lambda _request: peer),
        permissions=NativePermissions.bypass(),
        approvals=ApprovalPolicy.DENY,
        clock=FakeClock(),
        ids=SequentialIds(),
    )
    servers = load_mcp(args.mcp_config)

    def make(raw_sink: Callable[[RawFrame], None]) -> AgentshimDriver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                launch,
                agent=agent,
                mcp_servers=servers,
                raw_sink=raw_sink,
                new_conversation_id=conversation_id,
            ),
            preferred_id=CONVERSATION,
        )

    wakeup = Wakeup()
    engine = Engine(launch, FileStore.at(args.state_dir), make, wakeup.notify)
    serve_session(engine, wakeup, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
