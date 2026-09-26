"""``ctx.progress``: the host-owned pending framework-log buffer.

Split from ``runtime.py`` by capability; see that module's docstring.

Strategies used to keep their own ``list[str]`` buffer (``self._board_log``),
thread it through their turns object, and pass ``board=``/``progress_path=``
explicitly on every ``ctx.state.commit``/``ctx.gates.run`` call. This
capability moves that buffer to the host: a strategy declares its progress
path once (:meth:`_Progress.declare`), then calls :meth:`_Progress.note` as
pure blocks become available (see
:mod:`vibesys.orchestration.progress_log`'s ``render_*`` functions).
``ctx.state.commit`` and ``ctx.gates.run`` drain and write the buffer
themselves; a strategy never renders or writes anything for it directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration._host import HostResources


class _Progress:
    """Buffer pending framework-log blocks; declare the run's board path once."""

    def __init__(self, host: HostResources) -> None:
        self._host = host
        self._path: Path | None = None
        self._pending: list[str] = []

    @property
    def path(self) -> Path | None:
        """Return this run's declared progress-board path, or ``None`` if undeclared."""
        return self._path

    def declare(self, path: Path) -> None:
        """Declare this run's progress-board path.

        Called once, early, by the owning strategy (typically where it
        resolves its board layout); ``ctx.state.commit`` and
        ``ctx.gates.run`` write pending entries here without the strategy
        naming the path again on every call.
        """
        self._path = path

    def note(self, block: str) -> None:
        """Buffer one pre-rendered framework-log block for the next flush.

        *block* is one ``vibesys.orchestration.progress_log.render_*``
        output: pure, unwritten Markdown. Only ``ctx.state.commit`` and
        ``ctx.gates.run`` write it to disk.
        """
        self._pending.append(block)

    def drain(self) -> list[str]:
        """Take and clear the pending blocks, in the order they were noted."""
        pending = self._pending
        self._pending = []
        return pending
