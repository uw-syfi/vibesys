"""The machine-readable line the stdio bridge writes to stderr when it ends unsuccessfully.

Kept apart from :mod:`server.stdio_bridge` so a relay that ends cleanly never pays for
importing Pydantic: the bridge runs once per connection and its startup is on the
connection's critical path. ``docs/contributing/wire-protocol.md`` (section "The stdio bridge")
documents the line.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict

from server.stdio_bridge import EXIT_STATUS, BridgeOutcome, BridgeResult


class BridgeReport(BaseModel):
    """One JSON object on one stderr line: how the relay ended and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: BridgeOutcome
    exit_status: int
    detail: str

    @classmethod
    def of(cls, result: BridgeResult) -> Self:
        """The report for ``result``."""
        return cls(
            outcome=result.outcome, exit_status=EXIT_STATUS[result.outcome], detail=result.detail
        )

    def line(self) -> str:
        """The report as one newline-terminated JSON line."""
        return self.model_dump_json() + "\n"
