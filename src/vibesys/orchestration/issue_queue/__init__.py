"""Public surface for the issue-queue orchestration plugin."""

from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.models import IssueQueueOptions

if TYPE_CHECKING:
    from vibesys.orchestration.issue_queue.plugin import PLUGIN, REGISTRATION


def __getattr__(name: str) -> object:
    """Load execution wiring only when requested, keeping value and shell imports acyclic."""
    if name in {"PLUGIN", "REGISTRATION"}:
        # Eager wiring imports create a cycle through the core policy; importlib would hide the
        # declared dependency behind runtime loading.
        from vibesys.orchestration.issue_queue.plugin import (  # noqa: PLC0415  # lint-waiver: LW-771103 [PLC0415]; eager wiring creates a cycle through the core policy; importlib hides the declared dependency.
            PLUGIN,
            REGISTRATION,
        )

        return PLUGIN if name == "PLUGIN" else REGISTRATION
    raise AttributeError(name)


__all__ = ["PLUGIN", "REGISTRATION", "IssueQueueOptions"]
