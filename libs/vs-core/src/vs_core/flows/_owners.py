"""``ctx.owners``: ask another library's owner to perform an operation the search registered.

Some of what a search needs is not core's: an issue board is read and changed by the library
that owns it. The search names such operations once, in ``@search(..., owned=...)``; core
registers their codec, the host must provide an owner for each before the run starts, and a flow
asks one with ``await ctx.owners.perform(request, within=s)``. The request is an
``OperationRequest`` of a registered kind; the result is the typed outcome the owner reported, or
the ``Failed`` that ended the wait. Whether an unacknowledged write landed is the search's to
settle (a keyed write may be sent again), so a ``Failed`` is a value to match on, not an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core._registry import ContractError
from vs_core.types.common import THIS_RUN, DecisionId
from vs_core.types.search import OperationFinished
from vs_core.types.strategy import Operation, OperationRequest
from vs_core.types.until import Failed, Within

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.types.common import Seconds

    from ._kit import Kit
    from ._labels import Label


class Owners:
    """The owner-operation aspect of ``ctx``."""

    def __init__(self, kit: Kit) -> None:
        """Bind the aspect to its flow."""
        self._kit = kit

    async def perform(
        self, request: OperationRequest, *, within: Seconds | Within, label: Label | None = None
    ) -> BaseModel | Failed:
        """Have the owner perform ``request``: its typed outcome, or the failure that ended the wait.

        ``request`` must be of a kind the search registered with ``owned=``; ``label`` labels the
        operation's span.
        """
        owned = {item.descriptor.kind for item in self._kit.run.wiring.owned}
        if request.kind not in owned:
            message = f"{request.kind!r} is not an operation this search owns: list it in owned="
            raise ContractError(("owners", "perform"), message)
        decision = Operation(decision_id=DecisionId(root="owned"), scope=THIS_RUN, request=request)
        bound = within if isinstance(within, Within) else Within(limit=within)
        match await self._kit.wait(decision, bound, label):
            case OperationFinished(result=finished):
                if finished.outcome is None:
                    message = f"the owner of {request.kind!r} finished without an outcome"
                    raise ContractError(("owners", "perform"), message)
                return finished.outcome
            case Failed() as failed:
                return failed
            case unexpected:
                message = f"an operation ended with {unexpected.kind}"
                raise ContractError(("owners", "perform"), message)
