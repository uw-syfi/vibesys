"""Omnigent implementation of the stateful agent-driver contract."""

# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from vibesys.agents.cli_common import build_schema_hint
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentObserver,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
)
from vibesys.agents.drivers._omnigent_lifecycle import CloseLifecycle as _CloseLifecycle
from vibesys.agents.drivers._omnigent_lifecycle import LifecycleState as _LifecycleState
from vibesys.agents.drivers._omnigent_mcp import OmnigentMCPTools as _OmnigentMCPTools
from vibesys.agents.drivers._omnigent_runtime import (
    OmnigentAsyncRuntime as _OmnigentAsyncRuntime,
)
from vibesys.agents.drivers._omnigent_runtime import OmnigentAsyncTask as _OmnigentAsyncTask
from vibesys.agents.host_resource_declarations import (
    declare_active_rust_toolchain_resources,
    resolve_active_rust_toolchain,
)
from vibesys.agents.omnigent.providers import (
    OMNIGENT_PROVIDER_EXECUTORS,
    OmnigentExecutorSpec,
    supported_providers,
)
from vibesys.run.events import CommandResultPayload, JsonResultPayload, ToolResultPayload
from vs_sandbox import HostResourceContext

if TYPE_CHECKING:
    import concurrent.futures
    from collections.abc import Callable, Coroutine

_TOOL_EXECUTOR_ATTR = "_tool_executor"
"""Private Omnigent 0.10.0 tool-dispatch seam, guarded before assignment."""

_OS_ENV_HELPER_ATTR = "_helper"
_HELPER_SANDBOX_ATTR = "sandbox"
_EXPLICIT_HELPER_ENV_ATTR = "_vibesys_explicit_parent_environment"
_PROVIDER_ENVIRONMENT_ATTRS = {
    "claude": "_extra_env",
    "codex": "_env",
}
"""Private Omnigent 0.10.0 environment seams, centralized by adapters below."""

_OMNIGENT_INTERNAL_HIDDEN = frozenset({".codex-tmp"})
"""Runtime-owned workspace paths that OS tools must not traverse."""

_SESSION_SHUTDOWN_TIMEOUT = 5.0

OMNIGENT_CAPABILITIES = AgentCapabilities(
    mcp_servers=True,
    timeouts=True,
    session_reuse=True,
)
"""Capabilities available before constructing Omnigent runtime resources."""


def _unwrap_turn(
    outcome: tuple[AgentTurnResult | None, BaseException | None],
) -> AgentTurnResult:
    result, error = outcome
    if error is not None:
        raise error
    assert result is not None  # noqa: S101  # paired result/error contract
    return result


class OmnigentDriverError(RuntimeError):
    """An Omnigent driver requirement could not be satisfied safely."""


def _cleanup_after_failure(
    error: BaseException,
    cleanup: Callable[[], None],
    *,
    description: str,
) -> None:
    """Run setup cleanup without replacing the error that triggered it."""
    try:
        cleanup()
    except BaseException as cleanup_error:  # noqa: BLE001
        error.add_note(f"{description} also failed: {cleanup_error}")


@dataclass
class _OwnedOSTools:
    """Schemas, dispatcher, and OS environments created as one owned unit."""

    schemas: list[dict[str, Any]]
    dispatch: Callable[[str, dict[str, Any]], Any]
    environments: tuple[Any, ...]

    def handles(self, name: str) -> bool:
        """Return whether ``name`` belongs to this sandboxed tool surface."""
        return any(schema.get("name") == name for schema in self.schemas)

    def close(self) -> None:
        """Close every environment exactly once and preserve the first failure."""
        environments, self.environments = self.environments, ()
        first_error: BaseException | None = None
        for environment in environments:
            try:
                environment.close()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


@dataclass
class _ExecutorResources:
    """Synchronous resources transferred atomically to one session.

    The executor is intentionally separate: its async ``close`` must run on the
    driver event loop. These synchronous resources are retained by the driver
    until shared loop workers from a cancelled turn have drained.
    """

    scratch: tempfile.TemporaryDirectory[str] | None = None
    os_tools: _OwnedOSTools | None = None

    def close(self) -> None:
        """Close all resources exactly once and preserve the first failure."""
        os_tools, self.os_tools = self.os_tools, None
        scratch, self.scratch = self.scratch, None
        first_error: BaseException | None = None
        if os_tools is not None:
            try:
                os_tools.close()
            except BaseException as exc:  # noqa: BLE001
                first_error = exc
        if scratch is not None:
            try:
                scratch.cleanup()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _is_top_level_dot_path(path: Path) -> bool:
    return len(path.parts) == 1 and path.name.startswith(".")


def _resolve_executor_spec(provider: str) -> OmnigentExecutorSpec:
    spec = OMNIGENT_PROVIDER_EXECUTORS.get(provider)
    if spec is None:
        raise OmnigentDriverError(
            f"Omnigent does not support agent provider {provider!r}; "
            f"supported providers: {supported_providers()}"
        )
    return spec


def _missing_omnigent(what: str, exc: ImportError) -> OmnigentDriverError:
    return OmnigentDriverError(
        f"{what} is not importable ({type(exc).__name__}: {exc}). "
        "Reinstall dependencies with `uv sync` (omnigent is a base dependency)."
    )


def _sandbox_backend_for_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux_bwrap"
    if sys.platform == "darwin":
        return "darwin_seatbelt"
    if os.name == "nt":
        return "windows_jobobject"
    raise OmnigentDriverError(f"Omnigent has no sandbox backend for platform {sys.platform!r}")


def _flatten_tool_schema(tool: Any) -> dict[str, Any]:  # noqa: ANN401
    function = tool.get_schema().get("function", {})
    return {
        "name": function.get("name"),
        "description": function.get("description"),
        "parameters": function.get("parameters", {"type": "object", "properties": {}}),
    }


_HELPER_ENVIRONMENT_HOOK_LOCK = threading.Lock()
_helper_environment_hook_installed = False


def _install_helper_environment_hook() -> None:
    """Teach Omnigent 0.10's helper builder about per-helper parent environments."""
    global _helper_environment_hook_installed  # noqa: PLW0603
    with _HELPER_ENVIRONMENT_HOOK_LOCK:
        if _helper_environment_hook_installed:
            return
        try:
            from omnigent.inner import os_env as omnigent_os_env  # noqa: PLC0415
        except ImportError as exc:
            raise _missing_omnigent("Omnigent OS-environment tools", exc) from exc
        # The seam is a private module-level function of a pinned dependency, so
        # both the read and the rebind below go through the dynamic module
        # attribute rather than a declared symbol.
        os_env_module: Any = omnigent_os_env
        original = getattr(omnigent_os_env, "build_helper_env", None)
        if not callable(original):
            raise OmnigentDriverError(
                "Omnigent OS environment has no callable 'build_helper_env' seam; "
                "this integration requires the private Omnigent 0.10.0 helper API"
            )

        # Parameter names match Omnigent's own ``build_helper_env`` so callers
        # that pass them by keyword keep working through the patch.
        def build_helper_env(parent_env: Any, sandbox: Any) -> dict[str, str]:  # noqa: ANN401
            explicit = getattr(sandbox, _EXPLICIT_HELPER_ENV_ATTR, None)
            source = explicit if isinstance(explicit, MappingProxyType) else parent_env
            return cast("dict[str, str]", original(source, sandbox))

        os_env_module.build_helper_env = build_helper_env
        _helper_environment_hook_installed = True


def _adapt_helper_environment(os_environment: Any, environment: dict[str, str]) -> None:  # noqa: ANN401
    """Bind an immutable parent environment to one Omnigent 0.10 helper."""
    helper = getattr(os_environment, _OS_ENV_HELPER_ATTR, None)
    sandbox = getattr(helper, _HELPER_SANDBOX_ATTR, None)
    if helper is None or sandbox is None:
        raise OmnigentDriverError(
            f"Omnigent OS environment has no {_OS_ENV_HELPER_ATTR!r}."
            f"{_HELPER_SANDBOX_ATTR!r} seam; this integration requires the private "
            "Omnigent 0.10.0 helper API"
        )
    _install_helper_environment_hook()
    setattr(
        sandbox,
        _EXPLICIT_HELPER_ENV_ATTR,
        MappingProxyType({**os.environ, **environment}),
    )


def _adapt_provider_environment(
    executor: Any,  # noqa: ANN401
    *,
    provider: str,
    environment: dict[str, str],
) -> None:
    """Merge explicit values into Omnigent's version-pinned provider spawn env."""
    attribute = _PROVIDER_ENVIRONMENT_ATTRS.get(provider)
    provider_environment = getattr(executor, attribute, None) if attribute is not None else None
    if not isinstance(provider_environment, dict):
        raise OmnigentDriverError(
            f"Omnigent {provider!r} executor has no mutable environment seam; "
            "this integration requires the private Omnigent 0.10.0 provider API"
        )
    provider_environment.update(environment)


def _build_os_tools(  # noqa: C901  # construction cleans every partially-created helper
    os_env_spec: Any,  # noqa: ANN401
    workspace: Path,
    environment: dict[str, str] | None = None,
    shell_os_env_spec: Any | None = None,  # noqa: ANN401
) -> _OwnedOSTools:
    """Build Omnigent's sandboxed filesystem tools and their dispatcher."""
    try:
        from omnigent.inner.os_env import create_os_environment  # noqa: PLC0415
        from omnigent.tools.base import ToolContext  # noqa: PLC0415
        from omnigent.tools.builtins.os_env import build_os_env_tools  # noqa: PLC0415
    except ImportError as exc:
        raise _missing_omnigent("Omnigent OS-environment tools", exc) from exc

    try:
        os_env = create_os_environment(os_env_spec)
    except OSError as exc:
        raise OmnigentDriverError(
            f"Omnigent cannot provide its {_sandbox_backend_for_platform()!r} "
            f"sandbox on this host: {exc}"
        ) from exc
    if os_env is None:
        raise OmnigentDriverError(
            f"Omnigent could not create a sandboxed OS environment for {workspace}"
        )

    environments = [os_env]
    try:
        if shell_os_env_spec is not None:
            shell_os_env = create_os_environment(shell_os_env_spec)
            if shell_os_env is None:
                raise OmnigentDriverError(  # noqa: TRY301
                    f"Omnigent could not create a sandboxed shell environment for {workspace}"
                )
            environments.append(shell_os_env)
            _adapt_helper_environment(os_env, {})
            _adapt_helper_environment(shell_os_env, environment or {})
            tools = build_os_env_tools(os_env)
            shell_tools = build_os_env_tools(shell_os_env)
            shell_tool = next((tool for tool in shell_tools if tool.name() == "sys_os_shell"), None)
            if shell_tool is None:  # pragma: no cover - guarded against Omnigent API drift
                raise OmnigentDriverError(  # noqa: TRY301
                    "Omnigent did not provide its sys_os_shell tool"
                )
            tools = [shell_tool if tool.name() == "sys_os_shell" else tool for tool in tools]
        else:
            _adapt_helper_environment(os_env, environment or {})
            tools = build_os_env_tools(os_env)
        by_name = {tool.name(): tool for tool in tools}
        schemas = [_flatten_tool_schema(tool) for tool in tools]
        context = ToolContext(task_id="vibesys", agent_id="vibesys", workspace=workspace)
    except BaseException:
        for resource in environments:
            with contextlib.suppress(BaseException):
                resource.close()
        raise

    async def dispatch(name: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
        tool = by_name.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}"}

        def invoke() -> Any:  # noqa: ANN401
            return tool.invoke(json.dumps(args), context)

        return await asyncio.to_thread(invoke)

    return _OwnedOSTools(schemas=schemas, dispatch=dispatch, environments=tuple(environments))


def _usage_from_mapping(usage: dict[str, Any]) -> AgentUsage:
    return AgentUsage(
        input_tokens=usage.get("input_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_cost_usd=usage.get("total_cost_usd"),
        duration_ms=usage.get("duration_ms"),
    )


def _emit(observer: AgentObserver | None, event: AgentEvent) -> None:
    if observer is not None:
        observer.on_event(event)


def _tool_result_payload(
    result: Any,  # noqa: ANN401
    error: str | None,
    duration: float,
) -> ToolResultPayload:
    """Map the structure Omnigent actually reported, never guessing.

    A dict or list result is preserved as parsed JSON (coerced through
    ``json.dumps`` so the event always serializes); anything else keeps the
    command shape the flattened ``stdout``/``stderr`` strings already carry.
    """
    if isinstance(result, (dict, list)):
        value = json.loads(json.dumps(result, default=repr))
        return JsonResultPayload(value=value)
    return CommandResultPayload(
        stdout=str(result) if result is not None else "",
        stderr=str(error) if error is not None else "",
        exit_code=None,
        duration=duration,
    )


async def _drive_turn(
    executor: Any,  # noqa: ANN401
    *,
    request: AgentTurnRequest,
    reasoning_effort: str | None,
    tool_schemas: list[dict[str, Any]],
    observer: AgentObserver | None,
) -> AgentTurnResult:
    """Translate one Omnigent event stream into neutral events and a result."""
    try:
        from omnigent import (  # noqa: PLC0415
            ExecutorConfig,
            TextChunk,
            ToolCallComplete,
            ToolCallRequest,
            TurnComplete,
        )
    except ImportError as exc:
        raise _missing_omnigent("Omnigent executor event types", exc) from exc

    message = request.message
    if request.output_schema is not None:
        message += build_schema_hint(request.output_schema)
    messages: list[Any] = [{"role": "user", "content": message}]
    config = (
        ExecutorConfig(extra={"reasoning_effort": reasoning_effort})
        if reasoning_effort is not None
        else None
    )
    chunks: list[str] = []
    response: str | None = None
    usage = AgentUsage()

    async for event in executor.run_turn(messages, tool_schemas, request.instructions, config):
        if isinstance(event, TextChunk):
            chunks.append(event.text)
            _emit(observer, AgentEvent(AgentEventKind.TEXT, text=event.text))
        elif isinstance(event, ToolCallRequest):
            _emit(
                observer,
                AgentEvent(
                    AgentEventKind.TOOL_CALL,
                    payload={"tool": event.name, "args": event.args},
                ),
            )
        elif isinstance(event, ToolCallComplete):
            duration = event.duration_ms / 1000
            _emit(
                observer,
                AgentEvent(
                    AgentEventKind.TOOL_RESULT,
                    payload={
                        "tool": event.name,
                        "stdout": str(event.result) if event.result is not None else "",
                        "stderr": str(event.error) if event.error is not None else "",
                        "exit_code": None,
                        "duration": duration,
                        "status": getattr(event.status, "value", str(event.status)),
                        "result_payload": _tool_result_payload(event.result, event.error, duration),
                    },
                ),
            )
        elif isinstance(event, TurnComplete):
            response = event.response
            usage = _usage_from_mapping(event.usage or {})
            if event.usage:
                _emit(observer, AgentEvent(AgentEventKind.USAGE, usage=usage))

    provider_session_id = getattr(executor, "thread_id", None)
    return AgentTurnResult(
        text=response if response is not None else "".join(chunks),
        usage=usage,
        provider_session_id=(provider_session_id if isinstance(provider_session_id, str) else None),
    )


class OmnigentSession:
    """One configured Omnigent executor and provider conversation."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        driver: OmnigentDriver,
        spec: AgentSessionSpec,
        executor: Any,  # noqa: ANN401
        tool_schemas: list[dict[str, Any]],
        mcp_tools: _OmnigentMCPTools | None = None,
        resources: _ExecutorResources | None = None,
    ) -> None:
        """Own ``executor`` until this session is closed."""
        self._driver = driver
        self._spec = spec
        self._executor = executor
        self._tool_schemas = tool_schemas
        self._mcp_tools = mcp_tools
        self._resources: _ExecutorResources | None = resources or _ExecutorResources()
        self._lifecycle = threading.Condition()
        self._close_lifecycle = _CloseLifecycle(self._lifecycle)
        self._turn_lock = threading.Lock()
        self._active_turn: (
            _OmnigentAsyncTask[tuple[AgentTurnResult | None, BaseException | None]] | None
        ) = None
        self._failed = False

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Run one resumable Omnigent turn."""
        # A session is one provider conversation and remains sequential. Other
        # sessions submit independent tasks to the driver runtime and can overlap.
        with self._turn_lock:
            with self._lifecycle:
                if self._close_lifecycle.state is not _LifecycleState.OPEN:
                    raise RuntimeError("Omnigent session is closed")
                if self._failed:
                    raise RuntimeError("Omnigent session must be reset after a failed turn")
            try:
                with self._lifecycle:
                    if self._close_lifecycle.state is not _LifecycleState.OPEN:
                        raise RuntimeError(  # noqa: TRY301
                            "Omnigent session is closed"
                        )
                    task = self._driver.start_task(self._run_turn(request, observer))
                    self._active_turn = task
                try:
                    return _unwrap_turn(task.result())
                except BaseException:
                    task.cancel_and_wait()
                    raise
                finally:
                    with self._lifecycle:
                        if self._active_turn is task:
                            self._active_turn = None
            except BaseException:
                with self._lifecycle:
                    self._failed = True
                raise

    def resume_provider_session(self, session_id: str) -> bool:
        """Refuse the checkpoint: Omnigent executors have no resume entry point.

        Omnigent 0.10 owns its executor's conversation lifecycle internally and
        exposes no way to attach a thread created by an earlier process, so
        there is nothing to adopt and the turn starts a fresh conversation.
        Reported through ``provider_session_resume=False`` as well, so callers
        can tell before they build a session.
        """
        del session_id
        return False

    def cancel(self) -> None:
        """Cancel the in-flight turn, if any, and wait for it to unwind.

        Omnigent runs each turn as a task on the driver's event loop, so this
        cancels through that loop and is safe from any other thread. It is a
        no-op once the turn has completed or the session has begun closing,
        which is what makes repeated calls idempotent. The cancelled turn's
        failure still surfaces in ``run_turn``, not here.
        """
        if self.owns_current_loop_thread():
            raise RuntimeError("Omnigent session cannot be cancelled from its event-loop thread")
        with self._lifecycle:
            if self._close_lifecycle.state is not _LifecycleState.OPEN:
                return
            active = self._active_turn
        if active is not None:
            active.cancel_and_wait()

    def close(self) -> None:
        """Release the executor exactly once."""
        if self.owns_current_loop_thread():
            raise RuntimeError("Omnigent session cannot be closed from its event-loop thread")
        active: _OmnigentAsyncTask[Any] | None = None
        owner = self._close_lifecycle.begin_close()
        if owner:
            with self._lifecycle:
                active = self._active_turn
        else:
            return
        first_error: BaseException | None = None
        try:
            first_error = self._close_resources(active)
        except BaseException as exc:  # noqa: BLE001  # completion must still be signaled
            first_error = exc
        finally:
            self._close_lifecycle.finish_close(first_error)
        if first_error is not None:
            raise first_error

    def owns_current_loop_thread(self) -> bool:
        """Return whether the caller is the driver async runtime thread."""
        return self._driver.owns_current_loop_thread()

    def _close_resources(
        self,
        active: _OmnigentAsyncTask[Any] | None,
    ) -> BaseException | None:
        """Best-effort all resources and preserve the first cleanup failure."""
        if active is not None:
            active.cancel_and_wait()

        first_error: BaseException | None = None
        try:
            cleanup = self._driver.submit(self._shutdown())
            try:
                first_error = cleanup.result()
            except BaseException as exc:  # noqa: BLE001
                first_error = exc
                cleanup.cancel()
        except BaseException as exc:  # noqa: BLE001
            first_error = exc
        try:
            self._driver.release_session(self)
        except BaseException as exc:  # noqa: BLE001
            if first_error is None:
                first_error = exc
        resources, self._resources = self._resources, None
        if resources is not None:
            try:
                if active is None:
                    resources.close()
                else:
                    self._driver.defer_resources(resources)
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        return first_error

    async def _run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None,
    ) -> tuple[AgentTurnResult | None, BaseException | None]:
        try:
            turn = _drive_turn(
                self._executor,
                request=request,
                reasoning_effort=self._spec.reasoning_effort,
                tool_schemas=self._tool_schemas,
                observer=observer,
            )
            if request.timeout is not None:
                return (
                    await asyncio.wait_for(turn, timeout=request.timeout.total_seconds()),
                    None,
                )
            return await turn, None
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            # asyncio deliberately re-raises KeyboardInterrupt/SystemExit out
            # of tasks. Encode it so it is re-raised on the invoking thread
            # without terminating this session's loop thread.
            return None, exc

    async def _shutdown(self) -> BaseException | None:
        """Close this session's native resources on the driver runtime."""
        first_error: BaseException | None = None
        try:
            close = getattr(self._executor, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except BaseException as exc:  # noqa: BLE001
            first_error = exc
        mcp_tools = self._mcp_tools
        if mcp_tools is not None:
            try:
                await mcp_tools.close()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
            else:
                self._mcp_tools = None
        return first_error


class OmnigentDriver:
    """Create sandboxed Omnigent sessions and own their native resources."""

    def __init__(self) -> None:
        """Create a driver with no live sessions."""
        self._runtime = _OmnigentAsyncRuntime(start_timeout=_SESSION_SHUTDOWN_TIMEOUT)
        self._sessions: set[OmnigentSession] = set()
        self._lifecycle = threading.Condition()
        self._close_lifecycle = _CloseLifecycle(self._lifecycle)
        self._creating_sessions = 0
        self._deferred_resources: list[_ExecutorResources] = []

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe the requirements Omnigent 0.10.0 can satisfy."""
        return OMNIGENT_CAPABILITIES

    def create_session(self, spec: AgentSessionSpec) -> OmnigentSession:
        """Validate setup requirements and create a confined session."""
        with self._lifecycle:
            if self._close_lifecycle.state is not _LifecycleState.OPEN:
                raise RuntimeError("Omnigent driver is closed")
            self._creating_sessions += 1
        try:
            self._validate_spec(spec)
            executor, schemas, mcp_tools, resources = self._build_executor(spec)
            session = OmnigentSession(
                driver=self,
                spec=spec,
                executor=executor,
                tool_schemas=schemas,
                mcp_tools=mcp_tools,
                resources=resources,
            )
            with self._lifecycle:
                closed = self._close_lifecycle.state is not _LifecycleState.OPEN
                if not closed:
                    self._sessions.add(session)
            if closed:
                session.close()
                raise RuntimeError("Omnigent driver is closed")
            return session
        finally:
            with self._lifecycle:
                self._creating_sessions -= 1
                self._lifecycle.notify_all()

    def close(self) -> None:  # noqa: C901, PLR0912  # exhaustive owner cleanup
        """Close every outstanding session."""
        if self.owns_current_loop_thread():
            raise RuntimeError("Omnigent driver cannot be closed from its event-loop thread")
        owner = self._close_lifecycle.begin_close()
        if not owner:
            return
        first_error: BaseException | None = None
        try:
            with self._lifecycle:
                while self._creating_sessions > 0:
                    self._lifecycle.wait()
                sessions = tuple(self._sessions)
            for session in sessions:
                try:
                    session.close()
                except BaseException as exc:  # noqa: BLE001
                    if first_error is None:
                        first_error = exc
            try:
                self._runtime.close()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
            with self._lifecycle:
                deferred, self._deferred_resources = self._deferred_resources, []
            for resources in deferred:
                try:
                    resources.close()
                except BaseException as exc:  # noqa: BLE001
                    if first_error is None:
                        first_error = exc
        except BaseException as exc:  # noqa: BLE001  # completion must still be signaled
            first_error = exc
        finally:
            self._close_lifecycle.finish_close(first_error)
        if first_error is not None:
            raise first_error

    def _validate_spec(self, spec: AgentSessionSpec) -> None:
        _resolve_executor_spec(spec.provider)
        if spec.policy.host_resources:
            raise OmnigentDriverError("Omnigent cannot enforce VibeSys host-resource grants")
        if spec.policy.containerized:
            raise OmnigentDriverError(
                "Omnigent cannot run this agent in VibeSys's container execution path"
            )
        project_paths = spec.policy.project_paths
        read_only_paths = () if project_paths is None else project_paths.read_only_paths
        unsupported_paths = [path for path in read_only_paths if not _is_top_level_dot_path(path)]
        if unsupported_paths:
            raise OmnigentDriverError(
                "Omnigent 0.10.0 can accept only top-level dot paths as "
                "contract-protected read-only project paths; unsupported paths: "
                f"{[str(path) for path in unsupported_paths]}"
            )

    def run_awaitable(self, awaitable: Any) -> Any:  # noqa: ANN401
        """Run and drain one coroutine on the driver-owned async runtime."""
        task = self.start_task(awaitable)
        try:
            return task.result()
        except BaseException:
            task.cancel_and_wait()
            raise

    def submit[Result](
        self,
        awaitable: Coroutine[Any, Any, Result],
    ) -> concurrent.futures.Future[Result]:
        """Submit session work to this driver's async runtime."""
        return self._runtime.submit(awaitable)

    def start_task[Result](
        self,
        awaitable: Coroutine[Any, Any, Result],
    ) -> _OmnigentAsyncTask[Result]:
        """Start one turn as an explicitly owned runtime task."""
        return self._runtime.start_task(awaitable)

    def owns_current_loop_thread(self) -> bool:
        """Return whether the caller is the driver async runtime thread."""
        return self._runtime.is_current_thread()

    def defer_resources(self, resources: _ExecutorResources) -> None:
        """Retain resources until shared default-executor workers have drained."""
        with self._lifecycle:
            self._deferred_resources.append(resources)

    def _executor_class(self, spec: OmnigentExecutorSpec) -> type[Any]:
        try:
            module = import_module(spec.module)
        except ImportError as exc:
            raise _missing_omnigent(spec.module, exc) from exc
        try:
            return getattr(module, spec.class_name)
        except AttributeError as exc:
            raise OmnigentDriverError(
                f"Omnigent module {spec.module!r} has no {spec.class_name!r}; "
                "this integration requires the Omnigent 0.10.0 executor API"
            ) from exc

    def _build_os_env(
        self,
        spec: AgentSessionSpec,
        *,
        additional_write_paths: tuple[Path, ...] = (),
        env_passthrough: tuple[str, ...] = (),
        include_toolchain: bool = False,
    ) -> Any:  # noqa: ANN401
        try:
            from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec  # noqa: PLC0415
        except ImportError as exc:
            raise _missing_omnigent("Omnigent OS-environment datamodel", exc) from exc
        workspace = spec.workspace
        project_paths = spec.policy.project_paths
        hidden = set(() if project_paths is None else project_paths.hidden_paths)
        allow_hidden = [
            entry.name
            for entry in sorted(workspace.iterdir(), key=lambda path: path.name)
            if entry.name.startswith(".")
            and entry.name not in _OMNIGENT_INTERNAL_HIDDEN
            and Path(entry.name) not in hidden
        ]
        environment = {**os.environ, **dict(spec.environment)}
        read_paths = None
        if include_toolchain:
            resources = declare_active_rust_toolchain_resources(
                HostResourceContext(env=environment),
                workspace=workspace,
            )
            read_paths = sorted(
                {
                    str(resource.path.expanduser().resolve())
                    for resource in resources
                    if resource.path.exists()
                }
            )
        sandbox = OSEnvSandboxSpec(
            type=_sandbox_backend_for_platform(),
            read_paths=read_paths,
            write_paths=[str(workspace), *(str(path) for path in additional_write_paths)],
            cwd_allow_hidden=allow_hidden,
            cwd_hidden_scan_recursive=False,
            cwd_hidden_scan_overflow="error",
            mask_paths=sorted({str(path) for path in hidden} | _OMNIGENT_INTERNAL_HIDDEN),
            env_passthrough=list(env_passthrough) or None,
        )
        return OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=sandbox,
        )

    def _build_executor(  # noqa: C901, PLR0915
        self, spec: AgentSessionSpec
    ) -> tuple[Any, list[dict[str, Any]], _OmnigentMCPTools | None, _ExecutorResources]:
        executor_spec = _resolve_executor_spec(spec.provider)
        executor_cls = self._executor_class(executor_spec)
        os_env_spec = self._build_os_env(spec)
        resources = _ExecutorResources(
            scratch=tempfile.TemporaryDirectory(prefix="vibesys-omnigent-")
        )
        assert resources.scratch is not None  # noqa: S101  # initialized above
        scratch_path = Path(resources.scratch.name)
        environment = {**os.environ, **dict(spec.environment)}
        tool_environment = dict(spec.environment)
        rust_toolchain = resolve_active_rust_toolchain(
            HostResourceContext(env=environment),
            workspace=spec.workspace,
        )
        if rust_toolchain is not None:
            rust_bin = rust_toolchain[0] / "bin"
            tool_environment.update(
                {
                    "CARGO_HOME": str(scratch_path / "cargo-home"),
                    "PATH": os.pathsep.join((str(rust_bin), environment.get("PATH", ""))),
                    "RUSTC": str(rust_bin / "rustc"),
                    "RUSTDOC": str(rust_bin / "rustdoc"),
                    "RUSTUP_AUTO_INSTALL": "0",
                }
            )
        try:
            shell_os_env_spec = self._build_os_env(
                spec,
                additional_write_paths=(scratch_path,),
                env_passthrough=tuple(tool_environment),
                include_toolchain=True,
            )
        except BaseException as error:
            _cleanup_after_failure(
                error,
                resources.close,
                description="Omnigent executor resource cleanup",
            )
            raise
        executor_kwargs: dict[str, Any] = {
            "cwd": str(spec.workspace),
            "model": spec.model,
            "os_env": os_env_spec,
        }
        if spec.provider == "codex":
            # Codex's native workspace sandbox cannot represent Omnigent's
            # dot-path masks. Route all filesystem access through the
            # sandboxed sys_os_* tools instead.
            executor_kwargs["disable_native_tools"] = True
        try:
            executor = executor_cls(**executor_kwargs)
        except ImportError as exc:
            error = OmnigentDriverError(
                f"Omnigent provider {spec.provider!r} is unavailable: {exc}"
            )
            _cleanup_after_failure(
                error,
                resources.close,
                description="Omnigent executor resource cleanup",
            )
            raise error from exc
        except BaseException as error:
            _cleanup_after_failure(
                error,
                resources.close,
                description="Omnigent executor resource cleanup",
            )
            raise
        if not hasattr(executor, _TOOL_EXECUTOR_ATTR):
            error = OmnigentDriverError(
                f"{executor_cls.__name__} has no {_TOOL_EXECUTOR_ATTR!r} slot; "
                "this integration requires the private Omnigent 0.10.0 tool-dispatch seam"
            )
            _cleanup_after_failure(
                error,
                lambda: self.close_executor(executor, resources=resources),
                description="Omnigent executor cleanup",
            )
            raise error
        try:
            _adapt_provider_environment(
                executor,
                provider=spec.provider,
                environment=dict(spec.environment),
            )
        except BaseException as error:
            _cleanup_after_failure(
                error,
                lambda: self.close_executor(executor, resources=resources),
                description="Omnigent executor cleanup",
            )
            raise
        mcp_tools: _OmnigentMCPTools | None = None
        try:
            os_tools = _build_os_tools(
                os_env_spec,
                spec.workspace,
                tool_environment,
                shell_os_env_spec,
            )
            resources.os_tools = os_tools
            mcp_tools = _OmnigentMCPTools.build(
                servers=spec.mcp_servers,
                workspace=spec.workspace,
                harness=executor_spec.harness,
                session_id=lambda: cast("str | None", getattr(executor, "thread_id", None)),
            )
            if mcp_tools is not None:
                self.run_awaitable(mcp_tools.initialize())

            async def dispatch(name: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
                if mcp_tools is not None and mcp_tools.handles(name):
                    return await mcp_tools.dispatch(name, args)
                if os_tools.handles(name):
                    return await os_tools.dispatch(name, args)
                return {"error": f"unknown tool {name!r}"}

            setattr(executor, _TOOL_EXECUTOR_ATTR, dispatch)
        except BaseException as error:
            if mcp_tools is not None:
                try:
                    self.run_awaitable(mcp_tools.close())
                except BaseException as cleanup_error:  # noqa: BLE001
                    error.add_note(f"Omnigent MCP cleanup also failed: {cleanup_error}")
            _cleanup_after_failure(
                error,
                lambda: self.close_executor(executor, resources=resources),
                description="Omnigent executor cleanup",
            )
            raise
        mcp_schemas = [] if mcp_tools is None else mcp_tools.schemas
        duplicate_names = {schema["name"] for schema in os_tools.schemas} & {
            schema["name"] for schema in mcp_schemas
        }
        if duplicate_names:
            error = OmnigentDriverError(
                f"Omnigent MCP tools conflict with OS tools: {sorted(duplicate_names)}"
            )
            assert mcp_tools is not None  # noqa: S101  # duplicate MCP schema implies owner
            _cleanup_after_failure(
                error,
                lambda: self.run_awaitable(mcp_tools.close()),
                description="Omnigent MCP cleanup",
            )
            _cleanup_after_failure(
                error,
                lambda: self.close_executor(executor, resources=resources),
                description="Omnigent executor cleanup",
            )
            raise error
        return executor, [*os_tools.schemas, *mcp_schemas], mcp_tools, resources

    def release_session(self, session: OmnigentSession) -> None:
        """Forget a closed session after it has released its owned resources."""
        with self._lifecycle:
            self._sessions.discard(session)

    def close_executor(
        self,
        executor: Any,  # noqa: ANN401
        *,
        run_awaitable: Callable[[Any], Any] | None = None,
        resources: _ExecutorResources | None = None,
    ) -> None:
        """Close a native executor, awaiting asynchronous cleanup when needed."""
        first_error: BaseException | None = None
        try:
            close = getattr(executor, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    (run_awaitable or self.run_awaitable)(result)
        except BaseException as exc:  # noqa: BLE001
            first_error = exc
        if resources is not None:
            try:
                resources.close()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
