"""Focused tests for the Omnigent agent-driver adapter."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import importlib
import inspect
import json
import os
import shutil
import sys
import threading
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from omnigent.inner import os_env as omnigent_os_env
from omnigent.inner.executor import ExecutorConfig
from omnigent.inner.sandbox import (
    SandboxPolicy,
)
from omnigent.tools.builtins import os_env as omnigent_os_tools
from tests.support import run_test_command

from vibesys.events import CommandResultPayload, JsonResultPayload
from vibesys.schemas import JudgeResponse
from vs_agent.api import (
    AgentEvent,
    MCPServerSpec,
)
from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.drivers import omnigent as driver_subject
from vs_agent.drivers._omnigent_runtime import OmnigentAsyncRuntime
from vs_agent.drivers.omnigent import (
    _TOOL_EXECUTOR_ATTR,
    OmnigentDriver,
    OmnigentDriverError,
    OmnigentSession,
    _build_os_tools,
    _LifecycleState,
)
from vs_agent.omnigent.providers import OMNIGENT_PROVIDER_EXECUTORS
from vs_sandbox.api import HostResource, ProjectPathPolicy

omnigent = pytest.importorskip("omnigent")
TextChunk = omnigent.TextChunk
ToolCallComplete = omnigent.ToolCallComplete
ToolCallRequest = omnigent.ToolCallRequest
TurnComplete = omnigent.TurnComplete


def _sandbox_backend_available() -> bool:
    if os.environ.get("VIBESYS_REQUIRE_SANDBOX_TESTS") == "1":
        return True
    if sys.platform.startswith("linux"):
        return shutil.which("bwrap") is not None
    if sys.platform == "darwin":
        return shutil.which("sandbox-exec") is not None
    return False


requires_sandbox_backend = pytest.mark.skipif(
    not _sandbox_backend_available(),
    reason="requires the platform sandbox backend (bwrap / sandbox-exec)",
)


def _registered_executor_class(provider: str) -> type[Any]:
    spec = OMNIGENT_PROVIDER_EXECUTORS[provider]
    module = importlib.import_module(spec.module)
    executor_class = getattr(module, spec.class_name)
    assert isinstance(executor_class, type)
    return executor_class


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_registered_executor_matches_pinned_omnigent_api(provider: str) -> None:
    driver = OmnigentDriver()
    executor_spec = OMNIGENT_PROVIDER_EXECUTORS[provider]

    executor_class = _registered_executor_class(provider)
    parameters = inspect.signature(executor_class.__init__).parameters

    assert executor_class.__name__ == executor_spec.class_name
    assert "cwd" in parameters
    assert "model" in parameters


@pytest.mark.parametrize(
    ("provider", "required_binary"),
    [("claude", None), ("codex", "codex")],
)
def test_registered_executor_exposes_tool_dispatch_seam(
    provider: str,
    required_binary: str | None,
) -> None:
    if required_binary is not None and shutil.which(required_binary) is None:
        pytest.skip(f"{provider} executor needs the {required_binary!r} CLI to construct")
    driver = OmnigentDriver()
    executor_class = _registered_executor_class(provider)

    executor = executor_class(cwd=".", model=None)
    try:
        assert hasattr(executor, _TOOL_EXECUTOR_ATTR)
    finally:
        driver.close_executor(executor)


class _FakeExecutor:
    def __init__(self, events: list[Any], *, delay: float = 0) -> None:
        self.events = events
        self.delay = delay
        self.calls: list[tuple[Any, ...]] = []
        self.close_calls = 0
        self.thread_id = "provider-session"
        self._tool_executor = None

    def run_turn(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        instructions: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[object]:
        self.calls.append((messages, tools, instructions, config, os.environ.get("DRIVER_TEST")))

        async def stream() -> AsyncIterator[object]:
            if self.delay:
                await asyncio.sleep(self.delay)
            for event in self.events:
                yield event

        return stream()

    async def close(self) -> None:
        self.close_calls += 1


@dataclass
class _Observer:
    events: list[AgentEvent] = field(default_factory=list)

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)


def _spec(tmp_path: Path, **changes: object) -> AgentSessionSpec:
    base = AgentSessionSpec(
        role="judge",
        provider="codex",
        workspace=tmp_path,
        model="gpt-5.5",
        policy=AgentExecutionPolicy(),
    )
    return replace(base, **changes)


def _session(
    tmp_path: Path,
    executor: _FakeExecutor,
) -> tuple[OmnigentDriver, OmnigentSession]:
    driver = OmnigentDriver()
    with patch.object(
        driver,
        "_build_executor",
        return_value=(executor, [{"name": "sys_os_read"}], None, None),
    ):
        session = driver.create_session(
            _spec(tmp_path, reasoning_effort="high", environment=(("DRIVER_TEST", "set"),))
        )
    return driver, session


@pytest.mark.parametrize(
    ("provider", "attribute"),
    [("codex", "_env"), ("claude", "_extra_env")],
)
def test_provider_environment_is_applied_during_session_creation_without_mutating_parent(
    provider: str,
    attribute: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "VIBESYS_TEST_PROVIDER_ENV"
    monkeypatch.setenv(key, "ambient")
    provider_environments: list[dict[str, str]] = []

    class Executor(_FakeExecutor):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__([])
            environment = {"BASE": "preserved"}
            setattr(self, attribute, environment)
            provider_environments.append(environment)

    driver = OmnigentDriver()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: Executor)
    monkeypatch.setattr(driver, "_build_os_env", lambda _spec, **_kwargs: object())
    monkeypatch.setattr(driver_subject, "resolve_active_rust_toolchain", lambda *_a, **_k: None)
    monkeypatch.setattr(
        driver_subject,
        "_build_os_tools",
        lambda *_args, **_kwargs: SimpleNamespace(
            schemas=[],
            handles=lambda _name: False,
            dispatch=lambda _name, _args: None,
            close=lambda: None,
        ),
    )

    driver.create_session(_spec(tmp_path, provider=provider, environment=((key, "session"),)))
    assert provider_environments == [{"BASE": "preserved", key: "session"}]
    assert os.environ[key] == "ambient"
    inherited = run_test_command(
        [sys.executable, "-c", f"import os; print(os.environ[{key!r}])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert inherited == "ambient"
    driver.close()


def test_distinct_tool_helpers_snapshot_their_own_environment_without_command_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    key = "CARGO_HOME"
    monkeypatch.setenv(key, "ambient")
    start_together = threading.Barrier(2)
    environment_ready = threading.Barrier(3)
    captured: list[str] = []

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0
            helper = Helper()
            setattr(self, "_helper", helper)
            self.start_helper = helper.start

        def close(self) -> None:
            self.close_calls += 1

    class Helper:
        def __init__(self) -> None:
            self.sandbox = SandboxPolicy(
                backend_type="none",
                active=False,
                read_roots=None,
                write_roots=[],
                write_files=[],
                allow_network=True,
            )

        def start(self) -> None:
            helper_environment = omnigent_os_env.build_helper_env(os.environ, self.sandbox)
            captured.append(helper_environment[key])
            environment_ready.wait(timeout=2)

    class ShellTool:
        @staticmethod
        def name() -> str:
            return "sys_os_shell"

        @staticmethod
        def get_schema() -> dict[str, Any]:
            return {
                "function": {
                    "name": "sys_os_shell",
                    "description": "shell",
                    "parameters": {"type": "object"},
                }
            }

        def __init__(self, resource: Resource) -> None:
            self.resource = resource

        def invoke(self, arguments: str, _context: object) -> str:
            start_together.wait(timeout=2)
            self.resource.start_helper()
            return arguments

    resources: list[Resource] = []

    def create_environment(_spec: object) -> Resource:
        resource = Resource()
        resources.append(resource)
        return resource

    monkeypatch.setattr(omnigent_os_env, "create_os_environment", create_environment)
    monkeypatch.setattr(
        omnigent_os_tools,
        "build_os_env_tools",
        lambda environment: [ShellTool(environment)],
    )
    cargo_homes = [str(tmp_path / "optimizer cargo"), str(tmp_path / "chat cargo")]
    built = [
        _build_os_tools(object(), tmp_path, {key: cargo_home}, object())
        for cargo_home in cargo_homes
    ]

    def dispatch_together() -> tuple[list[str], str]:
        def unrelated_subprocess() -> str:
            environment_ready.wait(timeout=2)
            return run_test_command(
                [sys.executable, "-c", f"import os; print(os.environ[{key!r}])"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures: list[concurrent.futures.Future[Any]] = [
                pool.submit(
                    asyncio.run,
                    os_tools.dispatch("sys_os_shell", {"command": "cargo test"}),
                )
                for os_tools in built
            ]
            inherited = pool.submit(unrelated_subprocess)
            return [future.result(timeout=2) for future in futures], inherited.result(timeout=2)

    commands, first_inherited = dispatch_together()
    monkeypatch.setenv(key, "new ambient")
    restarted_commands, second_inherited = dispatch_together()

    assert commands == ['{"command": "cargo test"}', '{"command": "cargo test"}']
    assert restarted_commands == commands
    assert sorted(captured) == sorted(cargo_homes * 2)
    assert os.environ[key] == "new ambient"
    assert (first_inherited, second_inherited) == ("ambient", "new ambient")
    for os_tools in built:
        os_tools.close()
    assert [resource.close_calls for resource in resources] == [1, 1, 1, 1]


def test_os_tool_setup_closes_all_environments_when_schema_building_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class Helper:
        sandbox = SimpleNamespace()

        @staticmethod
        def _start_locked() -> None:
            pass

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0
            self._helper = Helper()

        def close(self) -> None:
            self.close_calls += 1

    schema_error = RuntimeError("schema exploded")

    class BrokenTool:
        @staticmethod
        def name() -> str:
            return "sys_os_shell"

        @staticmethod
        def get_schema() -> dict[str, Any]:
            raise schema_error

    resources: list[Resource] = []

    def create_environment(_spec: object) -> Resource:
        resource = Resource()
        resources.append(resource)
        return resource

    monkeypatch.setattr(omnigent_os_env, "create_os_environment", create_environment)
    monkeypatch.setattr(
        omnigent_os_tools,
        "build_os_env_tools",
        lambda _environment: [BrokenTool()],
    )

    with pytest.raises(RuntimeError, match="schema exploded"):
        _build_os_tools(object(), tmp_path, {"CARGO_HOME": "isolated"}, object())

    assert [resource.close_calls for resource in resources] == [1, 1]


def test_os_tool_setup_validates_helper_seam_and_closes_partial_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    resources: list[Resource] = []

    def create_environment(_spec: object) -> Resource:
        resource = Resource()
        resources.append(resource)
        return resource

    monkeypatch.setattr(omnigent_os_env, "create_os_environment", create_environment)

    with pytest.raises(OmnigentDriverError, match=r"_helper.*sandbox"):
        _build_os_tools(object(), tmp_path, {"CARGO_HOME": "isolated"}, object())

    assert [resource.close_calls for resource in resources] == [1, 1]


def test_owned_os_tools_close_all_resources_once_and_preserve_first_failure() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str, error: BaseException | None = None) -> None:
            self.name = name
            self.error = error

        def close(self) -> None:
            closed.append(self.name)
            if self.error is not None:
                raise self.error

    first_error = KeyboardInterrupt("first")
    os_tools = driver_subject._OwnedOSTools(  # noqa: SLF001  # LW-010001; asserts the owner closes every resource once and retains the first BaseException
        schemas=[],
        dispatch=lambda _name, _args: None,
        environments=(
            Resource("first", first_error),
            Resource("second", RuntimeError("second")),
        ),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        os_tools.close()

    assert raised.value is first_error
    assert closed == ["first", "second"]
    os_tools.close()
    assert closed == ["first", "second"]


def test_turn_normalizes_events_usage_schema_and_session_id(tmp_path: Path) -> None:
    executor = _FakeExecutor(
        [
            TextChunk("partial"),
            ToolCallRequest(name="Read", args={"path": "a"}),
            ToolCallComplete(name="Read", result="contents"),
            TurnComplete(response="final", usage={"input_tokens": 3, "output_tokens": 5}),
        ]
    )
    driver, session = _session(tmp_path, executor)
    observer = _Observer()

    result = session.run_turn(
        AgentTurnRequest(
            "answer",
            instructions="system",
            output_schema=JudgeResponse,
        ),
        observer,
    )

    assert result.text == "final"
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 5
    assert result.provider_session_id == "provider-session"
    messages, tools, instructions, config, environment = executor.calls[0]
    assert messages[0]["content"].startswith("answer")
    assert "JudgeResponse" in messages[0]["content"]
    assert tools == [{"name": "sys_os_read"}]
    assert instructions == "system"
    assert config.extra["reasoning_effort"] == "high"
    assert environment is None
    assert "DRIVER_TEST" not in os.environ
    assert [event.kind.value for event in observer.events] == [
        "text",
        "tool_call",
        "tool_result",
        "usage",
    ]
    driver.close()


def test_tool_call_complete_attaches_typed_result_payloads(tmp_path: Path) -> None:
    executor = _FakeExecutor(
        [
            ToolCallComplete(name="query", result={"rows": [1, 2]}, duration_ms=250),
            ToolCallComplete(name="list", result=["a", "b"], duration_ms=100),
            ToolCallComplete(name="shell", result="stdout text", error="boom", duration_ms=500),
            TurnComplete(response="final"),
        ]
    )
    driver, session = _session(tmp_path, executor)
    observer = _Observer()

    session.run_turn(AgentTurnRequest("go"), observer)
    driver.close()

    results = [event.payload for event in observer.events if event.kind.value == "tool_result"]
    assert [payload["tool"] for payload in results] == ["query", "list", "shell"]

    json_payload = results[0]["result_payload"]
    assert isinstance(json_payload, JsonResultPayload)
    assert json_payload.value == {"rows": [1, 2]}
    # content stays the flattened string even when the payload is typed JSON.
    assert results[0]["stdout"] == str({"rows": [1, 2]})

    list_payload = results[1]["result_payload"]
    assert isinstance(list_payload, JsonResultPayload)
    assert list_payload.value == ["a", "b"]

    command_payload = results[2]["result_payload"]
    assert command_payload == CommandResultPayload(
        stdout="stdout text",
        stderr="boom",
        exit_code=None,
        duration=0.5,
    )


def test_turn_complete_response_wins_and_chunks_are_fallback(tmp_path: Path) -> None:
    final_executor = _FakeExecutor([TextChunk("chunk"), TurnComplete(response="final")])
    final_driver, final_session = _session(tmp_path, final_executor)
    assert final_session.run_turn(AgentTurnRequest("one")).text == "final"
    final_driver.close()

    chunk_executor = _FakeExecutor([TextChunk("one "), TextChunk("two")])
    chunk_driver, chunk_session = _session(tmp_path, chunk_executor)
    assert chunk_session.run_turn(AgentTurnRequest("two")).text == "one two"
    chunk_driver.close()


def test_timeout_poisons_session_and_cleanup_is_idempotent(tmp_path: Path) -> None:
    executor = _FakeExecutor([], delay=0.1)
    driver, session = _session(tmp_path, executor)

    with pytest.raises(TimeoutError):
        session.run_turn(AgentTurnRequest("slow", timeout=timedelta(milliseconds=1)))
    with pytest.raises(RuntimeError, match="must be reset"):
        session.run_turn(AgentTurnRequest("again"))

    session.close()
    session.close()
    driver.close()
    assert executor.close_calls == 1


def test_session_cleanup_closes_owned_os_environments(tmp_path: Path) -> None:
    class Resource:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    executor = _FakeExecutor([])
    resource = Resource()
    driver = OmnigentDriver()
    with patch.object(
        driver,
        "_build_executor",
        return_value=(executor, [], None, SimpleNamespace(close=resource.close)),
    ):
        driver.create_session(_spec(tmp_path))

    driver.close()

    assert resource.close_calls == 1


def test_close_cancels_an_active_turn_from_another_thread(tmp_path: Path) -> None:
    started = threading.Event()
    finalized = threading.Event()

    class NeverEndingExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    finalized.set()
                if False:
                    yield None

            return stream()

    executor = NeverEndingExecutor([])
    driver, session = _session(tmp_path, executor)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(session.run_turn, AgentTurnRequest("work forever"))
        assert started.wait(timeout=2)
        session.close()
        with pytest.raises(concurrent.futures.CancelledError):
            turn.result(timeout=2)

    assert finalized.is_set()
    driver.close()
    assert executor.close_calls == 1


def test_cancel_stops_an_active_turn_without_closing_the_session(tmp_path: Path) -> None:
    started = threading.Event()
    finalized = threading.Event()

    class NeverEndingExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    finalized.set()
                if False:
                    yield None

            return stream()

    executor = NeverEndingExecutor([])
    driver, session = _session(tmp_path, executor)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(session.run_turn, AgentTurnRequest("work forever"))
        assert started.wait(timeout=2)
        session.cancel()
        session.cancel()  # idempotent once the turn is gone
        with pytest.raises(concurrent.futures.CancelledError):
            turn.result(timeout=2)

    assert finalized.is_set()
    # Unlike close(), cancel leaves the session's resources alone.
    assert executor.close_calls == 0
    session.close()
    driver.close()
    assert executor.close_calls == 1


def test_close_waits_for_turn_cancellation_cleanup_before_executor_close(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    executor_closed = threading.Event()

    class CleanupExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cleanup_started.set()
                    await asyncio.to_thread(release_cleanup.wait, 2)
                    cleanup_finished.set()
                if False:
                    yield None

            return stream()

        async def close(self) -> None:
            assert cleanup_finished.is_set()
            executor_closed.set()
            await super().close()

    executor = CleanupExecutor([])
    driver, session = _session(tmp_path, executor)
    turn_thread = threading.Thread(target=lambda: _ignore_cancelled_turn(session))
    close_thread = threading.Thread(target=session.close)
    turn_thread.start()
    assert started.wait(timeout=2)

    close_thread.start()
    assert cleanup_started.wait(timeout=2)
    assert not executor_closed.wait(timeout=0.05)
    release_cleanup.set()
    close_thread.join(timeout=2)
    turn_thread.join(timeout=2)

    assert not close_thread.is_alive()
    assert not turn_thread.is_alive()
    assert executor_closed.is_set()
    driver.close()
    assert executor.close_calls == 1


@pytest.mark.parametrize("close_target", ["session", "driver"])
def test_close_from_observer_rejects_without_deadlocking_loop(
    tmp_path: Path,
    close_target: str,
) -> None:
    executor = _FakeExecutor([TextChunk("event")])
    driver, session = _session(tmp_path, executor)

    class ClosingObserver:
        def on_event(self, event: AgentEvent) -> None:
            del event
            if close_target == "session":
                session.close()
            else:
                driver.close()

    with pytest.raises(RuntimeError, match="event-loop thread"):
        session.run_turn(AgentTurnRequest("trigger callback"), ClosingObserver())

    assert executor.close_calls == 0
    driver.close()
    assert executor.close_calls == 1


def test_driver_close_drains_blocking_default_executor_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_subject, "_SESSION_SHUTDOWN_TIMEOUT", 0.01)
    worker_started = threading.Event()
    finalized = threading.Event()
    release_worker = threading.Event()
    close_finished = threading.Event()
    resource_closed = threading.Event()

    class ThreadedExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                def block() -> None:
                    worker_started.set()
                    release_worker.wait(timeout=2)
                    finalized.set()

                await asyncio.to_thread(block)
                if False:
                    yield None

            return stream()

    class Resources:
        def close(self) -> None:
            assert finalized.is_set()
            resource_closed.set()

    executor = ThreadedExecutor([])
    resources = Resources()
    driver = OmnigentDriver()
    with patch.object(
        driver,
        "_build_executor",
        return_value=(executor, [], None, resources),
    ):
        session = driver.create_session(_spec(tmp_path))
    turn_thread = threading.Thread(target=lambda: _ignore_cancelled_turn(session))
    turn_thread.start()
    assert worker_started.wait(timeout=2)
    close_thread = threading.Thread(target=lambda: (driver.close(), close_finished.set()))
    close_thread.start()

    assert not close_finished.wait(timeout=0.05)
    release_worker.set()
    close_thread.join(timeout=2)
    turn_thread.join(timeout=2)

    assert close_finished.is_set()
    assert not close_thread.is_alive()
    assert not turn_thread.is_alive()
    assert resource_closed.is_set()
    assert executor.close_calls == 1


@pytest.mark.parametrize("failure", ["start", "ready"])
def test_driver_runtime_setup_failure_closes_loop(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(driver_subject, "_SESSION_SHUTDOWN_TIMEOUT", 0.01)
    created_loops: list[asyncio.AbstractEventLoop] = []
    new_event_loop = asyncio.new_event_loop

    def capture_loop() -> asyncio.AbstractEventLoop:
        loop = new_event_loop()
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(driver_subject.asyncio, "new_event_loop", capture_loop)
    if failure == "start":

        def fail_start(_thread: threading.Thread) -> None:
            _failure_message = "thread start failed"
            raise RuntimeError(_failure_message)  # test sentinel

        monkeypatch.setattr(driver_subject.threading.Thread, "start", fail_start)
        error = "thread start failed"
    else:
        monkeypatch.setattr(OmnigentAsyncRuntime, "_serve", lambda _self: None)
        error = "event loop did not start"

    with pytest.raises(RuntimeError, match=error):
        OmnigentDriver()

    assert len(created_loops) == 1
    assert created_loops[0].is_closed()


def _ignore_cancelled_turn(session: OmnigentSession) -> None:
    with contextlib.suppress(concurrent.futures.CancelledError):
        session.run_turn(AgentTurnRequest("use a worker"))


def test_concurrent_session_close_callers_receive_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, session = _session(tmp_path, _FakeExecutor([]))
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def fail_cleanup(_active: concurrent.futures.Future[Any] | None) -> None:
        cleanup_started.set()
        assert release_cleanup.wait(timeout=2)
        raise KeyboardInterrupt

    monkeypatch.setattr(session, "_close_resources", fail_cleanup)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(session.close)
        assert cleanup_started.wait(timeout=2)
        waiter = pool.submit(session.close)
        release_cleanup.set()
        for future in (owner, waiter):
            with pytest.raises(KeyboardInterrupt):
                future.result(timeout=2)

    with pytest.raises(KeyboardInterrupt):
        session.close()
    with pytest.raises(KeyboardInterrupt):
        driver.close()


def test_independent_sessions_overlap_on_one_driver_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    active_lock = threading.Lock()
    active = 0
    maximum_active = 0
    runtime_threads: set[int] = set()

    class OverlappingExecutor(_FakeExecutor):
        def __init__(self, answer: str) -> None:
            super().__init__([])
            self.answer = answer

        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                nonlocal active, maximum_active
                runtime_threads.add(threading.get_ident())
                with active_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    await asyncio.to_thread(barrier.wait, 2)
                    await asyncio.sleep(0)
                    yield TurnComplete(response=self.answer)
                finally:
                    with active_lock:
                        active -= 1

            return stream()

    driver = OmnigentDriver()
    executors = [OverlappingExecutor("optimizer"), OverlappingExecutor("chat")]
    remaining = iter(executors)
    monkeypatch.setattr(
        driver,
        "_build_executor",
        lambda _spec: (next(remaining), [], None, None),
    )
    sessions = [driver.create_session(_spec(tmp_path)) for _ in executors]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(session.run_turn, AgentTurnRequest("work")) for session in sessions]
        results = [future.result(timeout=3) for future in futures]

    assert [result.text for result in results] == ["optimizer", "chat"]
    assert maximum_active == 2
    assert active == 0
    assert len(runtime_threads) == 1

    driver.close()
    assert [executor.close_calls for executor in executors] == [1, 1]
    with pytest.raises(RuntimeError, match="closed"):
        driver.run_awaitable(asyncio.sleep(0))


def test_turns_in_one_session_run_in_submission_order(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    observed_messages: list[str] = []

    class OrderedExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                message = messages[0]["content"]
                assert isinstance(message, str)
                observed_messages.append(message)
                if message == "first":
                    first_started.set()
                    await asyncio.to_thread(release_first.wait, 2)
                yield TurnComplete(response=message)

            return stream()

    driver, session = _session(tmp_path, OrderedExecutor([]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.run_turn, AgentTurnRequest("first"))
        assert first_started.wait(timeout=2)
        second = pool.submit(session.run_turn, AgentTurnRequest("second"))
        assert observed_messages == ["first"]
        release_first.set()
        assert first.result(timeout=2).text == "first"
        assert second.result(timeout=2).text == "second"

    assert observed_messages == ["first", "second"]
    driver.close()


def test_close_racing_a_queued_turn_cancels_active_and_rejects_queued(
    tmp_path: Path,
) -> None:
    first_started = threading.Event()
    starts = 0

    class BlockingExecutor(_FakeExecutor):
        def run_turn(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            instructions: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[object]:
            del messages, tools, instructions, config

            async def stream() -> AsyncIterator[object]:
                nonlocal starts
                starts += 1
                first_started.set()
                await asyncio.Future()
                if False:
                    yield None

            return stream()

    executor = BlockingExecutor([])
    driver, session = _session(tmp_path, executor)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(session.run_turn, AgentTurnRequest("active"))
        assert first_started.wait(timeout=2)
        queued = pool.submit(session.run_turn, AgentTurnRequest("queued"))
        session.close()
        with pytest.raises(concurrent.futures.CancelledError):
            active.result(timeout=2)
        with pytest.raises(RuntimeError, match="closed"):
            queued.result(timeout=2)

    assert starts == 1
    driver.close()


def test_driver_close_continues_after_cleanup_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCloseExecutor(_FakeExecutor):
        async def close(self) -> None:
            self.close_calls += 1
            raise KeyboardInterrupt

    driver = OmnigentDriver()
    executors = [FailingCloseExecutor([]), _FakeExecutor([])]
    remaining = iter(executors)
    monkeypatch.setattr(
        driver,
        "_build_executor",
        lambda _spec: (next(remaining), [], None, None),
    )
    for _ in executors:
        driver.create_session(_spec(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        driver.close()

    assert [executor.close_calls for executor in executors] == [1, 1]
    with pytest.raises(RuntimeError, match="closed"):
        driver.run_awaitable(asyncio.sleep(0))


def test_concurrent_driver_close_callers_receive_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _FakeExecutor([])
    driver, session = _session(tmp_path, executor)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    original_close = session.close

    def fail_close() -> None:
        cleanup_started.set()
        assert release_cleanup.wait(timeout=2)
        original_close()
        raise KeyboardInterrupt

    monkeypatch.setattr(session, "close", fail_close)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(driver.close)
        assert cleanup_started.wait(timeout=2)
        waiter = pool.submit(driver.close)
        release_cleanup.set()
        for future in (owner, waiter):
            with pytest.raises(KeyboardInterrupt):
                future.result(timeout=2)

    with pytest.raises(KeyboardInterrupt):
        driver.close()
    assert executor.close_calls == 1


def test_driver_close_waits_for_in_flight_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_started = threading.Event()
    release_build = threading.Event()
    close_started = [threading.Event(), threading.Event()]
    close_finished = [threading.Event(), threading.Event()]
    executor = _FakeExecutor([])
    driver = OmnigentDriver()

    def build(
        _spec: AgentSessionSpec,
    ) -> tuple[
        _FakeExecutor,
        list[dict[str, Any]],
        None,
        None,
    ]:
        build_started.set()
        assert release_build.wait(timeout=2)
        return executor, [], None, None

    monkeypatch.setattr(driver, "_build_executor", build)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        creation = pool.submit(driver.create_session, _spec(tmp_path))
        assert build_started.wait(timeout=2)

        def close(index: int) -> None:
            close_started[index].set()
            driver.close()
            close_finished[index].set()

        closing = pool.submit(close, 0)
        assert close_started[0].wait(timeout=2)
        second_closing = pool.submit(close, 1)
        assert close_started[1].wait(timeout=2)
        assert not close_finished[0].wait(timeout=0.05)
        assert not close_finished[1].wait(timeout=0.05)
        release_build.set()
        with pytest.raises(RuntimeError, match="closed"):
            creation.result(timeout=2)
        closing.result(timeout=2)
        second_closing.result(timeout=2)

    assert all(finished.is_set() for finished in close_finished)
    assert executor.close_calls == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {
                "policy": AgentExecutionPolicy(
                    host_resources=(HostResource(Path("model")),),
                )
            },
            "host-resource",
        ),
        (
            {
                "policy": AgentExecutionPolicy(
                    project_paths=ProjectPathPolicy(read_only_paths=("src/protected",)),
                )
            },
            "top-level",
        ),
        ({"policy": AgentExecutionPolicy(containerized=True)}, "container"),
    ],
)
def test_create_session_rejects_unsupported_requirements_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
    message: str,
) -> None:
    driver = OmnigentDriver()

    def build(_spec: AgentSessionSpec) -> tuple[Any, list[dict[str, Any]]]:
        pytest.fail("executor must not be built")

    monkeypatch.setattr(driver, "_build_executor", build)

    with pytest.raises(OmnigentDriverError, match=message):
        driver.create_session(_spec(tmp_path, **change))


def test_create_session_accepts_supported_top_level_project_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = OmnigentDriver()
    executor = _FakeExecutor([])
    monkeypatch.setattr(
        driver,
        "_build_executor",
        lambda _spec: (executor, [], None, None),
    )
    policy = ProjectPathPolicy(
        read_only_paths=(".git", ".vibesys"),
        hidden_paths=(".env",),
    )

    session = driver.create_session(
        _spec(tmp_path, policy=AgentExecutionPolicy(project_paths=policy))
    )

    session.close()
    driver.close()


def test_create_session_accepts_non_dot_hidden_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = OmnigentDriver()
    executor = _FakeExecutor([])
    monkeypatch.setattr(
        driver,
        "_build_executor",
        lambda _spec: (executor, [], None, None),
    )
    policy = ProjectPathPolicy(hidden_paths=("agent.toml",))

    session = driver.create_session(
        _spec(tmp_path, policy=AgentExecutionPolicy(project_paths=policy))
    )

    session.close()
    driver.close()


def test_driver_owns_session_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _FakeExecutor([])
    driver = OmnigentDriver()
    monkeypatch.setattr(
        driver,
        "_build_executor",
        lambda _spec: (executor, [], None, None),
    )

    driver.create_session(_spec(tmp_path))
    driver.close()
    driver.close()

    assert executor.close_calls == 1


def test_missing_private_tool_executor_seam_fails_during_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExecutorWithoutSeam:
        close_calls = 0

        def __init__(self, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            self.close_calls += 1

    driver = OmnigentDriver()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: ExecutorWithoutSeam)
    monkeypatch.setattr(driver, "_build_os_env", lambda _workspace, **_kwargs: object())

    with pytest.raises(OmnigentDriverError, match="_tool_executor"):
        driver.create_session(_spec(tmp_path))
    driver.close()


def test_os_policy_is_always_sandboxed_and_workspace_scoped(tmp_path: Path) -> None:
    driver = OmnigentDriver()

    spec = driver._build_os_env(_spec(tmp_path))  # noqa: SLF001  # LW-010006; inspects the generated sandbox contract that has no public projection

    assert spec.sandbox is not None
    assert spec.sandbox.type != "none"
    assert spec.sandbox.read_paths is None
    assert spec.sandbox.write_paths == [str(tmp_path)]
    assert spec.cwd == str(tmp_path)


def test_os_policy_exposes_control_dotdirs_and_keeps_hidden_dotfiles_masked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".vibesys").mkdir()
    (tmp_path / ".codex-tmp").mkdir()
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / "agent.toml").write_text("secret", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secret").write_text("secret", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")
    toolchain = tmp_path.parent / "toolchain"
    toolchain.mkdir(exist_ok=True)
    policy = ProjectPathPolicy(
        read_only_paths=(".git", ".vibesys"),
        hidden_paths=(".env", "agent.toml", "config/secret"),
    )
    driver = OmnigentDriver()
    monkeypatch.setattr(
        "vs_agent.drivers.omnigent.declare_active_rust_toolchain_resources",
        lambda *_args, **_kwargs: (HostResource(toolchain, purpose="Rust toolchain"),),
    )

    spec = driver._build_os_env(  # noqa: SLF001  # LW-010007; inspects masked paths and toolchain grants in the internal sandbox spec
        _spec(tmp_path, policy=AgentExecutionPolicy(project_paths=policy)),
        env_passthrough=("CARGO_HOME",),
        include_toolchain=True,
    )

    assert spec.sandbox is not None
    assert spec.sandbox.write_paths == [str(tmp_path)]
    assert spec.sandbox.write_files is None
    assert spec.sandbox.read_paths == [str(toolchain)]
    assert spec.sandbox.env_passthrough == ["CARGO_HOME"]
    assert set(spec.sandbox.cwd_allow_hidden or ()) == {".git", ".vibesys"}
    assert spec.sandbox.cwd_hidden_scan_recursive is False
    assert spec.sandbox.cwd_hidden_scan_overflow == "error"
    assert spec.sandbox.mask_paths == [
        ".codex-tmp",
        ".env",
        "agent.toml",
        "config/secret",
    ]


@requires_sandbox_backend
def test_os_policy_masks_declared_non_dot_and_nested_paths(tmp_path: Path) -> None:
    (tmp_path / "public.txt").write_text("public-4417", encoding="utf-8")
    (tmp_path / "agent.toml").write_text("secret-9913", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secret").write_text("secret-7729", encoding="utf-8")
    policy = ProjectPathPolicy(hidden_paths=("agent.toml", "config/secret"))
    driver = OmnigentDriver()
    spec = driver._build_os_env(  # noqa: SLF001  # LW-010008; obtains the sandbox spec required to test denied reads against the real OS tool
        _spec(tmp_path, policy=AgentExecutionPolicy(project_paths=policy))
    )
    os_tools = _build_os_tools(spec, tmp_path)

    try:
        public = asyncio.run(os_tools.dispatch("sys_os_read", {"path": "public.txt"}))
        hidden = asyncio.run(os_tools.dispatch("sys_os_read", {"path": "agent.toml"}))
        nested = asyncio.run(os_tools.dispatch("sys_os_read", {"path": "config/secret"}))
    finally:
        os_tools.close()

    assert "public-4417" in str(public)
    assert "secret-9913" not in str(hidden)
    assert "error" in str(hidden).lower()
    assert "secret-7729" not in str(nested)
    assert "error" in str(nested).lower()


@requires_sandbox_backend
def test_real_os_shell_receives_explicit_environment_without_changing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "VIBESYS_TEST_REAL_HELPER_ENV"
    monkeypatch.setenv(key, "ambient")
    driver = OmnigentDriver()
    spec = driver._build_os_env(  # noqa: SLF001  # LW-010009; configures the internal sandbox spec to verify explicit child environment isolation
        _spec(tmp_path),
        env_passthrough=(key,),
    )
    os_tools = _build_os_tools(spec, tmp_path, {key: "session"})

    try:
        result = asyncio.run(os_tools.dispatch("sys_os_shell", {"command": f'printf %s "${key}"'}))
    finally:
        os_tools.close()
        driver.close()

    assert isinstance(result, str)
    assert json.loads(result)["stdout"] == "session"
    assert os.environ[key] == "ambient"


def test_codex_executor_disables_native_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Executor(_FakeExecutor):
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__([])
            self._env = {"BASE": "preserved"}
            captured["provider_environment"] = self._env

    driver = OmnigentDriver()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: Executor)
    monkeypatch.setattr(driver, "_build_os_env", lambda _spec, **_kwargs: object())
    rust_sysroot = tmp_path / "rust"
    target_libdir = rust_sysroot / "lib" / "rustlib" / "x86_64-unknown-linux-gnu" / "lib"
    monkeypatch.setattr(
        "vs_agent.drivers.omnigent.resolve_active_rust_toolchain",
        lambda _context, **_kwargs: (rust_sysroot, target_libdir),
    )

    def build_tools(
        _os_env: object,
        _workspace: Path,
        environment: dict[str, str],
        _shell_os_env: object,
    ) -> SimpleNamespace:
        captured["tool_environment"] = environment
        return SimpleNamespace(
            schemas=[],
            handles=lambda _name: False,
            dispatch=lambda _name, _args: None,
            close=lambda: None,
        )

    monkeypatch.setattr(
        "vs_agent.drivers.omnigent._build_os_tools",
        build_tools,
    )

    driver.create_session(_spec(tmp_path, environment=(("DRIVER_TEST", "session"),)))

    assert captured["disable_native_tools"] is True
    assert captured["provider_environment"] == {"BASE": "preserved", "DRIVER_TEST": "session"}
    assert str(captured["tool_environment"]["PATH"]).split(os.pathsep)[0] == str(
        rust_sysroot / "bin"
    )
    cargo_home = Path(captured["tool_environment"]["CARGO_HOME"])
    assert cargo_home.name == "cargo-home"
    assert cargo_home.parent.is_dir()
    assert not cargo_home.is_relative_to(tmp_path)
    assert "CARGO_TARGET_DIR" not in captured["tool_environment"]
    assert not any(key.endswith("_LINKER") for key in captured["tool_environment"])
    driver.close()
    assert not cargo_home.parent.exists()


@pytest.mark.parametrize(
    ("provider", "harness", "environment_attribute"),
    [("claude", "claude-sdk", "_extra_env"), ("codex", "codex", "_env")],
)
def test_executor_routes_only_declared_os_and_mcp_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    harness: str,
    environment_attribute: str,
) -> None:
    dispatched: list[tuple[str, str, dict[str, Any]]] = []

    class Executor(_FakeExecutor):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__([])
            setattr(self, environment_attribute, {})
            executors.append(self)

    executors: list[Executor] = []

    class MCPTools:
        schemas: ClassVar[list[dict[str, Any]]] = [{"name": "profiler__analyze", "parameters": {}}]
        close_calls = 0
        initialize_calls = 0

        @staticmethod
        def handles(name: str) -> bool:
            return name == "profiler__analyze"

        async def dispatch(self, name: str, args: dict[str, Any]) -> str:
            dispatched.append(("mcp", name, args))
            return "mcp result"

        async def initialize(self) -> None:
            self.initialize_calls += 1

        async def close(self) -> None:
            self.close_calls += 1

    mcp_tools = MCPTools()

    class MCPFactory:
        @staticmethod
        def build(**kwargs: object) -> MCPTools:
            assert kwargs["harness"] == harness
            return mcp_tools

    async def dispatch_os(name: str, args: dict[str, Any]) -> str:
        dispatched.append(("os", name, args))
        return "os result"

    driver = OmnigentDriver()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: Executor)
    monkeypatch.setattr(driver, "_build_os_env", lambda _spec, **_kwargs: object())
    monkeypatch.setattr(driver_subject, "resolve_active_rust_toolchain", lambda *_a, **_k: None)
    monkeypatch.setattr(driver_subject, "_OmnigentMCPTools", MCPFactory)
    monkeypatch.setattr(
        driver_subject,
        "_build_os_tools",
        lambda *_args, **_kwargs: SimpleNamespace(
            schemas=[{"name": "sys_os_read", "parameters": {}}],
            handles=lambda name: name == "sys_os_read",
            dispatch=dispatch_os,
            close=lambda: None,
        ),
    )

    session = driver.create_session(
        _spec(
            tmp_path,
            provider=provider,
            mcp_servers=(MCPServerSpec("profiler", "python"),),
        )
    )
    executor = executors[0]
    session.run_turn(AgentTurnRequest("exercise tool surface"))
    schemas = executor.calls[0][1]
    tool_executor = executor._tool_executor  # noqa: SLF001  # LW-010035; the pinned provider dispatch seam has no public invocation path in the fake executor.
    assert callable(tool_executor)

    assert mcp_tools.initialize_calls == 1
    assert [schema["name"] for schema in schemas] == ["sys_os_read", "profiler__analyze"]
    assert asyncio.run(tool_executor("sys_os_read", {"path": "x"})) == "os result"
    assert asyncio.run(tool_executor("profiler__analyze", {"pid": 1})) == "mcp result"
    assert asyncio.run(tool_executor("undeclared", {})) == {"error": "unknown tool 'undeclared'"}
    assert dispatched == [
        ("os", "sys_os_read", {"path": "x"}),
        ("mcp", "profiler__analyze", {"pid": 1}),
    ]

    driver.close()
    assert mcp_tools.close_calls == 1


def test_tool_schema_conflict_attempts_all_cleanup_and_preserves_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor: _FakeExecutor | None = None

    class Executor(_FakeExecutor):
        def __init__(self, **_kwargs: object) -> None:
            nonlocal executor
            super().__init__([])
            self._env: dict[str, str] = {}
            executor = self

    class MCPTools:
        schemas: ClassVar[list[dict[str, Any]]] = [{"name": "sys_os_read", "parameters": {}}]
        close_calls = 0

        async def initialize(self) -> None:
            pass

        @staticmethod
        def handles(_name: str) -> bool:
            return False

        async def close(self) -> None:
            self.close_calls += 1
            _failure_message = "MCP cleanup failed"
            raise RuntimeError(_failure_message)

    mcp_tools = MCPTools()

    class MCPFactory:
        @staticmethod
        def build(**_kwargs: object) -> MCPTools:
            return mcp_tools

    class Resource:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    resource = Resource()
    driver = OmnigentDriver()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: Executor)
    monkeypatch.setattr(driver, "_build_os_env", lambda _spec, **_kwargs: object())
    monkeypatch.setattr(driver_subject, "resolve_active_rust_toolchain", lambda *_a, **_k: None)
    monkeypatch.setattr(driver_subject, "_OmnigentMCPTools", MCPFactory)
    monkeypatch.setattr(
        driver_subject,
        "_build_os_tools",
        lambda *_args, **_kwargs: SimpleNamespace(
            schemas=[{"name": "sys_os_read", "parameters": {}}],
            handles=lambda name: name == "sys_os_read",
            dispatch=lambda *_args: None,
            close=resource.close,
        ),
    )

    with pytest.raises(OmnigentDriverError, match="conflict") as caught:
        driver.create_session(_spec(tmp_path, mcp_servers=(MCPServerSpec("profiler", "python"),)))

    assert any("MCP cleanup also failed" in note for note in caught.value.__notes__)
    assert mcp_tools.close_calls == 1
    assert executor is not None
    assert executor.close_calls == 1
    assert resource.close_calls == 1
    driver.close()


def test_interrupted_mcp_initialization_keeps_owner_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Executor:
        close_calls = 0

        def __init__(self, **_kwargs: object) -> None:
            self._env: dict[str, str] = {}
            self._tool_executor = None

        def close(self) -> None:
            self.close_calls += 1

    class MCPTools:
        schemas: ClassVar[list[dict[str, Any]]] = []
        close_calls = 0

        async def initialize(self) -> None:
            await asyncio.Future()

        async def close(self) -> None:
            self.close_calls += 1

    mcp_tools = MCPTools()

    class MCPFactory:
        @staticmethod
        def build(**_kwargs: object) -> MCPTools:
            return mcp_tools

    resource = SimpleNamespace(close_calls=0)

    def close_resource() -> None:
        resource.close_calls += 1

    resource.close = close_resource
    driver = OmnigentDriver()
    executor = Executor()
    monkeypatch.setattr(driver, "_executor_class", lambda _spec: lambda **_kwargs: executor)
    monkeypatch.setattr(driver, "_build_os_env", lambda _spec, **_kwargs: object())
    monkeypatch.setattr(driver_subject, "resolve_active_rust_toolchain", lambda *_a, **_k: None)
    monkeypatch.setattr(driver_subject, "_OmnigentMCPTools", MCPFactory)
    monkeypatch.setattr(
        driver_subject,
        "_build_os_tools",
        lambda *_args, **_kwargs: SimpleNamespace(
            schemas=[],
            handles=lambda _name: False,
            dispatch=lambda *_args: None,
            close=close_resource,
        ),
    )
    run_calls = 0

    def interrupt_first(awaitable: Coroutine[Any, Any, Any]) -> object:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 1:
            awaitable.close()
            raise KeyboardInterrupt
        return asyncio.run(awaitable)

    monkeypatch.setattr(driver, "run_awaitable", interrupt_first)

    with pytest.raises(KeyboardInterrupt):
        driver.create_session(_spec(tmp_path, mcp_servers=(MCPServerSpec("profiler", "python"),)))

    assert mcp_tools.close_calls == 1
    assert executor.close_calls == 1
    assert resource.close_calls == 1
    driver.close()
