"""What the whole-loop harness needs to know about a strategy, keyed by plugin id.

The scenario harness drives a strategy through the production host: it builds the CLI request,
folds the run.log and checks liveness. Everything in it that names a concrete
strategy lives here, so a ported strategy registers one `LoopSpec` and reuses the harness, the
loop invariants and the chaos driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.search.api import OPERATIONS
from vibesys.orchestration.issue_queue.search.api import issue_queue

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import OperationRegistry


@dataclass(frozen=True)
class LoopSpec:
    """One strategy as the harness sees it."""

    outer_loop: str
    """The CLI selection (`--outer-loop`) that runs the strategy."""
    registry: Callable[[], OperationRegistry]
    """The operations the strategy registers, which its run.log's records are decoded with."""


def _plain_registry() -> OperationRegistry:
    registry = issue_queue.registry()
    assert registry is not None
    return registry


LOOPS = {
    "dynamic": LoopSpec("dynamic", OPERATIONS.registry),
    "plain": LoopSpec("plain", _plain_registry),
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
