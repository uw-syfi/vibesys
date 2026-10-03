"""Plain output carriers for :class:`~vibesys.orchestration.profile_focus.focus.ProfileFocus`."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from vibesys.orchestration.profile_focus.state import (
    ProfileBottleneck,
    ProfileGuidanceStatus,
)

__all__ = ["FocusLedger", "FocusLedgerRow", "FocusView"]


class FocusLedgerRow(BaseModel):
    """One component's focus bookkeeping, as the plan prompt shows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: ProfileGuidanceStatus
    rounds_spent: int
    latest_share: float | None
    stalled_rounds: int


class FocusLedger(BaseModel):
    """Every tracked component in state order; empty before the first profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[FocusLedgerRow, ...] = ()


@dataclass(frozen=True, slots=True)
class FocusView:
    """Ephemeral prompt inputs derived from the authoritative focus state."""

    active_component: str = ""
    ledger: FocusLedger = field(default_factory=FocusLedger)
    ranked_bottlenecks: tuple[ProfileBottleneck, ...] = ()

    def implementer_prompt_context(self) -> dict[str, object]:
        """Return variables consumed by the implementer template."""
        return {"active_component": self.active_component}
