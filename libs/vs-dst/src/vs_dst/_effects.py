"""The effects ledger: what the executors' world did, kept across restarts of the host.

A host that dies loses its memory; the effects it caused do not go away. `RecoverableExecutors`
seal what each request's effect did, so a restarted host that clears a step finds its result
instead of running the effect again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_core.api import ContractError
from vs_runtime.api.testing import ScriptedExecutors

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_core.api.testing import Journal
    from vs_runtime.api.core import ExecutionContext, ExecutionResult
    from vs_runtime.api.testing import Script


@dataclass
class Effects:
    """What the executors' world did, kept across restarts of the host."""

    sealed: dict[str, ExecutionResult] = field(default_factory=dict)
    """The result each request's effect left behind: what a restarted host clears a step to."""
    executed: list[str] = field(default_factory=list)
    """Requests whose effect began, in order; a request in it twice ran twice. One with no
    sealed result was interrupted: its effect began and left nothing a restart can read."""
    finished: dict[str, int] = field(default_factory=dict)
    """How many times each request's effect finished and sealed a result."""


class RecoverableExecutors(ScriptedExecutors):
    """Scripted executors that seal what each request's effect did, and tell the host when one begins."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994708 [PLR0913]; the base class takes the script's five collaborators and this one adds the ledger it records into.
        # > Bundling them would change the `ScriptedExecutors` constructor every subclass shares.
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
        effects: Effects,
    ) -> None:
        """Bind the script and the shared record of effects."""
        super().__init__(script, core, registry, schemas, journal)
        self._effects = effects

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Perform the request once and seal its result."""
        if request.request_id is None:
            raise ContractError(("request_id",), "a dispatched request carries its identity")
        identity = request.request_id.root
        self.begin(identity)
        await self._running(request)
        result = await self._enacted(request, await super().execute(request, context))
        self._effects.sealed[identity] = result
        self._effects.finished[identity] = self._effects.finished.get(identity, 0) + 1
        return result

    def begin(self, identity: str) -> None:
        """The effect of the request ``identity`` has begun, and nothing of it is sealed yet.

        A request the far side holds begins too: it is in flight until the loop cancels it.
        """
        self._effects.executed.append(identity)
        self._began(len(self._effects.executed) - 1)

    def _began(self, index: int) -> None:
        """The ``index``-th effect of the run has begun, and nothing of it is sealed yet."""
        del index

    async def _running(self, request: Request) -> None:
        """What the host's own side does while the effect of ``request`` is in flight."""
        del request

    async def _enacted(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        """What the shell is told once the effect's side effects on the host have played out."""
        del request
        return result
