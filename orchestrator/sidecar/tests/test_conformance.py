"""Every conformance scenario, against the Python sidecar over a real Unix socket."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
import pytest
from agentshim import (
    AssistantText,
    ProviderUsage,
    Reasoning,
    TokenUsage,
    ToolCall,
    ToolResult,
    UsageReport,
)

from tests import conformance
from tests.fakes import ScriptedDriver, TurnScript
from tests.helpers import launch
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2_grpc as sc_grpc
from vs_sidecar.main import work
from vs_sidecar.server import Wakeup, serve
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

#: Only guards against a hang: a scenario that works takes milliseconds.
HANG_GUARD_SECONDS = 30


def events_of(step: Mapping[str, Any]) -> list[Any]:
    """The agentshim events one abstract provider step stands for."""
    if "think" in step:
        return [Reasoning(step["think"], step["item"])]
    if "say" in step:
        return [AssistantText(step["say"], step["item"])]
    if "tool" in step:
        return [
            ToolCall(step["id"], step["tool"], None),
            ToolResult(step["id"], step["tool"], step["output"], "", step["exit_code"], 0.0),
        ]
    if "tool_left_running" in step:
        return [ToolCall(step["id"], "Bash", {"command": step["tool_left_running"]})]
    usage = step["usage"]
    return [
        UsageReport(
            ProviderUsage(
                tokens=TokenUsage(input_tokens=usage["input"], output_tokens=usage["output"])
            ),
            None,
        )
    ]


class GrpcClient:
    """A ``conformance.Client`` over a Unix socket."""

    def __init__(self, stub: sc_grpc.SidecarServiceStub) -> None:
        self._stub = stub

    def send(self, command: sc.SidecarCommand) -> None:
        self._stub.Send(sc.SendRequest(command=command), timeout=HANG_GUARD_SECONDS)

    def follow(
        self, after_offset: int, until: Callable[[Sequence[sc.SidecarEvent]], bool]
    ) -> list[sc.SidecarEvent]:
        seen: list[sc.SidecarEvent] = []
        call = self._stub.Follow(
            sc.FollowRequest(after_offset=after_offset), timeout=HANG_GUARD_SECONDS
        )
        try:
            for response in call:
                seen.append(response.event)
                if until(seen):
                    break
        finally:
            call.cancel()
        return seen


@pytest.fixture
def served() -> Iterator[Callable[[Sequence[Sequence[Mapping[str, Any]]]], GrpcClient]]:
    cleanups: list[Callable[[], None]] = []

    def start(provider: Sequence[Sequence[Mapping[str, Any]]]) -> GrpcClient:
        wakeup = Wakeup()
        driver = ScriptedDriver(
            [TurnScript([e for step in turn for e in events_of(step)]) for turn in provider]
        )
        engine = Engine(launch(), MemoryStore(), lambda _sink: driver, wakeup.notify)
        engine.start()
        directory = tempfile.TemporaryDirectory(prefix="vs")
        server = serve(engine, wakeup, Path(directory.name) / "s.sock")
        stop = threading.Event()
        worker = threading.Thread(target=work, args=(engine, wakeup, stop))
        worker.start()
        channel = grpc.insecure_channel(f"unix:{directory.name}/s.sock")

        def cleanup() -> None:
            stop.set()
            wakeup.notify()
            worker.join(HANG_GUARD_SECONDS)
            channel.close()
            server.stop(grace=0).wait()
            directory.cleanup()

        cleanups.append(cleanup)
        return GrpcClient(sc_grpc.SidecarServiceStub(channel))

    yield start
    for cleanup in cleanups:
        cleanup()


@pytest.mark.parametrize("name", conformance.names())
def test_scenario(
    name: str, served: Callable[[Sequence[Sequence[Mapping[str, Any]]]], GrpcClient]
) -> None:
    scenario = conformance.load(name)
    client = served(scenario.provider)
    events = conformance.run(scenario, client)
    for check in scenario.checks:
        conformance.check(check, events, scenario, client)


def test_an_invalid_command_is_refused_with_invalid_argument(
    served: Callable[[Sequence[Sequence[Mapping[str, Any]]]], GrpcClient],
) -> None:
    client = served([])
    with pytest.raises(grpc.RpcError) as caught:
        client.send(sc.SidecarCommand())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
