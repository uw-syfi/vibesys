"""A search that decides nothing, for tests of the run around a search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import Value
from vs_core.api.search import search

if TYPE_CHECKING:
    from vs_core.api import Flow


class CounterConfig(Value):
    """The counter search has nothing to configure."""


@search("sequential", config=CounterConfig)
async def counter(ctx: Flow, config: CounterConfig) -> None:
    """A search that decides nothing: it waits to be stopped."""
    del config
    await ctx.first(ctx.stopped)
