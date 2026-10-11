"""The judge's review evidence, built from decoded readings (`vibesys.metrics`)."""

from vibesys.metrics import AcceptedReading
from vs_core.api import EvidenceKey, RevisionRef, Value


class ReviewEvidence(Value):
    """The readings of the trusted evaluations a judge was shown for one candidate.

    ``keys`` names the evidence the strategy asked core's evidence owner to decode, so
    the readings count as current only while the ledger still holds exactly that set
    for exactly that candidate. An empty ``readings`` after a non-empty ``keys`` means
    the owner could not decode them; the prompt then lists the evidence without numbers.
    """

    candidate: RevisionRef
    keys: tuple[EvidenceKey, ...]
    readings: tuple[AcceptedReading, ...] = ()
