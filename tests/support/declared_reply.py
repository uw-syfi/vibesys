"""An implementer reply whose ledger outcome is set directly, for tests of the ledger's rules.

A real implementer reports implementation status only (`ImplementerStatus`); whether the idea
held is derived by the framework from its measurement. The ledger's rules over a declared
outcome (disproven, inconclusive, supported) are still the rules the record and the transitions
apply to the outcomes they hold, so their tests need a reply that declares one. This is that
reply, and nothing in the product constructs it.
"""

from __future__ import annotations

from typing import override

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.multi.models import ImplementerResponse


class DeclaredImplementerResponse(ImplementerResponse):
    """An `ImplementerResponse` that declares ``declared`` as its ledger outcome when set."""

    declared: str | None = None

    @property
    @override
    def hypothesis_outcome(self) -> HypothesisOutcome:
        """The directly declared outcome, else the one the reported status stands for."""
        return HypothesisOutcome(self.declared) if self.declared else self.status.outcome
