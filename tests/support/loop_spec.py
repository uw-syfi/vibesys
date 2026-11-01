"""What the whole-loop harness needs to know about a strategy, keyed by plugin id.

The scenario harness drives a strategy through the production host: it builds the CLI request,
folds the run.log and checks liveness. Everything in it that names a concrete
strategy lives here, so a ported strategy registers one `LoopSpec` and reuses the harness, the
loop invariants and the chaos driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.issue_queue.search.api import issue_queue

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import OperationRegistry, SearchDef


@dataclass(frozen=True)
class LoopSpec:
    """One strategy as the harness sees it."""

    outer_loop: str
    """The CLI selection (`--outer-loop`) that runs the strategy."""
    registry: Callable[[], OperationRegistry]
    """The operations the strategy registers, which its run.log's records are decoded with."""


def _registry_of(search: SearchDef) -> Callable[[], OperationRegistry]:
    """The search's whole codec: the kit's operations and the ones it writes beside them."""

    def registry() -> OperationRegistry:
        found = search.registry()
        assert found is not None
        return found

    return registry


LOOPS = {
    "dynamic": LoopSpec("dynamic", _registry_of(dynamic)),
    "plain": LoopSpec("plain", _registry_of(issue_queue)),
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
