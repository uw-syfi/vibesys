"""What a machine reports about itself: hardware, Docker, the checkout and its runs.

The facts ``machine.describe`` carries (``server.host_api`` re-exports them): a closed
``Accelerator`` set, ``Gpu`` and ``Hardware``, Docker's two answers, the helper's checkout
and the run counts. Pydantic and the standard library only, so the host helper's light import
path holds.

Public surface: ``Accelerator``, ``Gpu``, ``Hardware``, ``DockerReachable``,
``DockerUnreachable``, ``MachineCheckout``, ``RunCounts``, ``MachineDescription``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator


class Accelerator(StrEnum):
    """How a machine's GPUs run work; a closed set, ``cpu`` when it has none."""

    CPU = "cpu"
    CUDA = "cuda"
    ROCM = "rocm"
    METAL = "metal"


class Gpu(BaseModel):
    """One GPU as its vendor's tool names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor: str
    model: str


class Hardware(BaseModel):
    """The machine's processors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_cores: PositiveInt
    gpus: tuple[Gpu, ...]
    accelerator: Accelerator
    """How the listed GPUs run work; ``cpu`` exactly when none is listed."""

    @model_validator(mode="after")
    def _accelerator_matches_gpus(self) -> Hardware:
        if (self.accelerator is Accelerator.CPU) != (not self.gpus):
            message = "accelerator is cpu exactly when no GPU is listed"
            raise ValueError(message)
        return self


class DockerReachable(BaseModel):
    """Docker's daemon answered, with its server version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reachable: Literal[True] = True
    version: str


class DockerUnreachable(BaseModel):
    """Docker could not be reached, and why, in one line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reachable: Literal[False] = False
    reason: str


class MachineCheckout(BaseModel):
    """The VibeSys checkout this helper runs from, which starts runs on this machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str
    version: str
    commit: str | None
    """``HEAD``'s commit, or ``None`` when git could not say."""
    dirty: bool | None
    """Whether tracked files differ from ``HEAD``; ``None`` when git could not say."""


class RunCounts(BaseModel):
    """Runs on this machine: live now, and ended since the host's local midnight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    live: NonNegativeInt
    ended_today: NonNegativeInt


class MachineDescription(BaseModel):
    """What one machine offers runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkout: MachineCheckout | None
    """``None`` when the helper runs from an installed distribution."""
    hardware: Hardware
    docker: DockerReachable | DockerUnreachable
    harness_clis: tuple[str, ...]
    """The vetted harness providers whose CLI is installed on this machine, in offer order."""
    harness_cli_names: dict[str, str] = Field(default_factory=dict)
    """Each of ``harness_clis`` as people call it ("Claude Code"), as agentshim names it; a
    helper that predates it sends none and a client shows the provider id."""
    runs: RunCounts
