"""How full a conversation's context window is, and when that retires the conversation.

A conversation carries every earlier message and tool result, so a long one grows toward
its model's context window; past it the provider refuses the turn. VibeSys retires a
conversation whose last turn reached ``CONTEXT_RENEWAL_FRACTION`` of the window before a
turn that may run without it (``AgentTurnRequest.renewable``).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

CONTEXT_RENEWAL_FRACTION = Fraction(4, 5)
"""The share of the model's context window at which a conversation is retired.

The headroom is for the next turn's own message and tool results: one benchmark log can add
tens of thousands of tokens, so a conversation at this share may not fit another turn.
"""


@dataclass(frozen=True, slots=True)
class ContextUse:
    """The tokens a conversation's context held after its last turn, and its model's window."""

    tokens: int
    """The context the next turn starts from, as the provider reported it."""
    window: int | None
    """The model's context window in tokens; ``None`` when unknown."""

    def near_limit(self, threshold: Fraction = CONTEXT_RENEWAL_FRACTION) -> bool:
        """Whether the context reached ``threshold`` of the window; never for an unknown window.

        Compared exactly, so a reading at the boundary token counts as reached.
        """
        if self.window is None or self.window <= 0:
            return False
        return Fraction(self.tokens, self.window) >= threshold
