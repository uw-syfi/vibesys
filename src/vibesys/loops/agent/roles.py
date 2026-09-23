"""Named agent handles for the built-in policy's shared run context.

The built-in loop intentionally shares one client, sandbox, workspace, and
session store. These bindings name its participants without provisioning a
second set of resources through the custom-policy runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.protocol import LoopContext
    from vs_agent.api import AgentSessionKey, MCPServerSpec

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class SharedAgentHandle:
    """One named role backed by the current context's existing agent client."""

    id: str
    context: LoopContext

    def turn_structured(  # noqa: PLR0913
        self,
        message: str,
        *,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        system_prompt: str = "",
        label: str = "",
        session_key: AgentSessionKey | None = None,
        reuse_session: bool | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
    ) -> T:
        """Use the context's control, progress, attribution, and session policy."""
        return self.context.invoke(
            kind=self.id,
            system_prompt=system_prompt,
            user_prompt=message,
            response_cls=response_cls,
            fallback_factory=fallback_factory,
            round_label=label,
            session_key=session_key,
            reuse_session=reuse_session,
            mcp_servers=mcp_servers,
        )


@dataclass(frozen=True, slots=True)
class BuiltInAgentRoles:
    """Participants in the built-in policy; the policy decides turn order."""

    orchestrator: SharedAgentHandle
    implementer: SharedAgentHandle
    judge: SharedAgentHandle
    profiler: SharedAgentHandle

    @classmethod
    def bind(cls, context: LoopContext) -> BuiltInAgentRoles:
        """Declare all roles against the run's already-provisioned client."""
        return cls(
            orchestrator=SharedAgentHandle("orchestrator", context),
            implementer=SharedAgentHandle("implementer", context),
            judge=SharedAgentHandle("judge", context),
            profiler=SharedAgentHandle("profiler", context),
        )


def _invoke_read_only_role(
    ctx: LoopContext,
    *,
    role: str,
    checkpoint_label: str,
    allowed_workspace_paths: tuple[str, ...] = (),
    agent: SharedAgentHandle | None = None,
    **invoke_kwargs: Any,  # noqa: ANN401  # tracked: #288
) -> Any:  # noqa: ANN401  # tracked: #288
    """Invoke an evidence-reading role and undo unauthorized mutations.

    Prompt-level role boundaries are useful guidance, but they are not an
    enforcement mechanism. Commit the framework's current state before the
    turn, then restore that exact tree if the agent writes tracked or untracked
    files outside its narrow allowlist. The structured response remains usable
    after restoration. Allowlisted text files are preserved across a full-tree
    restore so one permitted write cannot smuggle unrelated candidate edits.
    """
    ctx.snapshot_workspace(checkpoint_label)
    checkpoint = ctx.git.current_sha()
    if checkpoint is None:
        raise RuntimeError(f"Cannot isolate {role}: workspace checkpoint is unavailable")  # noqa: TRY003  # tracked: #288

    try:
        if agent is None:
            return ctx.invoke(**invoke_kwargs)
        kind = invoke_kwargs.pop("kind")
        if kind != agent.id or kind != role:
            raise ValueError(  # noqa: TRY003
                f"{role} turn was bound to {agent.id!r}, requested {kind!r}"
            )
        message = invoke_kwargs.pop("user_prompt")
        system_prompt = invoke_kwargs.pop("system_prompt")
        response_cls = invoke_kwargs.pop("response_cls")
        fallback_factory = invoke_kwargs.pop("fallback_factory")
        label = invoke_kwargs.pop("round_label", "")
        mcp_servers = invoke_kwargs.pop("mcp_servers", None)
        reuse_session = invoke_kwargs.pop("reuse_session", None)
        session_key = invoke_kwargs.pop("session_key", None)
        if invoke_kwargs:
            raise TypeError(  # noqa: TRY003
                f"unsupported {role} turn options: {', '.join(invoke_kwargs)}"
            )
        return agent.turn_structured(
            message,
            system_prompt=system_prompt,
            response_cls=response_cls,
            fallback_factory=fallback_factory,
            label=label,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
    finally:

        def is_allowed(path: str) -> bool:
            return any(
                path == allowed.rstrip("/") or path.startswith(f"{allowed.rstrip('/')}/")
                for allowed in allowed_workspace_paths
            )

        changes = ctx.git.pending_changes()
        unauthorized = [path for path in changes if not is_allowed(path)]
        if unauthorized:
            checkout_kwargs: dict[str, Any] = {"clean": True}
            if allowed_workspace_paths:
                checkout_kwargs["preserve_paths"] = allowed_workspace_paths
            if not ctx.git.checkout_tree(checkpoint, **checkout_kwargs):
                raise RuntimeError(  # noqa: TRY003  # tracked: #288
                    f"Cannot isolate {role}: failed to restore workspace checkpoint "
                    f"{checkpoint[:12]}"
                )
            remaining = [path for path in ctx.git.pending_changes() if not is_allowed(path)]
            if remaining:
                raise RuntimeError(  # noqa: TRY003  # tracked: #288
                    f"Cannot isolate {role}: workspace is still modified after restore: "
                    f"{', '.join(remaining[:8])}"
                )
            shown = ", ".join(unauthorized[:8])
            suffix = "" if len(unauthorized) <= 8 else f", ... (+{len(unauthorized) - 8} more)"  # noqa: PLR2004  # tracked: #288
            ctx.lprint(
                f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
                f"attempted by {role}: {shown}{suffix}"
            )
