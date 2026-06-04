"""Backend-agnostic agent runner package.

The ``build_agent_runner`` function is added at the bottom once the concrete
runner classes are imported, so the public API of this package is::

    from vibe_serve.agents import AgentRunner, build_agent_runner
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vibe_serve.config import Config
from vibe_serve.constants import DEFAULT_AGENT_BACKEND

from .base import AgentRunner
from .cli_runner import CliAgentRunner
from .deepagents_runner import DeepAgentsRunner

__all__ = ["AgentRunner", "DeepAgentsRunner", "CliAgentRunner", "build_agent_runner"]


def build_agent_runner(
    config: Config,
    *,
    agent_backend: str | None,
    cli_provider: str | None,
    backends: dict[str, Any] | None,
    skills: list[str],
    skill_source_dirs: list[Path],
    model: Any,
    model_name: str,
    run_log_file,
    use_docker: bool,
    use_modal: bool = False,
    log_dir: Path | None = None,
) -> AgentRunner:
    """Construct the right :class:`AgentRunner` for the requested backend.

    Args:
        config: Parsed :class:`~vibe_serve.config.Config`; the ``[agent]``
            section drives backend/provider/model/timeout selection.
        agent_backend: CLI override; if set, takes precedence over
            ``config.agent.backend``. Falls back to
            :data:`vibe_serve.constants.DEFAULT_AGENT_BACKEND` (``"cli"``).
        cli_provider: CLI override for ``config.agent.cli_provider``.
        backends: Mapping ``{"implementer": BaseSandbox, "judge": ..., "perf_eval": ...}``.
            Required for the deepagents path; ignored for the cli path.
        skills: Skill directory names already materialized in the workspace,
            for the deepagents backend (passed through to ``create_deep_agent``).
        skill_source_dirs: Absolute source paths of skill directories — the
            cli runner copies these into the workspace's ``.claude/skills/``
            on each invoke.
        model: Result of :func:`vibe_serve.llm_client._build_model` — only
            used by the deepagents path. The cli path uses ``model_name``.
        model_name: Bare model id (e.g. ``"claude-sonnet-4-6"``) — used both
            for the cli runner and for the deepagents ``AgentLogger`` prefix.
        run_log_file: Open text-mode file handle for the loop's main log.
        use_docker: Whether the loop is running with ``--docker``. The cli
            runner refuses Docker mode (CLI tools have their own sandboxing).
        log_dir: Optional directory where the cli runner appends per-invoke
            usage/cost records (``<log_dir>/usage.jsonl``). Ignored by the
            deepagents backend, which already shows live token counts in
            its callback prefix and would need a separate cleanup for JSONL
            persistence.

    Raises:
        SystemExit: If the requested backend is unknown, the cli backend was
            combined with ``--docker``, or the cli backend was selected
            without a provider.
    """
    agent_cfg = config.agent
    backend = agent_backend or agent_cfg.backend or DEFAULT_AGENT_BACKEND

    if backend == "deepagents":
        if backends is None:
            raise SystemExit(
                "internal error: build_agent_runner called with backend='deepagents' "
                "but no backends dict was provided"
            )
        return DeepAgentsRunner(
            model=model,
            backends=backends,
            skills=skills,
            model_name=model_name,
            run_log_file=run_log_file,
        )

    if backend == "cli":
        provider = cli_provider or agent_cfg.cli_provider or "codex"
        docker_sandboxes = None
        modal_sandboxes = None
        if use_docker or use_modal:
            # Both container backends reuse the DOCKER_PROVIDER_ENV registry
            # since the per-provider install + env requirements are
            # identical (we need node/npm, codex binary, PYTHONPATH, etc).
            from .cli_docker import DOCKER_PROVIDER_ENV

            if provider not in DOCKER_PROVIDER_ENV:
                flag = "--modal" if use_modal else "--docker"
                raise SystemExit(
                    f"--cli-provider {provider!r} is not yet supported with {flag}; "
                    f"supported: {sorted(DOCKER_PROVIDER_ENV)}"
                )
            if use_modal:
                modal_sandboxes = backends
            else:
                docker_sandboxes = backends
        timeout = agent_cfg.cli_timeout
        # cli_model overrides model.name for the CLI tool. If not set,
        # pass None so the CLI tool uses its own default.
        cli_model = agent_cfg.cli_model
        return CliAgentRunner(
            provider=provider,
            model=cli_model,
            skills=skill_source_dirs,
            model_name=model_name or cli_model or provider,
            timeout=timeout,
            run_log_file=run_log_file,
            docker_sandboxes=docker_sandboxes,
            modal_sandboxes=modal_sandboxes,
            log_dir=log_dir,
        )

    raise SystemExit(f"unknown agent backend: {backend!r}")
