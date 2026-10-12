"""Where a tool a run needs can be run, as a run environment answers it.

A profiler (or any tool the agent drives) is an executable that runs in one *site* of
the run: the agent's own shell, or wherever the GPU workload runs. Different run
environments put those sites in different places (a container image, a Slurm job on a
compute node, the host), so only the environment can say whether an executable can be
run at a site. ``ToolLocation`` is that answer: a command that runs it, or the reason it
cannot be run there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ToolSite(StrEnum):
    """The place in a run where a tool executes."""

    AGENT_SHELL = "agent_shell"
    """The agent's own shell: the editor container, or the host when the agent runs there."""
    COMMAND_ENVIRONMENT = "command_environment"
    """Where the run's own commands execute (build, evaluation, attribution): a container
    image or host whose contents only the executing environment can inspect."""
    GPU_WORKLOAD = "gpu_workload"
    """Wherever the GPU workload runs: the accelerator container, a Slurm job, or the cluster."""


@dataclass(frozen=True, slots=True)
class ToolLocation:
    """A command that runs a tool at a site, or why none exists there."""

    command: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        """Require a command or a reason, never both and never neither."""
        if (self.command is None) == (not self.reason):
            message = "a ToolLocation has either a command or a reason it is missing"
            raise ValueError(message)

    @property
    def available(self) -> bool:
        """Whether the tool can be run at the site."""
        return self.command is not None

    @classmethod
    def found(cls, command: str) -> ToolLocation:
        """The tool runs at the site as *command* (a bare name, or an absolute path)."""
        return cls(command=command)

    @classmethod
    def missing(cls, reason: str) -> ToolLocation:
        """The tool cannot run at the site, for *reason*."""
        return cls(reason=reason)


def assumed_tool(executable: str) -> ToolLocation:
    """A tool the run's image or cluster is responsible for providing.

    The environment cannot inspect an image it has not built or a remote it has not
    reached, so it assumes the tool is there and a missing one surfaces when it is used.
    """
    return ToolLocation.found(executable)
