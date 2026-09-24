"""Native Omnigent MCP integration for one VibeSys agent session."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_agent.contracts import MCPServerSpec


class OmnigentMCPError(RuntimeError):
    """Omnigent could not initialize the requested MCP tool surface."""

    @classmethod
    def dependency_unavailable(cls, error: ImportError) -> OmnigentMCPError:
        """Describe a missing pinned MCP API and how to restore it."""
        return cls(
            f"Omnigent MCP support is not importable ({type(error).__name__}: {error}). "
            "Reinstall dependencies with `uv sync` (omnigent is a base dependency)."
        )

    @classmethod
    def duplicate_server_names(cls, names: list[str]) -> OmnigentMCPError:
        """Describe duplicate configured MCP server names."""
        return cls(f"MCP server names must be unique: {names}")

    @classmethod
    def invalid_configuration(cls, details: str) -> OmnigentMCPError:
        """Describe a redacted native MCP configuration validation failure."""
        return cls(f"Invalid Omnigent MCP configuration: {details}")

    @classmethod
    def connection_failed(cls, details: str) -> OmnigentMCPError:
        """Describe redacted failures to connect configured MCP servers."""
        return cls(f"Omnigent could not connect MCP servers: {details}")


@dataclass(frozen=True)
class _NativeMCPAPI:
    agent_spec: type[Any]
    executor_spec: type[Any]
    server_config: type[Any]
    manager: type[Any]
    validate: Callable[[Any], Any]


def _raise_connection_failures(
    failures: Mapping[str, object], servers: tuple[MCPServerSpec, ...]
) -> None:
    """Raise a redacted error when any configured MCP server failed."""
    details = "; ".join(f"{name}: {error}" for name, error in sorted(failures.items()))
    raise OmnigentMCPError.connection_failed(_redact_environment_values(details, servers))


def _native_mcp_api() -> _NativeMCPAPI:
    """Load the pinned Omnigent MCP API only when the extra is selected."""
    try:
        from omnigent.runner.mcp_manager import (  # noqa: PLC0415  # lint-waiver: LW-010130 [PLC0415]; Keep RunnerMcpManager lazy in _native_mcp_api so unused providers and import cycles stay unloaded.
            RunnerMcpManager,
        )
        from omnigent.spec import (  # noqa: PLC0415  # lint-waiver: LW-010131 [PLC0415]; Keep this dependency lazy in _native_mcp_api so unused providers and import cycles stay unloaded.
            AgentSpec,
            ExecutorSpec,
            MCPServerConfig,
            validate,
        )
    except ImportError as exc:
        raise OmnigentMCPError.dependency_unavailable(exc) from exc
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
        raise OmnigentMCPError.duplicate_server_names(duplicate_names)

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
            raise OmnigentMCPError.invalid_configuration(
                _redact_environment_values(details, servers)
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
            message = "Omnigent MCP tools are already initialized"
            raise RuntimeError(message)
        if self._closed:
            message = "Omnigent MCP tools are closed"
            raise RuntimeError(message)
        try:
            result = await self._manager.schemas_for(self._agent_spec)
            if result.failures:
                _raise_connection_failures(result.failures, self._servers)
            self.schemas = list(result.schemas)
            self._tool_names = frozenset(result.tool_names)
            self._initialized = True
        except BaseException as error:
            try:
                await self.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-010132 [BLE001]; OmnigentMCPTools.initialize must finish cleanup and preserve cancellation or the first failure while releasing owned resources.
                error.add_note(f"Omnigent MCP cleanup also failed: {cleanup_error}")
            raise

    def handles(self, name: str) -> bool:
        """Return whether ``name`` belongs to this session's MCP surface."""
        return name in self._tool_names

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one namespaced MCP tool through Omnigent's native manager."""
        if self._closed:
            message = "Omnigent MCP tools are closed"
            raise RuntimeError(message)
        if not self._initialized:
            message = "Omnigent MCP tools are not initialized"
            raise RuntimeError(message)
        if name not in self._tool_names:
            message = f"Omnigent MCP tools do not contain {name!r}"
            raise RuntimeError(message)
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
