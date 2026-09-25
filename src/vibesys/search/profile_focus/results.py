"""Plain output carrier for :class:`~vibesys.search.profile_focus.focus.ProfileFocus`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.search.profile_focus.state import ProfileBottleneck

__all__ = ["FocusView"]


@dataclass(frozen=True, slots=True)
class FocusView:
    """Ephemeral prompt inputs derived from the authoritative focus state."""

    active_component: str = ""
    ledger_text: str = ""
    ranked_bottlenecks: tuple[ProfileBottleneck, ...] = ()

    def plan_prompt_context(self) -> dict[str, object]:
        """Return variables consumed by the orchestrator plan template."""
        return {
            "active_component": self.active_component,
            "ledger_text": self.ledger_text,
            "ranked_bottlenecks": [
                {
                    "component": item.name,
                    "cost_share": item.share * 100,
                    "evidence": item.evidence,
                }
                for item in self.ranked_bottlenecks
            ],
        }

    def implementer_prompt_context(self) -> dict[str, object]:
        """Return variables consumed by the implementer template."""
        return {"active_component": self.active_component}
