"""Read operator guidance without replaying pause, resume or other control intents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from vs_proto.api import imported_facts as pb
from vs_runtime.api.infrastructure import ControlRequest, SteerRequest, WithdrawRequest

from ._source import decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._facts import FactSet
    from ._source import ImportSource


def import_control(source: ImportSource, registry: OperationRegistry, facts: FactSet) -> None:
    request = decode(source, TypeAdapter(ControlRequest), registry)
    if isinstance(request, SteerRequest):
        facts.add(source, "$", pb.ImportedGuidance(identity=request.control_id, text=request.text))
    elif isinstance(request, WithdrawRequest):
        facts.add(
            source,
            "$",
            pb.ImportedGuidance(identity=request.withdraws, withdrawn=True, disposed=True),
        )
