"""Render semantic events from exactly one already-started run."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from headless.render import HeadlessRenderer

if TYPE_CHECKING:
    from vibesys.api import RunHandle, RunResult


async def run(handle: RunHandle, *, renderer: HeadlessRenderer | None = None) -> RunResult:
    """Render *handle* through completion; cancellation requests a cooperative stop."""
    renderer = renderer or HeadlessRenderer()
    try:
        async for event in handle.events():
            renderer.handle(event)
        return await handle.result()
    except asyncio.CancelledError as cancellation:
        handle.stop()
        try:
            await handle.result()
        finally:
            raise cancellation
