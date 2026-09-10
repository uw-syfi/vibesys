"""Issue-tracker runner customization.

Wraps any :class:`~vibesys.agents.contracts.AgentClientProtocol` and injects
tracker access for the ``judge`` and ``perf_eval`` phases. The wrapper
picks the right transport (MCP server spec or in-process ``@tool`` callables)
from the inner client's declared capabilities.

This module is the only place that knows BOTH:
  - the issue-tracker policy (creator/iteration/cap/types per phase)
  - the per-backend translation (``MCPServerSpec`` vs ``list[BaseTool]``)

The base AgentClient implementations stay agnostic — they only see the
generic ``mcp_servers``/``tools`` injection-point kwargs.

Implementer phase: passes through unmodified. The relevant issue is
inlined into the implementer's system prompt by the loop, so no tracker
tools are needed there.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any, TextIO, TypeVar

from langchain_core.tools import BaseTool  # noqa: TC002  # tracked: #288
from pydantic import BaseModel

from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import (  # noqa: TC001
    AgentCapabilities,
    AgentClientProtocol,
    MCPServerSpec,
)
from vibesys.agents.progress import AgentProgress  # noqa: TC001
from vibesys.agents.session_key import AgentSessionKey  # noqa: TC001
from vibesys.loops.plain.mcp_config import build_issue_mcp_spec
from vibesys.loops.plain.tools import build_issue_tools
from vs_issue_board import IssueBoard, IssueType

T = TypeVar("T", bound=BaseModel)


# Per-phase policy. Frozen at module load — these don't change at runtime.
# The judge cap is hard-coded (always 1 bug per review); perf_eval's cap is
# instance-configured because it comes from a CLI flag.
_JUDGE_ALLOWED_TYPES: frozenset[IssueType] = frozenset({IssueType.BUG})
_PERF_EVAL_ALLOWED_TYPES: frozenset[IssueType] = frozenset(
    {IssueType.BUG, IssueType.FEATURE, IssueType.PERF}
)
_JUDGE_CAP: int = 1


class PlainLoopAgentClient(AgentClient):
    """Wrap an AgentClient and inject tracker access for judge/perf_eval.

    The wrapper preserves the AgentClient surface for the kwargs
    the issue loop actually uses. The only addition is an explicit
    ``iteration`` kwarg on ``invoke()`` that the wrapper consumes (it
    determines the per-iteration cap scope) and does not forward.
    """

    def __init__(  # noqa: ANN204, D107  # tracked: #288
        self,
        inner: AgentClientProtocol,
        *,
        store: IssueBoard,
        max_issues_per_perf_eval: int,
    ):
        self._inner = inner
        self._store = store
        self._perf_eval_cap = max_issues_per_perf_eval

    @property
    def backend_name(self) -> str:  # noqa: D102  # tracked: #288
        return self._inner.backend_name

    @property
    def capabilities(self) -> AgentCapabilities:
        """Preserve the inner client's declared capabilities."""
        return self._inner.capabilities

    @property
    def driver_name(self) -> str | None:
        """Attribute turns to the wrapped client, not to the wrapper."""
        return self._inner.driver_name

    @property
    def provider(self) -> str | None:
        """Attribute turns to the wrapped client, not to the wrapper."""
        return self._inner.provider

    def model_for_kind(self, kind: str) -> str | None:
        """Report the wrapped client's model; the wrapper selects none."""
        return self._inner.model_for_kind(kind)

    def set_log_file(self, stream: TextIO | None) -> None:
        """Retarget inner-client logs when the run changes log files."""
        self._inner.set_log_file(stream)

    def close(self) -> None:
        """Close the inner client."""
        self._inner.close()

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Report the inner client's conversation for ``session_key``."""
        return self._inner.provider_session_id(session_key)

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Report where the inner client's last turn on ``session_key`` ran."""
        return self._inner.last_turn_provider_session_id(session_key)

    def invoke_text(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Delegate an unstructured turn without changing tracker policy."""
        return self._inner.invoke_text(
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            round_label=round_label,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            tools=tools,
            reuse_session=reuse_session,
            session_key=session_key,
        )

    def invoke(  # noqa: D102  # tracked: #288
        self,
        *,
        kind: str,
        response_cls: type[T],
        iteration: int | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        **kwargs: Any,  # noqa: ANN401  # tracked: #288
    ) -> T:
        if kind in ("judge", "perf_eval"):
            if iteration is None:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    f"PlainLoopAgentClient.invoke(kind={kind!r}) requires "
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
            response_cls=response_cls,
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

        Returns ``{"mcp_servers": [...]}`` when the runtime supports MCP and
        ``{"tools": [...]}`` when it supports in-process tools.

        Both factories share the policy semantics in
        :mod:`vs_issue_board.policy`, so cap and type-allowlist
        enforcement is byte-identical between backends.
        """
        if self._inner.capabilities.mcp_servers:
            spec = build_issue_mcp_spec(
                store_relpath="issues.json",
                creator=creator,
                iteration=iteration,
                cap=cap,
                allowed_types=set(allowed_types),
            )
            return {"mcp_servers": [spec]}
        if not self._inner.capabilities.in_process_tools:
            raise RuntimeError(  # noqa: TRY003
                f"agent backend {self._inner.backend_name!r} cannot expose issue-board tools"
            )
        # Deepagents and other explicitly in-process implementations.
        issue_tools = build_issue_tools(
            self._store,
            iteration=iteration,
            can_create=True,
            creator=creator,
            create_cap=cap,
            allowed_create_types=set(allowed_types),
        )
        return {"tools": issue_tools}
