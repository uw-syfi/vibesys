"""VibeSys execution policy layered over the credential-neutral Slurm library."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from vs_sandbox.agent_gpu import AgentGpuConfig
from vs_sandbox.slurm_comparison import ComparisonMachine
from vs_slurm.api import (
    MAX_WITHDRAWAL_WINDOW_SECONDS,
    PORT_PLACEHOLDER,
    LedgerPolicy,
    SlurmConfig,
    SlurmLocalTransport,
    SlurmService,
    load_slurm_config,
    withdrawal_window_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_UV_PYTHON_COMMAND_LENGTH = 3


class SlurmPolicyError(ValueError):
    """Invalid VibeSys policy in an operator-owned Slurm configuration."""

    @classmethod
    def load_failed(cls, path: Path, error: Exception) -> SlurmPolicyError:
        """Describe invalid settings by field without exposing their values."""
        if isinstance(error, ValidationError):
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in error.errors()}
            )
            detail = f"invalid settings: {', '.join(fields)}"
        else:
            detail = type(error).__name__
        return cls(f"Could not load VibeSys Slurm policy at {path}: {detail}")

    @classmethod
    def agent_gpu_needs_local_transport(cls, transport_kind: str) -> SlurmPolicyError:
        """Name the table and the transport that cannot offer it."""
        return cls(
            'vibesys.agent_gpu needs slurm.transport kind = "local": the agent\'s GPU jobs '
            "run through this host's srun on a filesystem it shares with the compute nodes, "
            f"not through a {transport_kind!r} transport"
        )


class SlurmExecutionPolicy(BaseModel):
    """Commands and setup selected by VibeSys for jobs on a remote cluster.

    ``agent_gpu``, when present, lets the agent run its own GPU commands as
    Slurm jobs (see :func:`agent_gpu_capability`). ``ledger``, when present
    (the ``[vibesys.ledger]`` table), submits evaluations through the project's
    evaluation ledger on the cluster, so runs share the job of one request, and
    with ``keep_jobs_on_stop`` a restarted run reattaches to its jobs instead of
    resubmitting them (#2757).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    remote_python: str = Field(default="python3", min_length=1)
    setup_script: str | None = None
    # Before the arguments, whose validation reads it.
    service: SlurmService | None = None
    accuracy_arguments: tuple[str, ...] = ()
    benchmark_arguments: tuple[str, ...] = ()
    agent_gpu: AgentGpuConfig | None = None
    ledger: LedgerPolicy | None = None
    comparison: ComparisonMachine | None = None
    """The ``[vibesys.comparison]`` table: what this machine gives comparison baselines."""

    @field_validator("remote_python")
    @classmethod
    def _valid_remote_executable(cls, value: str) -> str:
        path = PurePosixPath(value)
        if "/" in value and (not path.is_absolute() or path.as_posix() != value):
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930045 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "remote_python must be an executable name or absolute POSIX path"
            )
        return value

    @field_validator("setup_script")
    @classmethod
    def _valid_setup_script(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if not path.is_absolute() or path.as_posix() != value:
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930046 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "setup_script must be an absolute normalized POSIX path"
            )
        return value

    @field_validator("accuracy_arguments", "benchmark_arguments", mode="before")
    @classmethod
    def _normalize_arguments(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("accuracy_arguments", "benchmark_arguments")
    @classmethod
    def _valid_arguments(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        # The job script defines PORT only when it starts the service; without
        # one, the placeholder expands an unset variable and aborts the job
        # before it records any status.
        if info.data.get("service") is None and any(PORT_PLACEHOLDER in item for item in value):
            message = f"{info.field_name} uses {PORT_PLACEHOLDER} but no service is configured"
            raise ValueError(message)
        if any(not argument for argument in value):
            raise ValueError("evaluator arguments must contain non-empty strings")  # noqa: TRY003  # lint-waiver: LW-930047 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    def remote_argv(self, command: Sequence[str]) -> tuple[str, ...]:
        """Translate a local Python argv to the configured remote interpreter."""
        parts = tuple(command)
        if (
            len(parts) >= _UV_PYTHON_COMMAND_LENGTH
            and parts[:2] == ("uv", "run")
            and _is_python(parts[2])
        ):
            return (self.remote_python, *parts[3:])
        if parts and _is_python(parts[0]):
            return (self.remote_python, *parts[1:])
        return parts

    def private_paths(self) -> tuple[str, ...]:
        """The absolute cluster paths the operator wrote: interpreter, setup script, service and arguments.

        What a stage reports may echo them; they are the operator's, not the agent's to see.
        """
        service = () if self.service is None else self.service.command
        machine = (
            ()
            if self.comparison is None
            else (self.comparison.image_cache_dir, self.comparison.model_cache_dir)
        )
        words = (
            self.remote_python,
            self.setup_script or "",
            *service,
            *self.accuracy_arguments,
            *self.benchmark_arguments,
            *machine,
        )
        return tuple(word for word in words if word.startswith("/"))

    def remote_service(self) -> SlurmService | None:
        """Return the configured service with its Python launcher translated."""
        if self.service is None:
            return None
        return self.service.model_copy(update={"command": self.remote_argv(self.service.command)})


class _SlurmOperatorDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    slurm: dict[str, object]
    vibesys: SlurmExecutionPolicy = Field(default_factory=SlurmExecutionPolicy)


def load_slurm_policy(path: Path) -> SlurmExecutionPolicy:
    """Load VibeSys policy from the operator file without copying credentials."""
    config_path = path.expanduser()
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
        return _SlurmOperatorDocument.model_validate(document, strict=True).vibesys
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise SlurmPolicyError.load_failed(config_path, exc) from exc


def agent_gpu_capability(
    config: SlurmConfig, policy: SlurmExecutionPolicy
) -> AgentGpuConfig | None:
    """Return the agent GPU limits the run offers, or ``None`` when the operator configured none.

    The capability runs ``srun`` on this host, so it needs the local transport.
    """
    if policy.agent_gpu is None:
        return None
    if not isinstance(config.transport, SlurmLocalTransport):
        raise SlurmPolicyError.agent_gpu_needs_local_transport(config.transport.kind)
    return policy.agent_gpu


@dataclass(frozen=True, slots=True)
class SlurmOperatorSettings:
    """Everything the operator's Slurm file says, parsed and checked once.

    The cluster (``[slurm]``), the VibeSys policy (``[vibesys]``), and, through
    the policy, the agent GPU limits. Construction rejects a combination the
    run could not honor (agent GPU commands without the local transport, or a
    transport timeout whose withdrawal mark outlasts the longest one allowed), so
    holding a value means the file is usable and no later read can differ.
    """

    config: SlurmConfig
    policy: SlurmExecutionPolicy

    def __post_init__(self) -> None:
        """Reject agent GPU limits the cluster transport cannot serve, and a ledger its timeout breaks."""
        agent_gpu_capability(self.config, self.policy)
        _check_withdrawal_window(self.config, self.policy)

    @property
    def agent_gpu(self) -> AgentGpuConfig | None:
        """The agent's GPU limits, or ``None`` when the capability is off."""
        return self.policy.agent_gpu

    @property
    def agent_gpu_shell_command(self) -> tuple[str, ...] | None:
        """How the agent's GPU jobs reach Slurm: the local transport's shell, or ``None``.

        It is ``None`` exactly when the capability is off, since construction
        guarantees the capability has a local transport.
        """
        transport = self.config.transport
        if self.policy.agent_gpu is None or not isinstance(transport, SlurmLocalTransport):
            return None
        return transport.shell_command


def _check_withdrawal_window(config: SlurmConfig, policy: SlurmExecutionPolicy) -> None:
    """Refuse a transport timeout whose withdrawal mark outlasts ``MAX_WITHDRAWAL_WINDOW_SECONDS``.

    A run's withdrawal mark holds ``withdrawal_window_seconds`` of the
    transport timeout, and a reader that cannot decode the mark undoes it after
    ``MAX_WITHDRAWAL_WINDOW_SECONDS``; ``WithdrawalTiming`` refuses the same
    timeout when a withdrawal starts, and this names the file's keys at load.
    The claim window is not bound by the mark's: an unreadable mark holds at
    least ``MAX_WITHDRAWAL_WINDOW_SECONDS`` whatever the claim window.
    """
    if policy.ledger is None:
        return
    window = withdrawal_window_seconds(config.transport_timeout_seconds)
    if window > MAX_WITHDRAWAL_WINDOW_SECONDS:
        message = (
            f"slurm.transport_timeout_seconds={config.transport_timeout_seconds:g} makes a "
            f"withdrawal mark hold {window:g} s, more than the longest any withdrawal may "
            f"hold ({MAX_WITHDRAWAL_WINDOW_SECONDS:g} s), with [vibesys.ledger] set; "
            "lower slurm.transport_timeout_seconds"
        )
        raise SlurmPolicyError(message)


def load_slurm_operator_settings(path: Path) -> SlurmOperatorSettings:
    """Parse the operator file once; failures name the file and the offending fields."""
    return SlurmOperatorSettings(load_slurm_config(path), load_slurm_policy(path))


def _is_python(executable: str) -> bool:
    return PurePosixPath(executable).name in {
        "python",
        "python3",
        PurePosixPath(sys.executable).name,
    }
