"""AgentShim implementation of the stateful agent-driver contract."""

from __future__ import annotations

import os
import sys
import weakref
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from vibesys._agent_cli.base import CodingAgent
from vibesys._agent_cli.base import MCPServerSpec as AgentShimMCPServerSpec
from vibesys._agent_cli.claude import ClaudeCodeCodingAgent
from vibesys._agent_cli.codex import CodexCodingAgent
from vibesys._agent_cli.gemini import GeminiCodingAgent
from vibesys._agent_cli.opencode import OpencodeCodingAgent
from vibesys.agents.cli_common import build_schema_hint, materialize_native_output_schema
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentObserver,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
    MCPServerSpec,
    SessionDisposition,
)
from vibesys.agents.docker_executor import DockerCommandExecutor
from vibesys.agents.host_resource_declarations import declare_agent_host_resources
from vibesys.run.events import CommandResultPayload
from vs_sandbox import build_host_sandbox

AGENTSHIM_CAPABILITIES = AgentCapabilities(
    mcp_servers=True,
    nested_read_only_paths=True,
    hidden_paths=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""Capabilities invariant across AgentShim host and container execution."""

if TYPE_CHECKING:
    from collections.abc import Callable


class _ProviderFactory(Protocol):
    """Constructor signature shared by AgentShim provider classes."""

    def __call__(
        self,
        model: str | None = None,
        event_handler: Any | None = None,  # noqa: ANN401  # tracked: #288
        *,
        executor: Any | None = None,  # noqa: ANN401  # tracked: #288
    ) -> CodingAgent: ...


_PROVIDER_CLASSES: dict[str, _ProviderFactory] = {
    "claude": ClaudeCodeCodingAgent,
    "gemini": GeminiCodingAgent,
    "codex": CodexCodingAgent,
    "opencode": OpencodeCodingAgent,
}


def supported_providers() -> list[str]:
    """Return the sorted provider names the AgentShim driver can run."""
    return sorted(_PROVIDER_CLASSES)


_REASONING_EFFORT_PROVIDERS = frozenset({"codex", "claude"})
_PYTHON_MCP_COMMANDS = frozenset({"python", "python3"})

_MAX_CODEX_SESSION_TURNS = 2
_MAX_CODEX_SESSION_INPUT_TOKENS = 10_000_000
_MAX_CODEX_SESSION_DURATION_MS = 600_000


def _ignore_log(_message: str) -> None:
    """Discard a driver diagnostic when no log sink was configured."""


def _is_missing_codex_rollout(exc: RuntimeError) -> bool:
    message = str(exc)
    return "thread/resume failed" in message and "no rollout found" in message


def _is_missing_opencode_session(exc: RuntimeError) -> bool:
    # ``opencode run --session <id>`` prints ``Error: Session not found`` when
    # its session store no longer has the session (rebuilt or cleaned up).
    return "session not found" in str(exc).lower()


def _heavy_codex_turn_reason(agent: CodingAgent) -> str | None:
    session = getattr(agent, "_last_session", None)
    usage = getattr(session, "final_usage", None) or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    duration_ms = int(getattr(session, "duration_ms", 0) or 0)
    reasons: list[str] = []
    if input_tokens >= _MAX_CODEX_SESSION_INPUT_TOKENS:
        reasons.append(f"{input_tokens} input tokens")
    if duration_ms >= _MAX_CODEX_SESSION_DURATION_MS:
        reasons.append(f"{duration_ms} ms duration")
    return " and ".join(reasons) or None


def _usage_from_agent(agent: CodingAgent) -> AgentUsage:
    session = getattr(agent, "_last_session", None)
    raw = getattr(session, "final_usage", None) or {}
    return AgentUsage(
        input_tokens=raw.get("input_tokens"),
        cache_creation_input_tokens=raw.get("cache_creation_input_tokens"),
        cache_read_input_tokens=raw.get("cache_read_input_tokens"),
        output_tokens=raw.get("output_tokens"),
        total_cost_usd=getattr(session, "total_cost_usd", None),
        duration_ms=getattr(session, "duration_ms", None),
    )


def _as_agentshim_mcp(spec: MCPServerSpec, *, in_container: bool) -> AgentShimMCPServerSpec:
    command = spec.command
    if not in_container and command in _PYTHON_MCP_COMMANDS:
        command = sys.executable
    return AgentShimMCPServerSpec(
        name=spec.name,
        command=command,
        args=list(spec.args),
        env=dict(spec.env),
    )


class _AgentShimEventHandler:
    """Translate AgentShim callbacks into neutral driver events."""

    def __init__(self) -> None:
        self.observer: AgentObserver | None = None

    def _emit(self, event: AgentEvent) -> None:
        if self.observer is not None:
            self.observer.on_event(event)

    def on_thinking(self, text: str) -> None:
        self._emit(AgentEvent(kind=AgentEventKind.THINKING, text=text))

    def on_text(self, text: str) -> None:
        # Assistant text and reasoning are separate channels downstream
        # (``assistant`` vs ``analysis``), so a provider that can tell them
        # apart reports text here rather than through ``on_thinking``.
        self._emit(AgentEvent(kind=AgentEventKind.TEXT, text=text))

    def on_tool_call(self, tool: str, args: dict[str, Any] | str | None = None) -> None:
        self._emit(
            AgentEvent(
                kind=AgentEventKind.TOOL_CALL,
                payload={"tool": tool, "args": args if args is not None else {}},
            )
        )

    def on_tool_result(
        self,
        tool: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        duration: float | None = None,
    ) -> None:
        self._emit(
            AgentEvent(
                kind=AgentEventKind.TOOL_RESULT,
                text=stdout or stderr,
                payload={
                    "tool": tool,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "duration": duration,
                    "result_payload": CommandResultPayload(
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=exit_code,
                        duration=duration,
                    ),
                },
            )
        )

    def on_usage(self, usage: dict[str, Any]) -> None:
        self._emit(
            AgentEvent(
                kind=AgentEventKind.USAGE,
                usage=AgentUsage(
                    input_tokens=usage.get("input_tokens"),
                    cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                    cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    total_cost_usd=usage.get("total_cost_usd"),
                    duration_ms=usage.get("duration_ms"),
                ),
            )
        )


class AgentShimSession:
    """One configured AgentShim conversation."""

    def __init__(  # noqa: D107, PLR0913  # tracked: #288
        self,
        *,
        agent: CodingAgent,
        spec: AgentSessionSpec,
        provider: str,
        timeout: int | None,
        event_handler: _AgentShimEventHandler,
        docker_sandboxes: dict[str, Any] | None,
        log: Callable[[str], None],
    ) -> None:
        self._agent = agent
        self._spec = spec
        self._provider = provider
        self._timeout = timeout
        self._event_handler = event_handler
        self._docker_sandboxes = docker_sandboxes
        self._log = log
        self._turn_count = 0
        # Set when the provider conversation was dropped and restarted while
        # serving the current turn, so the turn's result can report it.
        self._restarted = False
        self._closed = False

    def run_turn(  # noqa: C901, D102, PLR0912  # tracked: #288
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        if self._closed:
            raise RuntimeError("agent session is closed")  # noqa: TRY003  # tracked: #288

        self._event_handler.observer = observer
        self._refresh_container()
        prompt, schema_path = self._prepare_prompt(request)
        self._set_output_schema(schema_path)
        self._restarted = False

        in_container = self._docker_sandboxes is not None
        mcp_servers = [
            _as_agentshim_mcp(server, in_container=in_container)
            for server in self._spec.mcp_servers
        ]
        if mcp_servers:
            self._agent.install_mcp_servers(self._spec.workspace, mcp_servers)

        timeout = self._timeout
        if request.timeout is not None:
            timeout = max(1, int(request.timeout.total_seconds()))
        workspace_arg = None if in_container else str(self._spec.workspace)

        turn_error: BaseException | None = None
        try:
            text = self._generate_with_restart_fallback(
                prompt,
                cwd=workspace_arg,
                timeout=timeout,
            )
            self._turn_count += 1
        except BaseException as exc:
            turn_error = exc
            raise
        finally:
            cleanup_error: Exception | None = None
            if mcp_servers:
                try:
                    self._agent.uninstall_mcp_servers(self._spec.workspace, mcp_servers)
                except Exception as exc:  # noqa: BLE001  # tracked: #288
                    cleanup_error = exc
                    if turn_error is not None:
                        self._log(
                            "MCP config cleanup failed while preserving the original "
                            f"agent error: {exc}"
                        )
            try:
                self._repair_workspace_ownership()
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                if cleanup_error is None:
                    cleanup_error = exc
                elif turn_error is not None:
                    self._log(f"workspace ownership repair also failed: {exc}")
                if turn_error is not None:
                    self._log(
                        "workspace ownership repair failed while preserving the original "
                        f"agent error: {exc}"
                    )
            self._event_handler.observer = None
            if turn_error is None and cleanup_error is not None:
                raise cleanup_error

        # Read the conversation ID before the thread-budget check, which may
        # drop it: the caller still deserves to know which conversation ran.
        provider_session_id = self._agent.session_id
        restarted = self._restarted or self._renew_codex_thread_if_needed()
        return AgentTurnResult(
            text=text,
            usage=_usage_from_agent(self._agent),
            provider_session_id=provider_session_id,
            disposition=(
                SessionDisposition.RESET_REQUIRED if restarted else SessionDisposition.REUSABLE
            ),
        )

    def close(self) -> None:
        """Release this logical session. AgentShim processes are per-turn."""
        self._closed = True
        self._event_handler.observer = None

    def resume_provider_session(self, session_id: str) -> bool:
        """Continue ``session_id`` on the next turn, if the agent accepts it.

        The agent adopts the ID only when its provider has a resume flag and no
        conversation is already attached, so a mid-run continuation is never
        overwritten by an older checkpoint. A stale or deleted transcript is
        handled later, by the restart fallback around ``generate``.
        """
        return self._agent.resume_from(session_id)

    def _prepare_prompt(self, request: AgentTurnRequest) -> tuple[str, str | None]:
        response_cls = request.output_schema
        native_schema_path: str | None = None
        if response_cls is not None and bool(
            getattr(type(self._agent), "supports_native_output_schema", False)
            and callable(getattr(self._agent, "set_output_schema_path", None))
        ):
            try:
                native_schema_path = materialize_native_output_schema(
                    self._spec.workspace,
                    response_cls,
                    allow_arbitrary_keys=getattr(
                        type(self._agent),
                        "native_output_schema_allows_arbitrary_keys",
                        False,
                    ),
                )
            except (OSError, TypeError, ValueError) as exc:
                self._log(
                    f"[structured-output] native schema unavailable for "
                    f"{response_cls.__name__}; using prompt fallback: "
                    f"{type(exc).__name__}: {exc}"
                )

        schema_arg = native_schema_path
        if native_schema_path is not None and getattr(
            type(self._agent), "native_output_schema_wants_absolute_path", False
        ):
            schema_arg = str(self._spec.workspace / native_schema_path)
        schema_hint = (
            build_schema_hint(response_cls)
            if response_cls is not None and native_schema_path is None
            else ""
        )
        prompt = f"{request.instructions}\n\n{request.message}{schema_hint}"
        return prompt, schema_arg

    def _set_output_schema(self, path: str | None) -> None:
        setter = getattr(self._agent, "set_output_schema_path", None)
        if callable(setter):
            setter(path)
        elif path is not None:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"{type(self._agent).__name__} advertised native output schemas "
                "without implementing set_output_schema_path()"
            )

    def _renew_codex_thread_if_needed(self) -> bool:
        """Retire an over-budget Codex thread, reporting whether it was dropped.

        Evaluated after a turn rather than before one, so the decision reads the
        usage of the turn that just finished and the caller learns about the
        restart from that turn's result instead of discovering it on the next.
        """
        if self._provider != "codex":
            return False
        reason = (
            f"{_MAX_CODEX_SESSION_TURNS} successful turns"
            if self._turn_count >= _MAX_CODEX_SESSION_TURNS
            else _heavy_codex_turn_reason(self._agent)
        )
        if reason is None:
            return False
        self._log(
            f"renewing Codex thread after {reason}; durable workspace state remains authoritative."
        )
        self._agent.forget_session()
        self._turn_count = 0
        return True

    def _generate_with_restart_fallback(
        self,
        prompt: str,
        *,
        cwd: str | None,
        timeout: int | None,
    ) -> str:
        """Run one turn, retrying once from a fresh conversation if a resume failed.

        Only a resumed turn is retried, and only once: with the conversation
        dropped, the retry takes the fresh-session branch, so a second failure
        is a real agent failure and propagates. The retry loses the earlier
        conversation, which ``self._restarted`` reports to the caller.
        """
        try:
            return self._agent.generate(prompt, cwd=cwd, timeout=timeout, silent=True)
        except RuntimeError as exc:
            if not self._is_failed_resume(exc):
                raise
            self._log(
                f"{self._provider} session is no longer available; "
                "retrying this turn with a fresh conversation."
            )
            self._agent.forget_session()
            self._turn_count = 0
            self._restarted = True
            return self._agent.generate(prompt, cwd=cwd, timeout=timeout, silent=True)

    def _is_failed_resume(self, exc: RuntimeError) -> bool:
        """Whether ``exc`` is a resumed turn failing because of the resume."""
        if self._agent.session_id is None:
            return False
        if self._provider == "codex":
            # Codex names the cause: the rollout the thread ID points at is gone.
            return _is_missing_codex_rollout(exc)
        if self._provider == "opencode":
            return _is_missing_opencode_session(exc)
        # Claude Code reports a rejected ``--resume`` as a plain nonzero exit
        # with no distinguishing message, and a session it will not resume is
        # unrecoverable, so any failed resumed turn is retried once from a
        # fresh conversation rather than being allowed to kill the run.
        # Timeouts do not reach here: they raise ``subprocess.TimeoutExpired``.
        return self._provider == "claude"

    def _refresh_container(self) -> None:
        if self._docker_sandboxes is None:
            return
        self._agent.executor.container_id = self._docker_sandboxes[self._spec.role]._container_id  # noqa: SLF001  # tracked: #288

    def _repair_workspace_ownership(self) -> None:
        if self._docker_sandboxes is None:
            return
        self._agent.executor.repair_workspace_ownership(uid=os.getuid(), gid=os.getgid())


class AgentShimDriver:
    """Create AgentShim sessions and translate VibeSys execution policy."""

    def __init__(  # noqa: D107  # tracked: #288
        self,
        *,
        provider: str,
        timeout: int | None = None,
        docker_sandboxes: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if provider not in _PROVIDER_CLASSES:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"unknown AgentShim provider {provider!r}; expected one of: "
                f"{sorted(_PROVIDER_CLASSES)}"
            )
        self._provider = provider
        self._provider_cls = _PROVIDER_CLASSES[provider]
        self._timeout = timeout
        self._docker_sandboxes = docker_sandboxes
        self._log = log or _ignore_log
        self._sessions: weakref.WeakSet[AgentShimSession] = weakref.WeakSet()
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe the policy and lifecycle features this driver enforces."""
        return replace(
            AGENTSHIM_CAPABILITIES,
            host_path_grants=self._docker_sandboxes is None,
            container_execution=self._docker_sandboxes is not None,
        )

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one configured AgentShim conversation."""
        if self._closed:
            raise RuntimeError("agent driver is closed")  # noqa: TRY003  # tracked: #288
        if spec.provider != self._provider:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"AgentShimDriver for {self._provider!r} cannot create a {spec.provider!r} session"
            )
        in_container = self._docker_sandboxes is not None
        if spec.policy.containerized != in_container:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "agent session container policy does not match the configured "
                "AgentShim execution mode"
            )

        event_handler = _AgentShimEventHandler()
        if in_container:
            assert self._docker_sandboxes is not None  # noqa: S101  # tracked: #288
            sandbox = self._docker_sandboxes.get(spec.role)
            if sandbox is None:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    f"no AgentShim Docker sandbox configured for role {spec.role!r}"
                )
            agent = self._provider_cls(
                model=spec.model,
                event_handler=event_handler,
                executor=DockerCommandExecutor(sandbox._container_id),  # noqa: SLF001  # tracked: #288
            )
        else:
            agent = self._provider_cls(model=spec.model, event_handler=event_handler)
        if spec.environment:
            agent.env = {**agent.env, **dict(spec.environment)}
        # A subprocess cwd does not rewrite $PWD, and bun-based CLIs (opencode)
        # trust $PWD over the real cwd for workspace config discovery: a stale
        # value makes them miss <workspace>/opencode.json. Let the cwd win.
        agent.env = {key: value for key, value in agent.env.items() if key != "PWD"}

        if not in_container:
            resources = declare_agent_host_resources(
                agent.env,
                binary_path=getattr(agent, "binary_path", None),
                provider=self._provider,
                additional=spec.policy.host_resources,
            )
            agent.sandbox = build_host_sandbox(
                spec.workspace,
                env=agent.env,
                resources=resources,
                log=self._log,
                project_path_policy=spec.policy.project_paths,
                require_enforcement=spec.policy.require_enforcement,
            )

        if spec.reasoning_effort is not None and self._provider in _REASONING_EFFORT_PROVIDERS:
            cast("CodexCodingAgent | ClaudeCodeCodingAgent", agent).set_reasoning_effort(
                spec.reasoning_effort
            )

        session = AgentShimSession(
            agent=agent,
            spec=spec,
            provider=self._provider,
            timeout=self._timeout,
            event_handler=event_handler,
            docker_sandboxes=self._docker_sandboxes,
            log=self._log,
        )
        self._sessions.add(session)
        return session

    def close(self) -> None:
        """Close every session created by this driver, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()
