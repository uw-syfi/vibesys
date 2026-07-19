"""Deepagents implementation of :class:`AgentRunner`.

Wraps ``deepagents.create_deep_agent`` and the existing
``vibesys.agent_runner.run_typed_agent`` plumbing — no behavior
change vs. what the simple loop did before this abstraction landed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, TypeVar

from deepagents import create_deep_agent
from langchain.agents.structured_output import AutoStrategy
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from vibesys._agent_cli.base import MCPServerSpec
from vibesys.agent_runner import (
    DEFAULT_MAX_TEXT_LEN,
    log_agent_config,
    run_typed_agent,
)
from vibesys.agents.callbacks import AgentLogger
from vibesys.agents.progress import AgentProgress

T = TypeVar("T", bound=BaseModel)


def _agent_label(kind: str) -> str:
    """Convert ``"perf_eval"`` to ``"Perf Eval"``, etc."""
    return kind.replace("_", " ").title()


class DeepAgentsRunner:
    """:class:`AgentRunner` backed by ``deepagents.create_deep_agent``."""

    backend_name = "deepagents"

    def __init__(
        self,
        *,
        model: Any,
        backends: dict[str, Any],
        skills: list[str],
        model_name: str | None,
        run_log_file: TextIO | None,
    ):
        self._model = model
        self._backends = backends
        self._skills = skills
        self._model_name = model_name
        self._run_log_file = run_log_file

    def invoke(
        self,
        *,
        kind: str,
        workspace: Path,  # noqa: ARG002 — backend already encapsulates cwd
        system_prompt: str,
        env: dict[str, str] | None = None,  # noqa: ARG002 — env on the BaseSandbox
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,  # noqa: ARG002 — cli-only injection point; deepagents uses tools=
        tools: list[BaseTool] | None = None,
    ) -> T:
        label = _agent_label(kind)

        # Fresh checkpointer + thread id per invocation, so the agent starts
        # with a clean context window each time. Mirrors what the simple loop
        # did at loop.py:410-411 before the runner abstraction landed.
        checkpointer = MemorySaver()
        thread_id = uuid.uuid4().hex

        backend = self._backends[kind]
        agent = create_deep_agent(
            model=self._model,
            backend=backend,
            system_prompt=system_prompt,
            skills=self._skills,
            response_format=AutoStrategy(response_cls),
            checkpointer=checkpointer,
            tools=tools,
        )
        log_agent_config(agent, label, self._run_log_file)

        callbacks: list[BaseCallbackHandler] = [
            AgentLogger(
                log_file=self._run_log_file,
                model_name=self._model_name,
                agent_label=label,
                progress=progress,
            )
        ]

        return run_typed_agent(
            agent,
            user_prompt,
            response_cls=response_cls,
            label=kind.upper(),
            fallback_factory=fallback_factory,
            callbacks=callbacks,
            thread_id=thread_id,
            round_label=round_label,
            log_file=self._run_log_file,
            max_text_len=DEFAULT_MAX_TEXT_LEN,
        )
