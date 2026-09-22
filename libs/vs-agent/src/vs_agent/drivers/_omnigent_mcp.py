"""Native Omnigent MCP integration for one VibeSys agent session."""

# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.contracts import MCPServerSpec


class OmnigentMCPError(RuntimeError):
    """Omnigent could not initialize the requested MCP tool surface."""


@dataclass(frozen=True)
class _NativeMCPAPI:
    agent_spec: type[Any]
    executor_spec: type[Any]
    server_config: type[Any]
    manager: type[Any]
    validate: Callable[[Any], Any]


def _native_mcp_api() -> _NativeMCPAPI:
    """Load the pinned Omnigent MCP API only when the extra is selected."""
    try:
        from omnigent.runner.mcp_manager import RunnerMcpManager  # noqa: PLC0415
        from omnigent.spec import (  # noqa: PLC0415
            AgentSpec,
            ExecutorSpec,
            MCPServerConfig,
            validate,
        )
    except ImportError as exc:
        raise OmnigentMCPError(
            f"Omnigent MCP support is not importable ({type(exc).__name__}: {exc}). "
            "Reinstall dependencies with `uv sync` (omnigent is a base dependency)."
        ) from exc
    return _NativeMCPAPI(
        agent_spec=AgentSpec,
        executor_spec=ExecutorSpec,
        server_config=MCPServerConfig,
        manager=RunnerMcpManager,
        validate=validate,
    )


def _translate_servers(
    servers: tuple[MCPServerSpec, ...],
    server_config: type[Any],
) -> list[Any]:
    """Translate neutral stdio declarations into Omnigent configurations."""
    names = [server.name for server in servers]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise OmnigentMCPError(f"MCP server names must be unique: {duplicate_names}")

    return [
        server_config(
            name=server.name,
            transport="stdio",
            command=sys.executable if server.command in {"python", "python3"} else server.command,
            args=list(server.args),
            env=dict(server.env),
        )
        for server in servers
    ]


def _redact_environment_values(message: str, servers: tuple[MCPServerSpec, ...]) -> str:
    """Remove configured environment values from native diagnostics."""
    redacted = message
    for server in servers:
        for _, value in server.env:
            if value:
                redacted = redacted.replace(value, "<redacted>")
    return redacted


@dataclass
class OmnigentMCPTools:
    """Schemas, dispatch, and lifecycle for native session-scoped MCP tools."""

    schemas: list[dict[str, Any]]
    _tool_names: frozenset[str]
    _manager: Any
    _agent_spec: Any
    _session_id: Callable[[], str | None]
    _servers: tuple[MCPServerSpec, ...]
    _initialized: bool = False
    _closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def build(
        cls,
        *,
        servers: tuple[MCPServerSpec, ...],
        workspace: Path,
        harness: str,
        session_id: Callable[[], str | None],
    ) -> OmnigentMCPTools | None:
        """Build an owned native manager without starting MCP subprocesses."""
        if not servers:
            return None

        native = _native_mcp_api()
        agent_spec = native.agent_spec(
            spec_version=1,
            name="vibesys-session-mcp",
            executor=native.executor_spec(config={"harness": harness}),
            mcp_servers=_translate_servers(servers, native.server_config),
        )
        validation = native.validate(agent_spec)
        if not validation.valid:
            details = "; ".join(f"{error.path}: {error.message}" for error in validation.errors)
            raise OmnigentMCPError(
                "Invalid Omnigent MCP configuration: "
                f"{_redact_environment_values(details, servers)}"
            )

        manager = native.manager(stdio_cwd=workspace)
        return cls(
            schemas=[],
            _tool_names=frozenset(),
            _manager=manager,
            _agent_spec=agent_spec,
            _session_id=session_id,
            _servers=servers,
        )

    @classmethod
    async def create(
        cls,
        *,
        servers: tuple[MCPServerSpec, ...],
        workspace: Path,
        harness: str,
        session_id: Callable[[], str | None],
    ) -> OmnigentMCPTools | None:
        """Build and initialize native session-scoped MCP tools."""
        tools = cls.build(
            servers=servers,
            workspace=workspace,
            harness=harness,
            session_id=session_id,
        )
        if tools is not None:
            await tools.initialize()
        return tools

    async def initialize(self) -> None:
        """Connect servers and discover their namespaced native schemas."""
        if self._initialized:
            raise RuntimeError("Omnigent MCP tools are already initialized")
        if self._closed:
            raise RuntimeError("Omnigent MCP tools are closed")
        try:
            result = await self._manager.schemas_for(self._agent_spec)
            if result.failures:
                details = "; ".join(
                    f"{name}: {error}" for name, error in sorted(result.failures.items())
                )
                raise OmnigentMCPError(  # noqa: TRY301
                    "Omnigent could not connect MCP servers: "
                    f"{_redact_environment_values(details, self._servers)}"
                )
            self.schemas = list(result.schemas)
            self._tool_names = frozenset(result.tool_names)
            self._initialized = True
        except BaseException as error:
            try:
                await self.close()
            except BaseException as cleanup_error:  # noqa: BLE001
                error.add_note(f"Omnigent MCP cleanup also failed: {cleanup_error}")
            raise

    def handles(self, name: str) -> bool:
        """Return whether ``name`` belongs to this session's MCP surface."""
        return name in self._tool_names

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one namespaced MCP tool through Omnigent's native manager."""
        if self._closed:
            raise RuntimeError("Omnigent MCP tools are closed")
        if not self._initialized:
            raise RuntimeError("Omnigent MCP tools are not initialized")
        if name not in self._tool_names:
            raise RuntimeError(f"Omnigent MCP tools do not contain {name!r}")
        return await self._manager.call_tool(
            self._agent_spec,
            name,
            arguments,
            session_id=self._session_id(),
        )

    async def close(self) -> None:
        """Shut down all native MCP connections and subprocesses once."""
        async with self._close_lock:
            if self._closed:
                return
            await self._manager.shutdown()
            self._closed = True
