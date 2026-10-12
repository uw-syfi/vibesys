"""The (state, view) pairs a strategy decided on or proposed during one simulated run.

A test that needs a real state the strategy reaches (one with a settlement outstanding, say)
simulates the world once and reads it from the capture, rather than building the state by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from vs_dst.api import World, simulate

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import Proposal, RunView, Strategy, StrategyDeclaration, StrategyEvent
    from vs_core.types.strategy import StrategyState
    from vs_dst.api import RunSetup


class Hosted(World, Protocol):
    """A vs-dst world whose strategy can be swapped for a recording one."""

    setup: RunSetup


@dataclass
class Capture[S: StrategyState]:
    """The pairs one run decided on or proposed, and a strategy over any of their states."""

    new_strategy: Callable[[], Strategy[S]]
    pairs: list[tuple[S, RunView]] = field(default_factory=list)

    def over(self, state: S) -> Strategy[S]:
        """A fresh strategy bound to ``state``."""
        return self.new_strategy().bind(state)


class CapturingStrategy[S: StrategyState]:
    """A strategy that records what it is asked to decide on and what it proposes."""

    def __init__(self, inner: Strategy[S], capture: Capture[S]) -> None:
        self._inner = inner
        self._capture = capture

    @property
    def state(self) -> S:
        """The wrapped strategy's state."""
        return self._inner.state

    @property
    def declaration(self) -> StrategyDeclaration:
        """The wrapped strategy's declaration."""
        return self._inner.declaration

    def bind(self, state: S) -> CapturingStrategy[S]:
        """The same policy over a different state."""
        return CapturingStrategy(self._inner.bind(state), self._capture)

    def on_event(self, view: RunView, event: StrategyEvent) -> S:
        """Fold the event as the wrapped strategy does."""
        return self._inner.on_event(view, event)

    def decide(self, view: RunView) -> Proposal[S]:
        """Record the pair decided on and the state proposed, then return the proposal."""
        self._capture.pairs.append((self._inner.state, view))
        proposal = self._inner.decide(view)
        self._capture.pairs.append((proposal.state, view))
        return proposal


def captured[S: StrategyState](world: Hosted, state_type: type[S]) -> Capture[S]:
    """Simulate ``world`` to its end; every pair its strategy (with ``state_type`` states) saw."""
    inner = world.setup.new_strategy
    capture: Capture[S] = Capture(new_strategy=inner)
    world.setup = replace(world.setup, new_strategy=lambda: CapturingStrategy(inner(), capture))
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    assert all(isinstance(state, state_type) for state, _ in capture.pairs)
    return capture


__all__ = ["Capture", "CapturingStrategy", "Hosted", "captured"]
