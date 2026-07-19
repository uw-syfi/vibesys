"""Presentation layer: event emission sink and per-surface renderers.

The backend never prints to the terminal directly. It emits typed events
through :data:`~vibesys.render.sink.OutputSink` (see :func:`output_sink`)
and writes plain text to the durable run log. Every human-facing surface —
the headless terminal view, the TUI client — is a renderer subscribed to
the same event stream.
"""

from vibesys.render.format import format_status_prefix, format_token_count
from vibesys.render.headless import HeadlessRenderer, TodoDisplay
from vibesys.render.sink import OutputSink, output_sink

__all__ = [
    "HeadlessRenderer",
    "OutputSink",
    "TodoDisplay",
    "format_status_prefix",
    "format_token_count",
    "output_sink",
]
