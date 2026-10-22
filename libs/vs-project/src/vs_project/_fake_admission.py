"""Faithful in-memory admission store using the production transition rules."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from vs_project._admission import _Claim, normalized_targets
from vs_project._admission_rules import checked_receipt, compare, initial_receipt
from vs_project.api.admissions import AdmissionBusyError
from vs_proto.api import admission, validate

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_project.api.admissions import AdmissionClaim


class FakeAdmissionStore:
    """Retained receipt bytes and exclusion, with no filesystem or real-time work."""

    def __init__(self) -> None:
        """Create one host-user store; consumers share it across claims/reopens."""
        self._receipts: dict[tuple[int, str], bytes] = {}
        self._owned: set[tuple[str, ...]] = set()

    def lookup(self, identity: admission.AdmissionIdentity) -> admission.AdmissionReceipt | None:
        """Read through the same decoder and semantic checks as file persistence."""
        validate(identity)
        payload = self._receipts.get((identity.operation_id, identity.request_id))
        if payload is None:
            return None
        receipt = checked_receipt(payload)
        if receipt.identity != identity:
            message = "admission receipt identity mismatch"
            raise ValueError(message)
        return receipt

    @contextmanager
    def claim(
        self,
        identity: admission.AdmissionIdentity,
        targets: tuple[str, ...],
        accepted_request: admission.AcceptedOperation,
    ) -> Iterator[AdmissionClaim]:
        """Acquire identity then sorted targets, preserving exact conflict semantics."""
        targets = normalized_targets(targets)
        requested = initial_receipt(identity, targets, accepted_request)
        keys = (
            ("identity", str(identity.operation_id), identity.request_id),
            *(("target", target) for target in targets),
        )
        if self._owned.intersection(keys):
            message = "admission is owned"
            raise AdmissionBusyError(message)
        self._owned.update(keys)
        try:
            key = (identity.operation_id, identity.request_id)
            receipt = self.lookup(identity)
            if receipt is None:
                receipt = requested
                self._receipts[key] = receipt.to_binary()
            else:
                compare(receipt, requested)
            claim = _Claim(receipt, lambda payload: self._receipts.__setitem__(key, payload))
            try:
                yield claim
            finally:
                claim.active = False
        finally:
            self._owned.difference_update(keys)
