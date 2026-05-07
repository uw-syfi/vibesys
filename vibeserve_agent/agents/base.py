"""Backend-agnostic agent runner protocol.

Two implementations live alongside this module:

- :mod:`vibeserve_agent.agents.deepagents_runner` wraps the existing
  ``deepagents`` + ``langchain`` stack used by every loop today.
- :mod:`vibeserve_agent.agents.cli_runner` wraps an
  ``agentshim``-backed ``libs.agent_cli`` compatibility layer, which drives
  external coding-agent CLIs
  (Claude Code, Gemini, Codex, Opencode).

The simple loop calls a single ``invoke()`` method per (iteration × phase).
There is intentionally no separate ``Session`` type — today's loops always
build a fresh agent for each call (clean context window) and ``libs.agent_cli``
is one-shot at the Python layer, so a reusable session would either be a thin
struct or would lie about reuse semantics on one of the backends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from langchain_core.tools import BaseTool
from libs.agent_cli.base import MCPServerSpec
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AgentRunner(Protocol):
    """Backend-agnostic agent invoker. One instance per loop run."""

    backend_name: str
    """Diagnostic identifier — ``"deepagents"`` or ``"cli"``."""

    def invoke(
        self,
        *,
        # static per-task config
        kind: str,
        workspace: Path,
        system_prompt: str,
        env: dict[str, str] | None = None,
        # dynamic per-call config
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
    ) -> T:
        """Run an agent and return a structured response.

        Args:
            kind: One of ``"implementer"``, ``"judge"``, ``"perf_eval"``.
                The deepagents runner uses this to pick the right
                ``BaseSandbox`` from its backends dict; both backends use
                it to derive the human-facing label.
            workspace: Workspace root for this phase. The deepagents runner
                ignores it (its sandbox already encapsulates a working
                directory); the cli runner passes it through as ``cwd``
                to the underlying CLI process.
            system_prompt: Rendered Jinja2 system prompt (per phase).
            env: Optional environment overrides (e.g. ``CUDA_VISIBLE_DEVICES``).
            user_prompt: Rendered Jinja2 user prompt (per iteration).
            response_cls: Pydantic model class the agent should produce.
            fallback_factory: Constructs a default ``response_cls`` instance
                used when the agent fails to produce a parseable response.
                Implementations must call this rather than raise.
            round_label: Short label used in log headers (e.g. ``"judge #3"``).
            mcp_servers: Optional list of stdio MCP servers to install for
                the duration of this call. The cli runner forwards these to
                the underlying ``CodingAgent``'s
                :meth:`~libs.agent_cli.base.CodingAgent.install_mcp_servers`
                hook (then uninstalls in ``finally``); the deepagents runner
                ignores this kwarg.
            tools: Optional list of in-process LangChain tools to expose to
                the agent for the duration of this call. The deepagents
                runner forwards them to ``create_deep_agent(tools=...)``;
                the cli runner ignores this kwarg (the cli path uses
                ``mcp_servers`` for tool injection). Both kwargs are
                transport-level injection points and contain no domain
                knowledge — loops or wrappers populate them.

        Returns:
            An instance of ``response_cls``, either parsed from the agent's
            output or produced by ``fallback_factory()``.
        """
        ...
