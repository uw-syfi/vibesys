"""Contract tests for VibeSys's native Omnigent MCP adapter."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from vs_agent.api import MCPServerSpec
from vs_agent.drivers import _omnigent_mcp as subject
from vs_agent.drivers._omnigent_mcp import OmnigentMCPError, OmnigentMCPTools

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@dataclass
class _FakeMCPServerConfig:
    name: str
    transport: str
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class _FakeAgentSpec:
    spec_version: int
    name: str
    executor: _FakeExecutorSpec
    mcp_servers: list[_FakeMCPServerConfig]


@dataclass
class _FakeExecutorSpec:
    config: dict[str, Any]


class _FakeManager:
    instances: ClassVar[list[_FakeManager]] = []
    schemas: ClassVar[list[dict[str, Any]]] = [
        {
            "type": "function",
            "name": "profiler__analyze",
            "description": "Analyze a profile",
            "parameters": {"type": "object"},
        }
    ]
    tool_names: ClassVar[set[str]] = {"profiler__analyze"}
    failures: ClassVar[dict[str, str]] = {}

    def __init__(self, *, stdio_cwd: object) -> None:
        self.stdio_cwd = stdio_cwd
        self.specs: list[_FakeAgentSpec] = []
        self.calls: list[tuple[Any, ...]] = []
        self.shutdown_calls = 0
        self.shutdown_override: Callable[[], Awaitable[None]] | None = None
        self.instances.append(self)

    async def schemas_for(self, spec: _FakeAgentSpec) -> SimpleNamespace:
        self.specs.append(spec)
        return SimpleNamespace(
            schemas=list(self.schemas),
            tool_names=set(self.tool_names),
            failures=dict(self.failures),
        )

    async def call_tool(
        self,
        spec: _FakeAgentSpec,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
    ) -> str:
        self.calls.append((spec, name, arguments, session_id))
        return "profile result"

    async def shutdown(self) -> None:
        if self.shutdown_override is not None:
            await self.shutdown_override()
            return
        self.shutdown_calls += 1


def _native_api(
    manager: type[_FakeManager] = _FakeManager,
    *,
    validation: SimpleNamespace | None = None,
) -> SimpleNamespace:
    validation_result = validation or SimpleNamespace(valid=True, errors=[])
    return SimpleNamespace(
        agent_spec=_FakeAgentSpec,
        executor_spec=_FakeExecutorSpec,
        server_config=_FakeMCPServerConfig,
        manager=manager,
        validate=lambda _spec: validation_result,
    )


@pytest.fixture(autouse=True)
def fake_native_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeManager.instances.clear()
    _FakeManager.schemas = [
        {
            "type": "function",
            "name": "profiler__analyze",
            "description": "Analyze a profile",
            "parameters": {"type": "object"},
        }
    ]
    _FakeManager.tool_names = {"profiler__analyze"}
    _FakeManager.failures = {}
    monkeypatch.setattr(
        subject,
        "_native_mcp_api",
        _native_api,
    )


def _servers() -> tuple[MCPServerSpec, ...]:
    return (
        MCPServerSpec(
            name="profiler",
            command="python",
            args=("-m", "vibesys.macos_cpu_profiler"),
            env=(("PROFILE_DIR", "/profiles"),),
        ),
        MCPServerSpec(
            name="issues",
            command="uvx",
            args=("issue-board", "serve"),
            env=(("BOARD", "local"),),
        ),
    )


def test_create_translates_multiple_servers_and_preserves_native_schemas(tmp_path: Path) -> None:
    tools = asyncio.run(
        OmnigentMCPTools.create(
            servers=_servers(),
            workspace=tmp_path,
            harness="codex",
            session_id=lambda: "session-1",
        )
    )

    assert tools is not None
    manager = _FakeManager.instances[0]
    assert manager.stdio_cwd == tmp_path
    assert manager.specs[0].mcp_servers == [
        _FakeMCPServerConfig(
            name="profiler",
            transport="stdio",
            command=sys.executable,
            args=["-m", "vibesys.macos_cpu_profiler"],
            env={"PROFILE_DIR": "/profiles"},
        ),
        _FakeMCPServerConfig(
            name="issues",
            transport="stdio",
            command="uvx",
            args=["issue-board", "serve"],
            env={"BOARD": "local"},
        ),
    ]
    assert tools.schemas == _FakeManager.schemas
    assert tools.handles("profiler__analyze")
    assert not tools.handles("sys_os_read")


def test_dispatch_uses_native_manager_and_current_provider_session_id(tmp_path: Path) -> None:
    session_id = ["provider-session-1"]
    tools = asyncio.run(
        OmnigentMCPTools.create(
            servers=_servers()[:1],
            workspace=tmp_path,
            harness="claude-sdk",
            session_id=lambda: session_id[0],
        )
    )
    assert tools is not None
    session_id[0] = "provider-session-2"

    result = asyncio.run(tools.dispatch("profiler__analyze", {"pid": 42}))

    manager = _FakeManager.instances[0]
    assert result == "profile result"
    assert manager.calls == [
        (manager.specs[0], "profiler__analyze", {"pid": 42}, "provider-session-2")
    ]


def test_connection_failure_closes_manager_without_exposing_server_environment(
    tmp_path: Path,
) -> None:
    _FakeManager.failures = {"profiler": "connection refused"}
    sensitive_value = "must-not-appear"
    server = MCPServerSpec(
        name="profiler",
        command="server",
        env=(("API_TOKEN", sensitive_value),),
    )

    with pytest.raises(OmnigentMCPError, match="profiler: connection refused") as caught:
        asyncio.run(
            OmnigentMCPTools.create(
                servers=(server,),
                workspace=tmp_path,
                harness="codex",
                session_id=lambda: None,
            )
        )

    assert sensitive_value not in str(caught.value)
    assert _FakeManager.instances[0].shutdown_calls == 1


def test_cancelled_discovery_closes_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class BlockingManager(_FakeManager):
        async def schemas_for(self, spec: _FakeAgentSpec) -> SimpleNamespace:
            self.specs.append(spec)
            started.set()
            return await asyncio.Future[SimpleNamespace]()

    monkeypatch.setattr(
        subject,
        "_native_mcp_api",
        lambda: _native_api(BlockingManager),
    )

    async def cancel_setup() -> None:
        setup = asyncio.create_task(
            OmnigentMCPTools.create(
                servers=_servers()[:1],
                workspace=tmp_path,
                harness="codex",
                session_id=lambda: None,
            )
        )
        await started.wait()
        setup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await setup

    asyncio.run(cancel_setup())

    assert BlockingManager.instances[-1].shutdown_calls == 1


def test_close_is_idempotent_and_closed_tools_cannot_dispatch(tmp_path: Path) -> None:
    async def exercise() -> tuple[OmnigentMCPTools, _FakeManager]:
        tools = await OmnigentMCPTools.create(
            servers=_servers()[:1],
            workspace=tmp_path,
            harness="codex",
            session_id=lambda: None,
        )
        assert tools is not None
        manager = _FakeManager.instances[0]
        await tools.close()
        await tools.close()
        return tools, manager

    tools, manager = asyncio.run(exercise())

    assert manager.shutdown_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(tools.dispatch("profiler__analyze", {}))


def test_duplicate_server_names_are_rejected_before_manager_creation(tmp_path: Path) -> None:
    duplicated = (
        MCPServerSpec(name="same", command="one"),
        MCPServerSpec(name="same", command="two"),
    )

    with pytest.raises(OmnigentMCPError, match="must be unique"):
        asyncio.run(
            OmnigentMCPTools.create(
                servers=duplicated,
                workspace=tmp_path,
                harness="codex",
                session_id=lambda: None,
            )
        )

    assert _FakeManager.instances == []


def test_native_validation_error_preserves_field_path_and_skips_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_error = SimpleNamespace(
        path="mcp_servers[0].name",
        message="tool name 'sys_os_read' collides with a reserved builtin tool name",
    )
    monkeypatch.setattr(
        subject,
        "_native_mcp_api",
        lambda: _native_api(validation=SimpleNamespace(valid=False, errors=[validation_error])),
    )

    with pytest.raises(
        OmnigentMCPError,
        match=r"mcp_servers\[0\]\.name: tool name 'sys_os_read' collides",
    ):
        asyncio.run(
            OmnigentMCPTools.create(
                servers=(MCPServerSpec(name="sys_os_read", command="server"),),
                workspace=tmp_path,
                harness="codex",
                session_id=lambda: None,
            )
        )

    assert _FakeManager.instances == []


def test_close_can_retry_after_shutdown_failure(tmp_path: Path) -> None:
    tools = asyncio.run(
        OmnigentMCPTools.create(
            servers=_servers()[:1],
            workspace=tmp_path,
            harness="codex",
            session_id=lambda: None,
        )
    )
    assert tools is not None
    manager = _FakeManager.instances[0]
    shutdown_attempts = 0

    async def flaky_shutdown() -> None:
        nonlocal shutdown_attempts
        shutdown_attempts += 1
        if shutdown_attempts == 1:
            _failure_message = "cleanup failed"
            raise RuntimeError(_failure_message)

    manager.shutdown_override = flaky_shutdown

    with pytest.raises(RuntimeError, match="cleanup failed"):
        asyncio.run(tools.close())
    asyncio.run(tools.close())

    assert shutdown_attempts == 2


def test_same_bare_tool_name_from_multiple_servers_stays_namespaced(tmp_path: Path) -> None:
    _FakeManager.schemas = [
        {"type": "function", "name": "one__status", "parameters": {}},
        {"type": "function", "name": "two__status", "parameters": {}},
    ]
    _FakeManager.tool_names = {"one__status", "two__status"}
    servers = (
        MCPServerSpec(name="one", command="server-one"),
        MCPServerSpec(name="two", command="server-two"),
    )

    tools = asyncio.run(
        OmnigentMCPTools.create(
            servers=servers,
            workspace=tmp_path,
            harness="codex",
            session_id=lambda: None,
        )
    )

    assert tools is not None
    assert [schema["name"] for schema in tools.schemas] == ["one__status", "two__status"]


_REAL_NATIVE_API = vars(subject)["_native_mcp_api"]


def test_real_native_api_exposes_the_pinned_omnigent_classes() -> None:
    pytest.importorskip("omnigent")

    api = _REAL_NATIVE_API()

    assert api.manager.__name__ == "RunnerMcpManager"
    assert api.agent_spec.__name__ == "AgentSpec"
    assert api.server_config.__name__ == "MCPServerConfig"
    assert callable(api.validate)


def test_missing_native_mcp_api_reports_import_detail_and_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "omnigent.runner.mcp_manager", None)

    with pytest.raises(
        OmnigentMCPError, match=r"not importable \((Import|ModuleNotFound)Error"
    ) as caught:
        _REAL_NATIVE_API()

    assert "uv sync" in str(caught.value)
    assert isinstance(caught.value.__cause__, ImportError)


def test_dependency_error_constructor_names_error_type_and_message() -> None:
    error = OmnigentMCPError.dependency_unavailable(ModuleNotFoundError("no omnigent"))

    assert str(error).startswith(
        "Omnigent MCP support is not importable (ModuleNotFoundError: no omnigent)."
    )


def _built(tmp_path: Path) -> OmnigentMCPTools:
    tools = OmnigentMCPTools.build(
        servers=_servers()[:1],
        workspace=tmp_path,
        harness="codex",
        session_id=lambda: None,
    )
    assert tools is not None
    return tools


def test_build_returns_none_without_servers(tmp_path: Path) -> None:
    assert (
        OmnigentMCPTools.build(
            servers=(), workspace=tmp_path, harness="codex", session_id=lambda: None
        )
        is None
    )


def test_lifecycle_misuse_is_rejected_with_specific_errors(tmp_path: Path) -> None:
    tools = _built(tmp_path)

    with pytest.raises(RuntimeError, match="not initialized"):
        asyncio.run(tools.dispatch("profiler__analyze", {}))
    asyncio.run(tools.initialize())
    with pytest.raises(RuntimeError, match="already initialized"):
        asyncio.run(tools.initialize())
    with pytest.raises(RuntimeError, match="do not contain 'other__tool'"):
        asyncio.run(tools.dispatch("other__tool", {}))

    closed = _built(tmp_path)
    asyncio.run(closed.close())
    with pytest.raises(RuntimeError, match="tools are closed"):
        asyncio.run(closed.initialize())


def test_failed_discovery_with_failed_cleanup_keeps_first_error_and_adds_note(
    tmp_path: Path,
) -> None:
    _FakeManager.failures = {"profiler": "connection refused"}
    tools = _built(tmp_path)

    async def failing_shutdown() -> None:
        message = "shutdown broke"
        raise OSError(message)

    _FakeManager.instances[0].shutdown_override = failing_shutdown

    with pytest.raises(OmnigentMCPError, match="connection refused") as caught:
        asyncio.run(tools.initialize())

    assert caught.value.__notes__ == ["Omnigent MCP cleanup also failed: shutdown broke"]
