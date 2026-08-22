"""CLI implementation of :class:`AgentRunner`.

Wraps the local ``vibesys._agent_cli`` compatibility layer, which is backed by
the open-source ``agentshim`` package plus a few repo-specific extensions for
Docker command routing and per-invocation MCP install/uninstall. Each
``invoke()``:

1. Materializes any configured skill directories into the workspace's
   ``.claude/skills/`` so Claude Code (and any other tool that picks them
   up) can use them.
2. Builds a combined prompt = ``system_prompt + user_prompt + JSON-schema hint``
   because CLI tools don't expose a separate "system" slot.
3. Passes :class:`AgentLogger` as the CLI event handler so on-screen output
   matches the deepagents path.
4. Calls ``agent.generate(prompt, cwd=workspace, …)``.
5. Reuses :func:`vibesys.agent_runner.parse_typed_response_text`
   to coerce the returned string back into the requested Pydantic model.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterable  # noqa: TC003  # tracked: #288
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, TypeVar, cast

from langchain_core.tools import BaseTool  # noqa: TC002  # tracked: #288
from pydantic import BaseModel

from vibesys._agent_cli.base import CodingAgent, MCPServerSpec  # noqa: TC001  # tracked: #288
from vibesys._agent_cli.claude import ClaudeCodeCodingAgent
from vibesys._agent_cli.codex import CodexCodingAgent
from vibesys._agent_cli.gemini import GeminiCodingAgent
from vibesys._agent_cli.opencode import OpencodeCodingAgent
from vibesys.agent_runner import (
    log_and_print,
    log_json_and_print,
    log_markdown_and_print,
    log_prompt_markdown_and_print,
    parse_typed_response_text,
)
from vibesys.agents.callbacks import AgentLogger
from vibesys.agents.cli_common import (
    agent_label,
    build_schema_hint,
    materialize_native_output_schema,
    materialize_skills,
)
from vibesys.agents.host_resource_declarations import declare_agent_host_resources
from vibesys.agents.progress import AgentProgress  # noqa: TC001  # tracked: #288
from vibesys.constants import ComputeBackend  # noqa: TC001  # tracked: #288
from vs_sandbox import HostResource, ProjectPathPolicy, build_host_sandbox

T = TypeVar("T", bound=BaseModel)


class _ProviderFactory(Protocol):
    """Constructor signature shared by every CLI provider agent class."""

    def __call__(
        self,
        model: str | None = None,
        event_handler: Any | None = None,  # noqa: ANN401  # tracked: #288
        *,
        executor: Any | None = None,  # noqa: ANN401  # tracked: #288
    ) -> CodingAgent: ...


# Providers whose CLI accepts a reasoning-effort level. Others silently
# ignore the setting rather than failing, so the gate is explicit.
_REASONING_EFFORT_PROVIDERS = frozenset({"codex", "claude"})

_PROVIDER_CLASSES: dict[str, _ProviderFactory] = {
    "claude": ClaudeCodeCodingAgent,
    "gemini": GeminiCodingAgent,
    "codex": CodexCodingAgent,
    "opencode": OpencodeCodingAgent,
}

# Codex provider threads retain useful implementation context, but long
# tool-heavy systems turns can approach the provider context limit even when
# the durable workspace contains everything needed to continue.  Keep one
# adjacent continuation, then start a fresh provider thread on the third turn.
_MAX_CODEX_SESSION_TURNS = 2
_MAX_CODEX_SESSION_INPUT_TOKENS = 10_000_000
_MAX_CODEX_SESSION_DURATION_MS = 600_000

_PYTHON_MCP_COMMANDS = frozenset({"python", "python3"})


def _resolve_mcp_interpreter(
    servers: list[MCPServerSpec], *, in_container: bool
) -> list[MCPServerSpec]:
    """Pin ``python`` MCP servers to the interpreter running VibeSys.

    A host agent inherits an interactive login shell's environment, not the
    launcher's, so a bare ``python`` resolves against the user's PATH and may
    be missing or lack ``mcp``/``pydantic``. The CLI then fails to spawn the
    stdio server and the agent silently loses its tools (profiler analysis,
    issue board). The interpreter running VibeSys always has those
    dependencies, and the host sandbox already imports ``sys.prefix`` and
    ``sys.base_prefix`` read-only. Container executors keep ``python``: the
    image provides its own and a host interpreter path does not exist there.
    """
    if in_container:
        return servers
    return [
        replace(server, command=sys.executable)
        if server.command in _PYTHON_MCP_COMMANDS
        else server
        for server in servers
    ]


def _is_missing_codex_rollout(exc: RuntimeError) -> bool:
    """Return whether Codex rejected a stale resumable thread."""
    message = str(exc)
    return "thread/resume failed" in message and "no rollout found" in message


def _heavy_codex_turn_reason(agent: CodingAgent) -> str | None:
    """Explain why the previous Codex turn is too heavy to resume efficiently."""
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


class CliAgentRunner:
    """:class:`AgentRunner` backed by ``vibesys._agent_cli`` CLI agents."""

    backend_name = "cli"

    def __init__(  # noqa: ANN204, D107, PLR0913  # tracked: #288
        self,
        *,
        provider: str,
        model: str | None = None,
        skills: list[Path] | None = None,
        compute_backend: ComputeBackend | None = None,
        model_name: str | None = None,
        timeout: int | None = None,
        run_log_file: TextIO | None = None,
        docker_sandboxes: dict[str, Any] | None = None,
        host_resources: Iterable[HostResource] = (),
        log_dir: Path | None = None,
        default_reasoning_effort: str | None = None,
        role_models: dict[str, str] | None = None,
        role_reasoning_efforts: dict[str, str] | None = None,
        project_path_policy: ProjectPathPolicy | None = None,
        require_host_sandbox: bool = False,
    ):
        if provider not in _PROVIDER_CLASSES:
            raise SystemExit(  # noqa: TRY003  # tracked: #288
                f"unknown cli provider {provider!r}; expected one of: {sorted(_PROVIDER_CLASSES)}"
            )
        self._provider = provider
        self._provider_cls = _PROVIDER_CLASSES[provider]
        self._model = model
        self._default_reasoning_effort = default_reasoning_effort
        self._role_models = dict(role_models or {})
        self._role_reasoning_efforts = dict(role_reasoning_efforts or {})
        self._skills: list[Path] = list(skills or [])
        self._compute_backend = compute_backend
        self._model_name = model_name
        self._timeout = timeout
        self._run_log_file = run_log_file
        self._docker_sandboxes = docker_sandboxes
        # Additional resource intent is provider-independent. The declaration
        # policy combines it with provider defaults only on the local CLI path.
        self._host_resources = tuple(host_resources)
        self._project_path_policy = project_path_policy
        self._require_host_sandbox = require_host_sandbox
        # When set, each ``invoke()`` appends one JSON record to
        # ``<log_dir>/usage.jsonl`` capturing per-call token counts and
        # cost. ``None`` disables the file write (legacy callers, unit
        # tests that don't care about usage).
        self._log_dir = log_dir
        # Cache agent instances per kind so session IDs persist across
        # invocations (enables conversation continuation). Experiment chat is
        # the exception: its history is carried explicitly in each prompt, so
        # it must not depend on provider-side session state.
        self._agents: dict[str, CodingAgent] = {}
        self._session_turn_counts: dict[str, int] = {}

    def invoke(  # noqa: D102, PLR0913  # tracked: #288
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        env: dict[str, str] | None = None,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,  # noqa: ARG002 — deepagents-only injection point; cli uses mcp_servers
        reuse_session: bool | None = None,
        session_key: str | None = None,
    ) -> T:
        native_schema_path: str | None = None
        native_schema_supported = bool(
            getattr(self._provider_cls, "supports_native_output_schema", False)
            and callable(getattr(self._provider_cls, "set_output_schema_path", None))
        )
        if native_schema_supported:
            try:
                native_schema_path = materialize_native_output_schema(
                    workspace,
                    response_cls,
                    allow_arbitrary_keys=getattr(
                        self._provider_cls,
                        "native_output_schema_allows_arbitrary_keys",
                        False,
                    ),
                )
            except (OSError, TypeError, ValueError) as exc:
                log_and_print(
                    f"[structured-output] native schema unavailable for "
                    f"{response_cls.__name__}; using prompt fallback: "
                    f"{type(exc).__name__}: {exc}",
                    self._run_log_file,
                )
        # Providers that read the schema themselves at command-build time (e.g.
        # Claude Code's inline ``--json-schema``) need an absolute path that
        # resolves independently of the subprocess working directory; others
        # keep the workspace-relative path their CLI resolves against its cwd.
        schema_arg = native_schema_path
        if native_schema_path is not None and getattr(
            self._provider_cls, "native_output_schema_wants_absolute_path", False
        ):
            schema_arg = str(workspace / native_schema_path)
        schema_hint = "" if native_schema_path else build_schema_hint(response_cls)
        combined_prompt = f"{system_prompt}\n\n{user_prompt}{schema_hint}"
        text = self._generate(
            kind=kind,
            workspace=workspace,
            env=env,
            combined_prompt=combined_prompt,
            round_label=round_label,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
            output_schema_path=schema_arg,
        )
        label = agent_label(kind)
        parsed = parse_typed_response_text(text, response_cls)
        if parsed is None:
            log_and_print(
                f"\n=== {label} ROUND OUTPUT (missing response) ===",
                self._run_log_file,
            )
            log_and_print(
                f"No structured response received from {label.lower()}.",
                self._run_log_file,
            )
            if text:
                log_and_print(
                    f"\n=== {label} ROUND OUTPUT (raw output) ===",
                    self._run_log_file,
                )
                log_markdown_and_print(text, self._run_log_file)
            return fallback_factory()

        log_and_print(
            f"\n=== {label} ROUND OUTPUT ===",
            self._run_log_file,
        )
        log_json_and_print(parsed.model_dump_json(indent=2), self._run_log_file)
        return parsed

    def invoke_text(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        env: dict[str, str] | None = None,
        user_prompt: str,
        round_label: str,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,  # noqa: ARG002 — deepagents-only
        reuse_session: bool | None = None,
        session_key: str | None = None,
    ) -> str:
        """Run a conversational CLI agent without requesting structured JSON."""
        text = self._generate(
            kind=kind,
            workspace=workspace,
            env=env,
            combined_prompt=f"{system_prompt}\n\n{user_prompt}",
            round_label=round_label,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
            output_schema_path=None,
        )
        label = agent_label(kind)
        if text:
            log_and_print(f"\n=== {label} ROUND OUTPUT ===", self._run_log_file)
            log_markdown_and_print(text, self._run_log_file)
        else:
            log_and_print(
                f"\n=== {label} ROUND OUTPUT (missing response) ===",
                self._run_log_file,
            )
            log_and_print(f"No response received from {label.lower()}.", self._run_log_file)
        return text

    def _generate(  # noqa: C901, PLR0912, PLR0913, PLR0915  # tracked: #288
        self,
        *,
        kind: str,
        workspace: Path,
        env: dict[str, str] | None,
        combined_prompt: str,
        round_label: str,
        invocation_id: str | None,
        progress: AgentProgress | None,
        mcp_servers: list[MCPServerSpec] | None,
        reuse_session: bool | None,
        session_key: str | None,
        output_schema_path: str | None,
    ) -> str:
        """Run one CLI generation with shared setup, logging, and cleanup."""
        label = agent_label(kind)
        selected_model = self._role_models.get(kind, self._model)
        configured_reasoning_effort = self._role_reasoning_efforts.get(
            kind, self._default_reasoning_effort
        )
        selected_reasoning_effort = (
            configured_reasoning_effort if self._provider in _REASONING_EFFORT_PROVIDERS else None
        )
        materialize_skills(
            workspace,
            self._skills,
            compute_backend=self._compute_backend,
            log_file=self._run_log_file,
        )

        logger = AgentLogger(
            log_file=self._run_log_file,
            model_name=selected_model or self._model_name,
            agent_label=label,
            progress=progress,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )

        # Reuse or construct the underlying agent. Reusing preserves the
        #    session_id so the CLI tool can resume the conversation. Chat owns
        #    its multi-turn history in the prompt, and provider session IDs can
        #    become unavailable when a sandbox or process changes, so every
        #    chat turn deliberately starts a fresh CLI session.
        reuse_agent = kind != "chat" and (reuse_session if reuse_session is not None else True)
        role_key = f"{kind}:{selected_model}:{selected_reasoning_effort}"
        cache_key = f"{role_key}:{session_key}" if session_key else role_key
        agent = self._agents.get(cache_key) if reuse_agent else None
        if agent is not None:
            # Update the event handler for this invocation's logger.
            agent.event_handler = logger
            # Sandbox may have been restarted with a new container (e.g.
            # reselect_gpu rebuilt it for a different --gpus device); refresh
            # the runner so the next exec targets the live container.
            if self._docker_sandboxes is not None:
                # Dynamic poke: only the docker path constructs agents with a
                # DockerCommandExecutor, which carries container_id.
                executor = getattr(agent, "executor", None)
                executor.container_id = self._docker_sandboxes[kind]._container_id  # pyright: ignore[reportOptionalMemberAccess]  # noqa: SLF001  # tracked: #288
        elif self._docker_sandboxes is not None:
            from vibesys.agents.docker_executor import (  # noqa: PLC0415  # tracked: #288
                DockerCommandExecutor,
            )

            sandbox = self._docker_sandboxes[kind]
            executor = DockerCommandExecutor(sandbox._container_id)  # noqa: SLF001  # tracked: #288
            agent = self._provider_cls(
                model=selected_model,
                event_handler=logger,
                executor=executor,
            )
            self._configure_reasoning_effort(agent, selected_reasoning_effort)
            if reuse_agent:
                self._agents[cache_key] = agent
        else:
            agent = self._provider_cls(model=selected_model, event_handler=logger)
            self._configure_reasoning_effort(agent, selected_reasoning_effort)
            # Host execution path: confine the agent to its workspace at the OS
            # level so it cannot read or modify sibling runs or unrelated host
            # files (issue #149). Container executors above are already
            # externally sandboxed and deliberately skip this.
            resources = declare_agent_host_resources(
                agent.env,
                binary_path=getattr(agent, "binary_path", None),
                provider=self._provider,
                additional=self._host_resources,
            )
            agent.sandbox = build_host_sandbox(
                Path(workspace),
                env=agent.env,
                resources=resources,
                log=lambda msg: log_and_print(msg, self._run_log_file),
                project_path_policy=self._project_path_policy,
                require_enforcement=self._require_host_sandbox,
            )
            if reuse_agent:
                self._agents[cache_key] = agent

        renewal_reason: str | None = None
        if self._provider == "codex" and reuse_agent:
            if self._session_turn_counts.get(cache_key, 0) >= _MAX_CODEX_SESSION_TURNS:
                renewal_reason = f"{_MAX_CODEX_SESSION_TURNS} successful turns"
            else:
                renewal_reason = _heavy_codex_turn_reason(agent)
        if renewal_reason is not None:
            log_and_print(
                f"[{label}] renewing Codex thread after {renewal_reason}; durable workspace "
                "state remains authoritative.",
                self._run_log_file,
            )
            cast("CodexCodingAgent", agent).session_id = None
            self._session_turn_counts[cache_key] = 0

        # Set this on every turn, including plain-text turns, so a reused
        # provider session cannot retain the previous response contract.
        schema_setter = getattr(agent, "set_output_schema_path", None)
        if callable(schema_setter):
            schema_setter(output_schema_path)
        elif output_schema_path is not None:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"{type(agent).__name__} advertised native output schemas "
                "without implementing set_output_schema_path()"
            )

        # Layer GPU env vars on top of the captured interactive env so the
        # spawned subprocess inherits CUDA_VISIBLE_DEVICES. Containerised
        # modes bake env vars into the container at start(), so skip here.
        _in_container = bool(self._docker_sandboxes)
        if env and not _in_container:
            agent.env = {**agent.env, **env}
        workspace_arg = None if _in_container else str(workspace)

        # Install per-provider MCP server config (file under workspace
        #    for claude/gemini/opencode, runtime --config flags for codex).
        #    Wrapped in try/finally so a crash in generate() still cleans up.
        if mcp_servers:
            mcp_servers = _resolve_mcp_interpreter(mcp_servers, in_container=_in_container)
            agent.install_mcp_servers(workspace, mcp_servers)

        log_and_print(
            f"\n=== {label} ROUND START: {round_label} ===",
            self._run_log_file,
        )
        log_and_print(
            f"backend: cli, provider: {self._provider}, "
            f"model: {selected_model or self._model_name}, "
            f"reasoning_effort: {selected_reasoning_effort or 'provider_default'}, "
            f"cwd: {workspace}",
            self._run_log_file,
        )
        log_and_print("--- input ---", self._run_log_file)
        log_prompt_markdown_and_print(combined_prompt, self._run_log_file)

        # Run the agent. Wrap exceptions to surface them in the run log
        #    before re-raising. The ``finally`` clause runs both cleanups —
        #    per-provider MCP config (so the next phase starts clean even
        #    if generate() raises) and the per-invocation usage record
        #    (tokens were spent either way, and an audit gap on failure
        #    defeats the purpose).
        agent_error: BaseException | None = None
        try:
            try:
                text = agent.generate(
                    combined_prompt,
                    cwd=workspace_arg,
                    timeout=self._timeout,
                    silent=True,
                )
            except RuntimeError as exc:
                # Codex rollouts may be evicted after a long turn even though
                # the local agent still has the thread ID. The full prompt and
                # workspace progress files carry the durable context, so retry
                # once as a fresh thread instead of aborting the outer loop.
                if not (
                    self._provider == "codex"
                    and getattr(agent, "session_id", None)
                    and _is_missing_codex_rollout(exc)
                ):
                    raise
                log_and_print(
                    f"[{label}] Codex session is no longer available; "
                    "retrying with a fresh thread.",
                    self._run_log_file,
                )
                cast("CodexCodingAgent", agent).session_id = None
                self._session_turn_counts[cache_key] = 0
                text = agent.generate(
                    combined_prompt,
                    cwd=workspace_arg,
                    timeout=self._timeout,
                    silent=True,
                )
            if reuse_agent:
                self._session_turn_counts[cache_key] = (
                    self._session_turn_counts.get(cache_key, 0) + 1
                )
        except BaseException as exc:
            agent_error = exc
            if isinstance(exc, Exception):
                log_and_print(
                    f"\n=== {label} ROUND ERROR: {round_label} ===",
                    self._run_log_file,
                )
                log_and_print(f"{type(exc).__name__}: {exc}", self._run_log_file)
            raise
        finally:
            cleanup_error: Exception | None = None
            if mcp_servers:
                try:
                    agent.uninstall_mcp_servers(workspace, mcp_servers)
                except Exception as cleanup_exc:  # noqa: BLE001  # tracked: #288
                    cleanup_error = cleanup_exc
                    if agent_error is not None:
                        log_and_print(
                            f"[{label}] MCP config cleanup failed while preserving the "
                            f"original agent error: {cleanup_exc}",
                            self._run_log_file,
                        )
            if self._docker_sandboxes is not None:
                try:
                    executor = agent.executor
                    executor.repair_workspace_ownership(uid=os.getuid(), gid=os.getgid())
                except Exception as cleanup_exc:  # noqa: BLE001  # tracked: #288
                    if cleanup_error is None:
                        cleanup_error = cleanup_exc
                    else:
                        log_and_print(
                            f"[{label}] workspace ownership repair also failed: {cleanup_exc}",
                            self._run_log_file,
                        )
                    if agent_error is not None:
                        log_and_print(
                            f"[{label}] workspace ownership repair failed while preserving "
                            f"the original agent error: {cleanup_exc}",
                            self._run_log_file,
                        )
            self._write_usage_record(
                kind=kind,
                round_label=round_label,
                agent=agent,
                model_name=selected_model or self._model_name,
                reasoning_effort=selected_reasoning_effort,
            )
            if agent_error is None and cleanup_error is not None:
                raise cleanup_error
        return text

    def _configure_reasoning_effort(self, agent: CodingAgent, reasoning_effort: str | None) -> None:
        """Apply provider-specific reasoning controls to a newly built CLI agent.

        Codex takes the level as a ``--config`` override and Claude Code as a
        ``--effort`` session flag, but both expose the same
        ``set_reasoning_effort`` hook, so the caller does not branch on which.
        """
        if reasoning_effort is None or self._provider not in _REASONING_EFFORT_PROVIDERS:
            return
        cast("CodexCodingAgent | ClaudeCodeCodingAgent", agent).set_reasoning_effort(
            reasoning_effort
        )

    def _write_usage_record(
        self,
        *,
        kind: str,
        round_label: str,
        agent: Any,  # noqa: ANN401  # tracked: #288
        model_name: str | None,
        reasoning_effort: str | None,
    ) -> None:
        """Append one JSONL record to ``<log_dir>/usage.jsonl`` for this call.

        Reads ``agent._last_session`` (stashed by
        :meth:`CLICodingAgent.generate`) for the cumulative usage block
        captured from the underlying CLI's final event.  Each record is a
        self-contained JSON object so ``jq -s`` / ``pandas.read_json
        (lines=True)`` can consume it without schema knowledge.

        Any :class:`OSError` while writing is logged and swallowed — a
        usage-log write failure must never break the agent loop.
        """
        if self._log_dir is None:
            return
        session = getattr(agent, "_last_session", None)
        usage = getattr(session, "final_usage", None) if session is not None else None
        usage = usage or {}
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "round_label": round_label,
            "provider": self._provider,
            "model": model_name,
            "reasoning_effort": reasoning_effort,
            "input_tokens": usage.get("input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_cost_usd": (
                getattr(session, "total_cost_usd", None) if session is not None else None
            ),
            "duration_ms": (getattr(session, "duration_ms", None) if session is not None else None),
        }
        target = self._log_dir / "usage.jsonl"
        try:
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            log_and_print(
                f"[usage] failed to append {target}: {type(exc).__name__}: {exc}",
                self._run_log_file,
            )
