"""Issue-tracker runner customization.

Wraps any :class:`~vibeserve_agent.agents.base.AgentRunner` and injects
tracker access for the ``judge`` and ``perf_eval`` phases. The wrapper
picks the right transport (MCP server spec for the cli backend, in-process
``@tool`` callables for the deepagents backend) by inspecting the inner
runner's ``backend_name``.

This module is the only place that knows BOTH:
  - the issue-tracker policy (creator/iteration/cap/types per phase)
  - the per-backend translation (``MCPServerSpec`` vs ``list[BaseTool]``)

The base AgentRunner implementations stay agnostic — they only see the
generic ``mcp_servers``/``tools`` injection-point kwargs.

Implementer phase: passes through unmodified. The relevant issue is
inlined into the implementer's system prompt by the loop, so no tracker
tools are needed there.
"""

from __future__ import annotations

from typing import Any, TypeVar

from langchain_core.tools import BaseTool
from libs.agent_cli.base import MCPServerSpec
from pydantic import BaseModel

from vibeserve_agent.agents.base import AgentRunner
from vibeserve_agent.loops.plain.mcp_config import build_issue_mcp_spec
from vibeserve_agent.loops.plain.issue_board import IssueBoard, IssueType
from vibeserve_agent.loops.plain.tools import build_issue_tools

T = TypeVar("T", bound=BaseModel)


# Per-phase policy. Frozen at module load — these don't change at runtime.
# The judge cap is hard-coded (always 1 bug per review); perf_eval's cap is
# instance-configured because it comes from a CLI flag.
_JUDGE_ALLOWED_TYPES: frozenset[IssueType] = frozenset({IssueType.BUG})
_PERF_EVAL_ALLOWED_TYPES: frozenset[IssueType] = frozenset(
    {IssueType.BUG, IssueType.FEATURE, IssueType.PERF}
)
_JUDGE_CAP: int = 1


class PlainLoopAgentRunner:
    """Wrap an AgentRunner and inject tracker access for judge/perf_eval.

    The wrapper preserves the AgentRunner Protocol surface for the kwargs
    the issue loop actually uses. The only addition is an explicit
    ``iteration`` kwarg on ``invoke()`` that the wrapper consumes (it
    determines the per-iteration cap scope) and does not forward.
    """

    def __init__(
        self,
        inner: AgentRunner,
        *,
        store: IssueBoard,
        max_issues_per_perf_eval: int,
    ):
        self._inner = inner
        self._store = store
        self._perf_eval_cap = max_issues_per_perf_eval

    @property
    def backend_name(self) -> str:
        return self._inner.backend_name

    def invoke(
        self,
        *,
        kind: str,
        iteration: int | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        **kwargs: Any,
    ) -> T:
        if kind in ("judge", "perf_eval"):
            if iteration is None:
                raise ValueError(
                    f"PlainLoopAgentRunner.invoke(kind={kind!r}) requires "
                    "iteration= so the cap can be scoped per-iteration"
                )
            if kind == "judge":
                tracker_kwargs = self._tracker_kwargs(
                    creator="judge",
                    iteration=iteration,
                    cap=_JUDGE_CAP,
                    allowed_types=_JUDGE_ALLOWED_TYPES,
                )
            else:  # perf_eval
                tracker_kwargs = self._tracker_kwargs(
                    creator="perf_eval",
                    iteration=iteration,
                    cap=self._perf_eval_cap,
                    allowed_types=_PERF_EVAL_ALLOWED_TYPES,
                )
            mcp_servers = tracker_kwargs.get("mcp_servers", mcp_servers)
            tools = tracker_kwargs.get("tools", tools)
        # implementer (and any other phase) passes through unmodified.
        return self._inner.invoke(
            kind=kind,
            mcp_servers=mcp_servers,
            tools=tools,
            **kwargs,
        )

    def _tracker_kwargs(
        self,
        *,
        creator: str,
        iteration: int,
        cap: int,
        allowed_types: frozenset[IssueType],
    ) -> dict[str, Any]:
        """Build the right injection-point kwarg for the inner backend.

        Returns ``{"mcp_servers": [...]}`` under the cli backend (the cli
        runner installs them as a stdio MCP server before ``generate()``)
        and ``{"tools": [...]}`` under deepagents (the runner forwards
        them straight to ``create_deep_agent(tools=...)``).

        Both factories share the policy semantics in
        :mod:`vibeserve_agent.loops.plain.tool_impl`, so cap and type-allowlist
        enforcement is byte-identical between backends.
        """
        if self._inner.backend_name == "cli":
            spec = build_issue_mcp_spec(
                store_relpath="issues.json",
                creator=creator,
                iteration=iteration,
                cap=cap,
                allowed_types=set(allowed_types),
            )
            return {"mcp_servers": [spec]}
        # deepagents (and any future in-process backend)
        issue_tools = build_issue_tools(
            self._store,
            iteration=iteration,
            can_create=True,
            creator=creator,
            create_cap=cap,
            allowed_create_types=set(allowed_types),
        )
        return {"tools": issue_tools}
