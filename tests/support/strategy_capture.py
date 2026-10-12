"""The (state, view) pairs a strategy decided on or proposed during one simulated run.

A test that needs a real state the strategy reaches (one with a settlement outstanding, say)
simulates the world once and reads it from the capture, rather than building the state by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from vs_dst.api import simulate

if TYPE_CHECKING:
    from vs_core.api import Proposal, RunView, Strategy, StrategyEvent
    from vs_dst.api import World


@dataclass
class Capture:
    """The pairs one run decided on or proposed, and a strategy over any of their states."""

    new_strategy: Any
    pairs: list[tuple[Any, RunView]] = field(default_factory=list)

    def over(self, state: Any) -> Any:  # noqa: ANN401  # lint-waiver: LW-731206 [ANN401]; the state type is each strategy's own, and the capture is shared by all of them.
        """A fresh strategy bound to ``state``."""
        return self.new_strategy().bind(state)


class CapturingStrategy:
    """A strategy that records what it is asked to decide on."""

    def __init__(self, inner: Strategy[Any], capture: Capture) -> None:
        self._inner = inner
        self._capture = capture
        self.declaration = inner.declaration

    @property
    def state(self) -> Any:  # noqa: ANN401  # lint-waiver: LW-731207 [ANN401]; see `Capture.over`.
        """The wrapped strategy's state."""
        return self._inner.state

    def bind(self, state: Any) -> CapturingStrategy:  # noqa: ANN401  # lint-waiver: LW-731208 [ANN401]; see `Capture.over`.
        """The same policy over a different state."""
        return CapturingStrategy(self._inner.bind(state), self._capture)

    def on_event(self, view: RunView, event: StrategyEvent) -> Any:  # noqa: ANN401  # lint-waiver: LW-731209 [ANN401]; see `Capture.over`.
        """Fold the event as the wrapped strategy does."""
        return self._inner.on_event(view, event)

    def decide(self, view: RunView) -> Proposal[Any]:
        """Record the pair decided on and the state proposed, then return the proposal."""
        self._capture.pairs.append((self._inner.state, view))
        proposal = self._inner.decide(view)
        self._capture.pairs.append((proposal.state, view))
        return proposal


def captured(world: Any) -> Capture:  # noqa: ANN401  # lint-waiver: LW-731210 [ANN401]; each strategy has its own world class and `World` does not expose the setup.
    """Simulate ``world`` to its end and return every pair its strategy decided on."""
    inner = world.setup.new_strategy
    capture = Capture(new_strategy=inner)
    world.setup = replace(
        world.setup, new_strategy=lambda: CapturingStrategy(inner(), capture)
    )
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return capture


__all__ = ["Capture", "CapturingStrategy", "captured"]
