"""Which readings the strategy accepts back.

The strategy embeds the ledger's own `EvidenceRef` values (chosen with
`vs_core.api.trusted_keys` and `ledger_refs`) in an `InterpretEvidence` request and accepts
back only readings for exactly the records it asked about (`vibesys.metrics.accept_readings`).
"""

from vibesys.metrics import AcceptedReading
from vibesys.metrics import accept_readings as _accept_readings
from vibesys.orchestration.dynamic.strategy._operations import EvidenceReadings
from vs_core.api import EvidenceRef


def accept_readings(
    outcome: EvidenceReadings, requested: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...] | str:
    """Bind each reading of the operation's outcome to the one requested record it decodes."""
    return _accept_readings(outcome.readings, requested)
