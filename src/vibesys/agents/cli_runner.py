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
import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, TypeVar, cast

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from vibesys._agent_cli import hostsandbox
from vibesys._agent_cli.base import CodingAgent, MCPServerSpec
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
from vibesys.agents.host_resource_declarations import declare_agent_host_resources
from vibesys.agents.progress import AgentProgress
from vibesys.host_resources import HostResource

T = TypeVar("T", bound=BaseModel)


class _ProviderFactory(Protocol):
    """Constructor signature shared by every CLI provider agent class."""

    def __call__(
        self,
        model: str | None = None,
        event_handler: Any | None = None,
        *,
        executor: Any | None = None,
    ) -> CodingAgent: ...


_PROVIDER_CLASSES: dict[str, _ProviderFactory] = {
    "claude": ClaudeCodeCodingAgent,
    "gemini": GeminiCodingAgent,
    "codex": CodexCodingAgent,
    "opencode": OpencodeCodingAgent,
}


def _agent_label(kind: str) -> str:
    """Convert ``"perf_eval"`` to ``"Perf Eval"``, etc."""
    return kind.replace("_", " ").title()


def _is_missing_codex_rollout(exc: RuntimeError) -> bool:
    """Return whether Codex rejected a stale resumable thread."""
    message = str(exc)
    return "thread/resume failed" in message and "no rollout found" in message


# Per-provider CLI skill-discovery paths, matching upstream
# vibesys-skills install.sh conventions. Each CLI tool auto-loads
# skills from a flat directory of `<skill-name>/SKILL.md`.
_CLI_SKILL_DIRS: tuple[str, ...] = (
    ".claude/skills",
    ".agents/skills",
    ".gemini/skills",
    ".cursor/skills",
    ".opencode/skills",
)


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Return all skill directories reachable under *root*.

    A "skill directory" is any directory containing a ``SKILL.md`` file.
    This accepts both flat layouts (``.agents/skills/<name>/SKILL.md``) and
    the tier-organized layout from vibesys-skills
    (``skills/<tier>/<name>/SKILL.md``).
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return [p.parent for p in root.rglob("SKILL.md")]


def _materialize_skills(
    workspace: Path, skill_dirs: list[Path], log_file: TextIO | None = None
) -> None:
    """Copy each skill directory into the per-CLI skill-discovery paths.

    Walks each ``skill_dirs`` entry for ``SKILL.md`` files and flattens each
    parent directory into every path under ``_CLI_SKILL_DIRS`` (one per CLI
    convention: ``.claude/skills``, ``.agents/skills``, ``.gemini/skills``,
    ``.cursor/skills``, ``.opencode/skills``). This makes the skills visible
    to whichever CLI provider ends up running in the workspace without the
    caller having to know which one was picked.

    Existing destinations are replaced so skill edits are picked up across
    iterations. Errors are logged but never raised — the loop should still
    make progress even if a skill fails to materialize.
    """
    if not skill_dirs:
        return

    # Collect every skill dir across all source roots, de-duplicated by name
    # (last writer wins — matches the prior single-source behaviour when the
    # same skill name appears in multiple roots).
    discovered: dict[str, Path] = {}
    for src in skill_dirs:
        for skill_dir in _discover_skill_dirs(src):
            discovered[skill_dir.name] = skill_dir

    if not discovered:
        return

    skip_names = {".git", "repos", "__pycache__"}
    skip_ignore = shutil.ignore_patterns(*skip_names)

    for target_rel in _CLI_SKILL_DIRS:
        target_root = workspace / target_rel
        target_root.mkdir(parents=True, exist_ok=True)
        for name, src_skill in discovered.items():
            dest = target_root / name
            try:
                if dest.exists() or dest.is_symlink():
                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.copytree(src_skill, dest, symlinks=True, ignore=skip_ignore)
            except OSError as exc:
                if log_file is not None:
                    log_and_print(
                        f"[skills] failed to materialize {src_skill} -> "
                        f"{dest}: {type(exc).__name__}: {exc}",
                        log_file,
                    )


def _build_schema_hint(response_cls: type[BaseModel]) -> str:
    """Render a short instruction telling the CLI tool what JSON to emit."""
    schema = json.dumps(response_cls.model_json_schema(), indent=2)
    return (
        "\n\n--\n"
        "Return EXACTLY one JSON object that conforms to the schema below. "
        "Do not wrap it in markdown fences. Do not include any extra prose "
        "before or after the JSON object.\n\n"
        f"Schema for {response_cls.__name__}:\n{schema}\n"
    )


class CliAgentRunner:
    """:class:`AgentRunner` backed by ``vibesys._agent_cli`` CLI agents."""

    backend_name = "cli"

    def __init__(
        self,
        *,
        provider: str,
        model: str | None = None,
        skills: list[Path] | None = None,
        model_name: str | None = None,
        timeout: int | None = None,
        run_log_file: TextIO | None = None,
        docker_sandboxes: dict[str, Any] | None = None,
        modal_sandboxes: dict[str, Any] | None = None,
        host_resources: Iterable[HostResource] = (),
        log_dir: Path | None = None,
    ):
        if provider not in _PROVIDER_CLASSES:
            raise SystemExit(
                f"unknown cli provider {provider!r}; expected one of: {sorted(_PROVIDER_CLASSES)}"
            )
        if docker_sandboxes is not None and modal_sandboxes is not None:
            raise SystemExit(
                "internal error: cli runner got both docker_sandboxes and "
                "modal_sandboxes — exactly one should be set"
            )
        self._provider = provider
        self._provider_cls = _PROVIDER_CLASSES[provider]
        self._model = model
        self._skills: list[Path] = list(skills or [])
        self._model_name = model_name
        self._timeout = timeout
        self._run_log_file = run_log_file
        self._docker_sandboxes = docker_sandboxes
        self._modal_sandboxes = modal_sandboxes
        # Additional resource intent is provider-independent. The declaration
        # policy combines it with provider defaults only on the local CLI path.
        self._host_resources = tuple(host_resources)
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

    def invoke(
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
    ) -> T:
        schema_hint = _build_schema_hint(response_cls)
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
        )
        label = _agent_label(kind)
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

    def invoke_text(
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
        )
        label = _agent_label(kind)
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

    def _generate(
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
    ) -> str:
        """Run one CLI generation with shared setup, logging, and cleanup."""
        label = _agent_label(kind)
        _materialize_skills(workspace, self._skills, log_file=self._run_log_file)

        logger = AgentLogger(
            log_file=self._run_log_file,
            model_name=self._model_name,
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
        reuse_agent = kind != "chat"
        agent = self._agents.get(kind) if reuse_agent else None
        if agent is not None:
            # Update the event handler for this invocation's logger.
            agent.event_handler = logger
            # Sandbox may have been restarted with a new container (e.g.
            # reselect_gpu rebuilt it for a different --gpus device, or the
            # Modal sandbox was recreated after a fallback restart); refresh
            # the runner so the next exec targets the live container.
            if self._docker_sandboxes is not None:
                # Dynamic poke: only the docker path constructs agents with a
                # DockerCommandExecutor, which carries container_id.
                executor = getattr(agent, "executor", None)
                executor.container_id = self._docker_sandboxes[kind]._container_id  # pyright: ignore[reportOptionalMemberAccess]
            # ModalCommandExecutor reads ``_modal_sandbox._sandbox`` on every
            # ``run()``, so a fallback-triggered sandbox restart is picked up
            # automatically — no per-invocation refresh needed here.
        elif self._docker_sandboxes is not None:
            from vibesys.agents.docker_executor import DockerCommandExecutor

            sandbox = self._docker_sandboxes[kind]
            executor = DockerCommandExecutor(sandbox._container_id)
            agent = self._provider_cls(
                model=self._model,
                event_handler=logger,
                executor=executor,
            )
            if reuse_agent:
                self._agents[kind] = agent
        elif self._modal_sandboxes is not None:
            from vibesys.agents.modal_executor import ModalCommandExecutor

            sandbox = self._modal_sandboxes[kind]
            executor = ModalCommandExecutor(sandbox)
            agent = self._provider_cls(
                model=self._model,
                event_handler=logger,
                executor=executor,
            )
            if self._provider == "codex" and hasattr(agent, "base_config_args"):
                # Codex-only attribute, guarded by the hasattr check above.
                codex_agent = cast(CodexCodingAgent, agent)
                codex_agent.base_config_args.extend(
                    [
                        "--config",
                        'cli_auth_credentials_store="file"',
                        "--config",
                        'forced_login_method="chatgpt"',
                    ]
                )
            if reuse_agent:
                self._agents[kind] = agent
        else:
            agent = self._provider_cls(model=self._model, event_handler=logger)
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
            agent.sandbox = hostsandbox.build(
                Path(workspace),
                env=agent.env,
                resources=resources,
                log=lambda msg: log_and_print(msg, self._run_log_file),
            )
            if reuse_agent:
                self._agents[kind] = agent

        # Layer GPU env vars on top of the captured interactive env so the
        # spawned subprocess inherits CUDA_VISIBLE_DEVICES. Containerised
        # modes bake env vars into the container at start(), so skip here.
        _in_container = bool(self._docker_sandboxes or self._modal_sandboxes)
        if env and not _in_container:
            agent.env = {**agent.env, **env}
        workspace_arg = None if _in_container else str(workspace)

        # Install per-provider MCP server config (file under workspace
        #    for claude/gemini/opencode, runtime --config flags for codex).
        #    Wrapped in try/finally so a crash in generate() still cleans up.
        if mcp_servers:
            agent.install_mcp_servers(workspace, mcp_servers)

        log_and_print(
            f"\n=== {label} ROUND START: {round_label} ===",
            self._run_log_file,
        )
        log_and_print(
            f"backend: cli, provider: {self._provider}, model: {self._model_name}, "
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
                cast(CodexCodingAgent, agent).session_id = None
                text = agent.generate(
                    combined_prompt,
                    cwd=workspace_arg,
                    timeout=self._timeout,
                    silent=True,
                )
        except Exception as exc:
            log_and_print(
                f"\n=== {label} ROUND ERROR: {round_label} ===",
                self._run_log_file,
            )
            log_and_print(f"{type(exc).__name__}: {exc}", self._run_log_file)
            raise
        finally:
            if mcp_servers:
                agent.uninstall_mcp_servers(workspace, mcp_servers)
            self._write_usage_record(kind=kind, round_label=round_label, agent=agent)
        return text

    def _write_usage_record(self, *, kind: str, round_label: str, agent: Any) -> None:
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
            "model": self._model_name,
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
