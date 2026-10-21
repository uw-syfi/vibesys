"""In-memory executor with stable invocation identity and a pure operation implementation."""

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy

from vs_project.api import StoreFence
from vs_proto.api import Span, validate
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown


class FakeReplayExecutor:
    """Execute a supplied pure operation once per durable span, retaining its exact result.

    Bind one instance per run, like the production executor services. Changed
    payload under an invocation id fails instead of returning an unrelated
    cached answer. A lower fence can never start or observe an invocation.
    """

    def __init__(self, perform: Callable[[Span], ReplayResult | Awaitable[ReplayResult]]) -> None:
        self._perform = perform
        self._invocations: dict[str, tuple[bytes, asyncio.Task[ReplayResult]]] = {}
        self._epoch = 0

    @property
    def invocations(self) -> tuple[str, ...]:
        return tuple(self._invocations)

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        validate(span)
        if fence.epoch < self._epoch:
            problem = "stale replay executor fence"
            raise ValueError(problem)
        self._epoch = fence.epoch
        content = self._request(span)
        previous = self._invocations.get(span.id)
        if previous is not None:
            if previous[0] != content:
                problem = f"replay invocation payload conflict: {span.id}"
                raise ValueError(problem)
            return deepcopy(await previous[1])
        task = asyncio.create_task(self._result(deepcopy(span)))
        self._invocations[span.id] = (content, task)
        return deepcopy(await task)

    async def _result(self, span: Span) -> ReplayResult:
        performed = self._perform(span)
        result = performed if isinstance(performed, ReplayResult) else await performed
        validate(result.outcome)
        return deepcopy(result)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        """The run-scoped invocation journal survives a host's disposable runtime."""
        validate(span)
        if fence.epoch < self._epoch:
            problem = "stale replay executor fence"
            raise ValueError(problem)
        self._epoch = fence.epoch
        previous = self._invocations.get(span.id)
        if previous is None:
            return ReplayAbsent()
        content, task = previous
        if content != self._request(span):
            problem = f"replay invocation payload conflict: {span.id}"
            raise ValueError(problem)
        if not task.done():
            return ReplayPending()
        if task.cancelled():
            return ReplayUnknown("invocation was interrupted without an outcome")
        error = task.exception()
        if error is not None:
            return ReplayUnknown(f"{type(error).__name__}: {error}")
        return deepcopy(task.result())

    @staticmethod
    def _request(span: Span) -> bytes:
        if span.replay is None:
            problem = "replay executor requires immutable operation metadata"
            raise ValueError(problem)
        return span.replay.to_binary()
