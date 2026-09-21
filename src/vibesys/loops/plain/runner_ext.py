"""Issue-tracker runner customization.

Wraps any :class:`~vs_agent.contracts.AgentClientProtocol` and injects
tracker access for the ``judge`` and ``perf_eval`` phases. The wrapper
requires an MCP-capable inner client and hands it an issue-board MCP server
spec.

This module is the only place that knows the issue-tracker policy
(creator/iteration/cap/types per phase). The base AgentClient implementations
stay agnostic: they only see the generic ``mcp_servers`` injection-point kwarg.

Implementer phase: passes through unmodified. The relevant issue is
inlined into the implementer's system prompt by the loop, so no tracker
tools are needed there.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any, TextIO, TypeVar

from pydantic import BaseModel

from vibesys.loops.plain.mcp_config import build_issue_mcp_spec
from vs_agent.client import AgentClient
from vs_agent.contracts import (  # noqa: TC001
    AgentCapabilities,
    AgentClientProtocol,
    MCPServerSpec,
)
from vs_agent.progress import AgentProgress  # noqa: TC001
from vs_agent.session_key import AgentSessionKey  # noqa: TC001
from vs_issue_board import IssueType

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
        max_issues_per_perf_eval: int,
    ):
        self._inner = inner
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
        **kwargs: Any,  # noqa: ANN401  # tracked: #288
    ) -> T:
        if kind in ("judge", "perf_eval"):
            if iteration is None:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    f"PlainLoopAgentClient.invoke(kind={kind!r}) requires "
                    "iteration= so the cap can be scoped per-iteration"
                )
            if kind == "judge":
                mcp_servers = [
                    self._issue_mcp_spec(
                        creator="judge",
                        iteration=iteration,
                        cap=_JUDGE_CAP,
                        allowed_types=_JUDGE_ALLOWED_TYPES,
                    )
                ]
            else:  # perf_eval
                mcp_servers = [
                    self._issue_mcp_spec(
                        creator="perf_eval",
                        iteration=iteration,
                        cap=self._perf_eval_cap,
                        allowed_types=_PERF_EVAL_ALLOWED_TYPES,
                    )
                ]
        # implementer (and any other phase) passes through unmodified.
        return self._inner.invoke(
            kind=kind,
            response_cls=response_cls,
            mcp_servers=mcp_servers,
            **kwargs,
        )

    def _issue_mcp_spec(
        self,
        *,
        creator: str,
        iteration: int,
        cap: int,
        allowed_types: frozenset[IssueType],
    ) -> MCPServerSpec:
        """Build the issue-board MCP server spec for one judge/perf_eval turn.

        Cap and type-allowlist enforcement live in :mod:`vs_issue_board.policy`.
        """
        if not self._inner.capabilities.mcp_servers:
            raise RuntimeError(  # noqa: TRY003
                f"agent backend {self._inner.backend_name!r} cannot expose issue-board tools"
            )
        return build_issue_mcp_spec(
            store_relpath="issues.json",
            creator=creator,
            iteration=iteration,
            cap=cap,
            allowed_types=set(allowed_types),
        )
