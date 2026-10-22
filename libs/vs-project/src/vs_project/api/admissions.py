"""Durable admission ownership, independent of project staging and run lifecycle.

A scoped claim excludes the request identity and every normalized target. Intent
must be durable before an effect; an existing intent requires reconciliation,
never another dispatch. Terminal receipts are retained without expiration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from vs_proto.api import admission


class AdmissionConflictError(ValueError):
    """An identity was reused with different target or accepted input bytes."""


class AdmissionBusyError(RuntimeError):
    """Another scoped claim owns this identity or a target; no effect was attempted."""


class AdmissionUnconfirmedError(RuntimeError):
    """Existing durable intent must be reconciled before any further effect."""


class AdmissionClaim(Protocol):
    """Only the lock-owning scope may publish intent or settle its receipt."""

    def observe(self) -> admission.AdmissionReceipt:
        """Return an independent validated receipt copy."""
        ...

    def record_intent(self, evidence: admission.PublicationEvidence) -> None:
        """Persist evidence before dispatch; reject every repeated dispatch intent."""
        ...

    def settle(self, status: admission.AdmissionStatus, result: admission.OperationResult) -> None:
        """Durably retain the typed original result, allowing identical replay only."""
        ...


class AdmissionStore(Protocol):
    """One host user's durable receipts and target exclusion owner."""

    def claim(
        self,
        identity: admission.AdmissionIdentity,
        targets: tuple[str, ...],
        accepted_request: admission.AcceptedOperation,
    ) -> AbstractContextManager[AdmissionClaim]:
        """Own identity then sorted targets; changed bytes or targets conflict."""
        ...

    def lookup(self, identity: admission.AdmissionIdentity) -> admission.AdmissionReceipt | None:
        """Read a bounded validated receipt; corruption is an error, never absence."""
        ...
