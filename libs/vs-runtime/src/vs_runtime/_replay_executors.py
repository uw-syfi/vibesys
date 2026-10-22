"""Closed typed routing for a run's native replay owners."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from vs_proto.api import validate
from vs_runtime._replay_operations import ReplayOperationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from protobuf import Message

    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_runtime._replay_runtime import (
        ReplayAbsent,
        ReplayExecutor,
        ReplayPending,
        ReplayResult,
        ReplayUnknown,
    )


class ReplayExecutors:
    """Select exactly one owning executor for each declared native request class.

    Composition supplies the closed map once. Owners validate their exact request
    and reply contracts and retain their own resource/recovery authority. An
    unknown type cannot fall through to another owner or a positional driver.
    """

    def __init__(self, owners: Mapping[type[Message], ReplayExecutor]) -> None:
        routes: dict[str, ReplayExecutor] = {}
        for request, owner in owners.items():
            name = request.desc().type_name
            if name in routes:
                problem = f"duplicate replay owner for native request: {name}"
                raise ReplayOperationError(problem)
            routes[name] = owner
        self._owners = MappingProxyType(routes)

    def _owner(self, span: Span) -> ReplayExecutor:
        validate(span)
        if span.replay is None or span.replay.policy is None or span.replay.policy.request is None:
            problem = "replay routing requires an exact native request"
            raise ReplayOperationError(problem)
        name = span.replay.policy.request.type_url
        owner = self._owners.get(name)
        if owner is None:
            problem = f"unbound replay native request: {name}"
            raise ReplayOperationError(problem)
        return owner

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        return await self._owner(span).execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        return await self._owner(span).inspect(span, fence)
