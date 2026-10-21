"""In-memory executor with stable invocation identity and a pure operation implementation."""

from collections.abc import Callable
from copy import deepcopy

from vs_project.api import StoreFence
from vs_proto.api import Span, validate
from vs_runtime._replay_runtime import ReplayResult


class FakeReplayExecutor:
    """Execute a supplied pure operation once per durable span, retaining its exact result.

    Bind one instance per run, like the production executor services. Changed
    payload under an invocation id fails instead of returning an unrelated
    cached answer. A lower fence can never start or observe an invocation.
    """

    def __init__(self, perform: Callable[[Span], ReplayResult]) -> None:
        self._perform = perform
        self._invocations: dict[str, tuple[bytes, ReplayResult]] = {}
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
        content = span.to_binary()
        previous = self._invocations.get(span.id)
        if previous is not None:
            if previous[0] != content:
                problem = f"replay invocation payload conflict: {span.id}"
                raise ValueError(problem)
            return deepcopy(previous[1])
        result = self._perform(deepcopy(span))
        validate(result.outcome)
        self._invocations[span.id] = (content, deepcopy(result))
        return deepcopy(result)
