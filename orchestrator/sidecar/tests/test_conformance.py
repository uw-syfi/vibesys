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
from tests.fakes import CrashError, ScriptedDriver, TurnScript
from tests.helpers import launch
from vs_sidecar.driver import StartError
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2_grpc as sc_grpc
from vs_sidecar.main import work
from vs_sidecar.server import Wakeup, serve
from vs_sidecar.store import FileStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

#: Only guards against a hang: a scenario that works takes milliseconds.
HANG_GUARD_SECONDS = 30


def script_of(turn: Sequence[Mapping[str, Any]]) -> TurnScript:
    """The scripted turn one abstract provider turn stands for."""
    events: list[Any] = []
    status = orch.TURN_STATUS_COMPLETED
    usage: ProviderUsage | None = None
    for step in turn:
        if "think" in step:
            events.append(Reasoning(step["think"], step["item"]))
        elif "say" in step:
            events.append(AssistantText(step["say"], step["item"]))
        elif "tool" in step:
            events.append(ToolCall(step["id"], step["tool"], None))
            events.append(
                ToolResult(step["id"], step["tool"], step["output"], "", step["exit_code"], 0.0)
            )
        elif "tool_left_running" in step:
            events.append(ToolCall(step["id"], "Bash", {"command": step["tool_left_running"]}))
        elif "report" in step:
            events.append(UsageReport(_usage(step["report"]), None))
        elif "total" in step:
            usage = _usage(step["total"])
        elif "end" in step:
            status = orch.TURN_STATUS_FAILED
        else:
            events.append(CrashError)
    return TurnScript(events, status, usage)


def _starts(turn: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the abstract provider turn is the session failing to start."""
    return any("start_fails" in step for step in turn)


def _usage(counts: Mapping[str, int]) -> ProviderUsage:
    return ProviderUsage(
        tokens=TokenUsage(input_tokens=counts["input"], output_tokens=counts["output"])
    )


class GrpcClient:
    """A ``conformance.Client`` over a Unix socket, to a sidecar it can kill and start again."""

    def __init__(self, provider: Sequence[Sequence[Mapping[str, Any]]], state: Path) -> None:
        self._provider = provider
        self._state = state
        self._cleanup: Callable[[], None] = lambda: None
        self._start()

    def _start(self) -> None:
        wakeup = Wakeup()
        starts = [step for turn in self._provider for step in turn if "start_fails" in step]
        failure = None
        if starts:
            reason = getattr(orch, f"START_FAILURE_REASON_{starts[0]['start_fails']}")
            failure = StartError(reason, starts[0]["detail"])
        driver = ScriptedDriver(
            [script_of(turn) for turn in self._provider if not _starts(turn)], start_error=failure
        )
        self._provider = []  # a restarted sidecar has lost the agent: it has nothing to play
        engine = Engine(
            launch(), FileStore.at(self._state / "store"), lambda _sink: driver, wakeup.notify
        )
        engine.start()
        socket = self._state / f"s{id(engine)}.sock"
        server = serve(engine, wakeup, socket)
        stop = threading.Event()
        worker = threading.Thread(target=self._work, args=(engine, wakeup, stop))
        worker.start()
        channel = grpc.insecure_channel(f"unix:{socket}")
        self._stub = sc_grpc.SidecarServiceStub(channel)

        def cleanup() -> None:
            stop.set()
            wakeup.notify()
            worker.join(HANG_GUARD_SECONDS)
            channel.close()
            server.stop(grace=0).wait()

        self._cleanup = cleanup

    @staticmethod
    def _work(engine: Engine, wakeup: Wakeup, stop: threading.Event) -> None:
        try:
            work(engine, wakeup, stop)
        except CrashError:
            return  # the scripted crash: the process is gone

    def restart(self) -> None:
        self._cleanup()
        self._start()

    def close(self) -> None:
        self._cleanup()

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
    clients: list[GrpcClient] = []

    def start(provider: Sequence[Sequence[Mapping[str, Any]]]) -> GrpcClient:
        # A short path: a Unix socket's path has a small limit.
        directory = Path(tempfile.mkdtemp(prefix="vs"))
        client = GrpcClient(provider, directory)
        clients.append(client)
        return client

    yield start
    for client in clients:
        client.close()


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
