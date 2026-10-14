"""What the whole-loop harness needs to know about a strategy, keyed by plugin id.

The scenario harness drives a strategy through the production host: it builds the CLI request,
decodes the committed envelope and checks liveness. Everything in it that names a concrete
strategy lives here, so a ported strategy registers one `LoopSpec` and reuses the harness, the
loop invariants and the chaos driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.search.api import OPERATIONS
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vs_core.api import FlowState, RunEnvelope

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState


@dataclass(frozen=True)
class LoopSpec:
    """One strategy as the harness sees it."""

    outer_loop: str
    """The CLI selection (`--outer-loop`) that runs the strategy."""
    decode_core: Callable[[str], CoreState]
    """Core's state out of the committed envelope's JSON, through the strategy's own codec."""


def _dynamic_core(envelope: str) -> CoreState:
    return OPERATIONS.registry().decode_envelope(RunEnvelope[FlowState], envelope).core


def _plain_core(envelope: str) -> CoreState:
    registry = issue_queue.registry()
    assert registry is not None
    return registry.decode_envelope(RunEnvelope[FlowState], envelope).core


LOOPS = {
    "dynamic": LoopSpec("dynamic", _dynamic_core),
    "plain": LoopSpec("plain", _plain_core),
}


class UnknownLoopError(ValueError):
    """The harness has no spec for this plugin id."""

    def __init__(self, plugin_id: str) -> None:
        super().__init__(f"no loop spec for plugin {plugin_id!r}; known: {sorted(LOOPS)}")


def loop_spec(plugin_id: str) -> LoopSpec:
    """The spec of the plugin, or a typed error naming the known ids."""
    try:
        return LOOPS[plugin_id]
    except KeyError:
        raise UnknownLoopError(plugin_id) from None
