"""Translate the deprecated ``slurm-gpu`` operator file into ``slurm`` settings.

``--run-environment slurm-gpu`` is an alias for the ``slurm`` run environment
with local transport and agent GPU commands. This module is the only reader of
the old ``[slurm_gpu]`` table. The translation is a pure function; a field that
cannot be carried over faithfully is rejected by name rather than guessed at.

Field mapping (old key -> new setting):

* ``partitions`` -> ``agent_gpu.partitions``; gates get ``--partition=A,B,...``
  (the scheduler starts a gate in whichever listed partition is earliest).
* ``max_gpus``, ``max_time_minutes``, ``default_time_minutes``,
  ``windows_command``, ``job_name_prefix``, ``srun_command``, ``scancel_command``
  -> the same ``agent_gpu`` keys.
* ``gate_gpus`` (or the task's accelerators per node) -> gate ``--gres=gpu:N``.
* ``gate_time_minutes`` -> gate ``--time=M`` and, with a fixed queue allowance,
  the job timeout.
* ``srun_arguments`` -> ``agent_gpu.srun_arguments`` and the gates' sbatch
  arguments (they must therefore be valid for ``sbatch`` too, such as
  ``--account=lab``).
* the program that precedes ``srun`` in ``srun_command`` (a login-container
  wrapper, for example) -> the local transport's ``shell_command`` prefix, so
  ``sbatch`` runs where ``srun`` did. ``scancel_command`` must carry the same
  prefix.
"""

from __future__ import annotations

import tomllib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vs_sandbox.agent_gpu import AgentGpuConfig
from vs_sandbox.slurm_policy import SlurmExecutionPolicy, SlurmOperatorSettings
from vs_slurm.api import SlurmConfig, SlurmLocalTransport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

#: Time the gates may wait for an allocation, beyond their own run time.
QUEUE_ALLOWANCE_SECONDS = 3600
_CLUSTER_NAME = "slurm-gpu"


class SlurmGpuAliasError(ValueError):
    """The deprecated ``slurm-gpu`` file cannot become ``slurm`` settings."""

    @classmethod
    def load_failed(cls, path: Path, error: Exception) -> SlurmGpuAliasError:
        """Describe invalid settings by field without echoing their values."""
        if isinstance(error, ValidationError):
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in error.errors()}
            )
            detail = f"invalid settings: {', '.join(fields)}"
        else:
            detail = type(error).__name__
        return cls(f"Could not load Slurm GPU config at {path}: {detail}")

    @classmethod
    def unfaithful(cls, key: str, reason: str) -> SlurmGpuAliasError:
        """Name the key whose meaning the slurm environment cannot reproduce."""
        return cls(f"slurm_gpu.{key} cannot be translated to the slurm environment: {reason}")


class _LegacyTable(AgentGpuConfig):
    """The old ``[slurm_gpu]`` table: the agent's limits plus its gates' size."""

    gate_gpus: Annotated[int, Field(gt=0)] = 1
    gate_time_minutes: Annotated[int, Field(gt=0)] = 60

    @model_validator(mode="after")
    def _gate_within_limits(self) -> Self:
        if self.gate_gpus > self.max_gpus:
            message = "gate_gpus exceeds max_gpus"
            raise ValueError(message)
        if self.gate_time_minutes > self.max_time_minutes:
            message = "gate_time_minutes exceeds max_time_minutes"
            raise ValueError(message)
        return self


class _LegacyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    slurm_gpu: _LegacyTable


def _wrapper_prefix(key: str, command: tuple[str, ...], program: str) -> tuple[str, ...]:
    """Return what precedes *program* in *command*, or reject a command not ending in it."""
    if PurePosixPath(command[-1]).name != program:
        raise SlurmGpuAliasError.unfaithful(
            key, f"it must end with {program!r} so the wrapper in front of it can be carried over"
        )
    return command[:-1]


def translate_slurm_gpu(
    table: Mapping[str, object], *, task_gpus: int | None, stage_root: str
) -> SlurmOperatorSettings:
    """Translate a parsed ``[slurm_gpu]`` table into the ``slurm`` environment's settings.

    *task_gpus* is the task's accelerators per node, when it declares resources;
    it sizes the gates instead of ``gate_gpus``. *stage_root* is the absolute
    directory, shared with the compute nodes, that the gates stage into.
    """
    legacy = _LegacyTable.model_validate(table, strict=True)
    gate_gpus = legacy.gate_gpus if task_gpus is None else task_gpus
    if gate_gpus > legacy.max_gpus:
        raise SlurmGpuAliasError.unfaithful(
            "max_gpus",
            f"the task needs {gate_gpus} GPUs per gate but the limit is {legacy.max_gpus}",
        )
    prefix = _wrapper_prefix("srun_command", legacy.srun_command, "srun")
    if _wrapper_prefix("scancel_command", legacy.scancel_command, "scancel") != prefix:
        raise SlurmGpuAliasError.unfaithful(
            "scancel_command", "it must have the same wrapper as srun_command"
        )
    config = SlurmConfig(
        name=_CLUSTER_NAME,
        remote_workspace_root=stage_root,
        transport=SlurmLocalTransport(kind="local", shell_command=(*prefix, "bash", "-c")),
        sbatch_arguments=(
            f"--partition={','.join(legacy.partitions)}",
            f"--gres=gpu:{gate_gpus}",
            f"--time={legacy.gate_time_minutes}",
            "--ntasks=1",
            *legacy.srun_arguments,
        ),
        job_timeout_seconds=legacy.gate_time_minutes * 60 + QUEUE_ALLOWANCE_SECONDS,
    )
    agent_gpu = AgentGpuConfig.model_validate(
        legacy.model_dump(exclude={"gate_gpus", "gate_time_minutes"}), strict=True
    )
    return SlurmOperatorSettings(config, SlurmExecutionPolicy(agent_gpu=agent_gpu))


def load_slurm_gpu_alias_settings(
    path: Path, *, task_gpus: int | None, stage_root: str
) -> SlurmOperatorSettings:
    """Read the deprecated file once and translate it; failures name the file and keys."""
    config_path = path.expanduser()
    try:
        with config_path.open("rb") as handle:
            document = _LegacyDocument.model_validate(tomllib.load(handle), strict=True)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise SlurmGpuAliasError.load_failed(config_path, exc) from exc
    return translate_slurm_gpu(
        document.slurm_gpu.model_dump(), task_gpus=task_gpus, stage_root=stage_root
    )
