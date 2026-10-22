"""Typed read-operation facade behind existing host adapters.

The generated registry owns routing, validation, budgets and correlation. The
adapter supplies host facts through ReadSource; no server module enters here.
"""

from vibesys.api.host_operations._handlers import handlers
from vibesys.api.host_operations._reads import ReadSource
from vs_proto.api import Accepted

__all__ = ["ReadSource", "execute_read"]


def execute_read(request: Accepted, source: ReadSource) -> bytes:
    """Execute one immutable accepted request with complete typed handler wiring."""
    return handlers(source).dispatch(request)
