"""One crash-safe receipt namespace shared by every request executor.

A receipt is one atomically written file keyed by (family, part, key) below a
Project state namespace. ``record_once`` is the idempotence rule every executor
relies on: recording the identical receipt again is a no-op, and recording a
different one under the same key raises ``ContractError`` naming the conflict.
``replace`` is for the few records that legitimately evolve, such as a counter.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vs_core.api import ContractError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_project.api import StateNamespace


class ReceiptStore:
    """Atomic receipt files below one state namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind to the namespace that holds this run's receipts."""
        self._namespace = namespace

    @staticmethod
    def _path(family: str, part: str, key: str) -> str:
        return f"{family}/{hashlib.sha256(key.encode()).hexdigest()}.{part}.json"

    def load[ReceiptT: BaseModel](
        self, family: str, part: str, key: str, model: type[ReceiptT]
    ) -> ReceiptT | None:
        """The recorded receipt, or None when nothing was recorded."""
        return self._namespace.load_optional(self._path(family, part, key), model)

    def record_once(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        """Durably record the receipt; identical replays pass, a different payload conflicts."""
        prior = self.load(family, part, key, type(receipt))
        if prior == receipt:
            return
        if prior is not None:
            raise ContractError(("request_id",), f"same {part} identity with another payload")
        self._namespace.save(self._path(family, part, key), receipt)

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        """Durably overwrite a record that is defined to change over time."""
        self._namespace.save(self._path(family, part, key), receipt)
