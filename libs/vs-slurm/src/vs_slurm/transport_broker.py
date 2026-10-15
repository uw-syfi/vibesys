"""Authorization policy for exposing a configured Slurm transport to a host broker."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from vs_decode.api import Unreadable, ValidationOptions, decode_json

from .config import SlurmConfig, SlurmConnectorTransport, SlurmLocalTransport, SlurmSshTransport
from .connector_protocol import (
    MAX_CONNECTOR_REQUEST_BYTES,
    SLURM_CONNECTOR_REQUEST,
    SlurmConnectorGetRequest,
    SlurmConnectorPutRequest,
    SlurmConnectorSyncToRequest,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import ProcessSpec

_SSH_SUFFIX_LENGTH = 3
_RSYNC_OPERAND_COUNT = 2


class SlurmTransportRequestError(PermissionError):
    """A broker call is outside the configured transport capability."""

    @classmethod
    def invalid_capability(cls) -> SlurmTransportRequestError:
        """Reject a call whose unguessable per-run token does not match."""
        return cls("invalid Slurm transport capability")

    @classmethod
    def unauthorized_destination(cls) -> SlurmTransportRequestError:
        """Reject a destination other than the configured SSH host."""
        return cls("Slurm transport request has an unauthorized destination")

    @classmethod
    def unauthorized_executable(cls) -> SlurmTransportRequestError:
        """Reject a program other than the exact configured transport command."""
        return cls("Slurm transport executable is not authorized")

    @classmethod
    def malformed_rsync(cls) -> SlurmTransportRequestError:
        """Reject an rsync call outside the runner's generated command shape."""
        return cls("Slurm rsync request is malformed")

    @classmethod
    def unauthorized_options(cls) -> SlurmTransportRequestError:
        """Reject command options the configured transport never generates."""
        return cls("Slurm rsync options are not authorized")

    @classmethod
    def remote_path_denied(cls) -> SlurmTransportRequestError:
        """Reject a transfer outside the configured cluster namespace."""
        return cls("Slurm transport path is outside the configured remote namespace")

    @classmethod
    def local_path_denied(cls) -> SlurmTransportRequestError:
        """Reject a transfer outside the roots owned by this run."""
        return cls("Slurm transport path is outside the run-owned roots")

    @classmethod
    def invalid_connector_request(
        cls, error: Unreadable | None = None
    ) -> SlurmTransportRequestError:
        """Reject malformed connector JSON while reporting field names, never values."""
        locations = (
            sorted({problem.key_path for problem in error.problems if problem.key_path})
            if error is not None
            else []
        )
        suffix = f": invalid fields {', '.join(locations)}" if locations else ""
        return cls(f"Slurm connector request is malformed{suffix}")


class SlurmTransportBrokerPolicy(Protocol):
    """Authorize the one host process a confined Slurm client asks to start."""

    @property
    def available(self) -> bool:
        """Whether this transport can safely be exposed through the broker."""
        ...

    @property
    def unavailable_reason(self) -> str | None:
        """Why the transport cannot be exposed, or ``None`` when it can."""
        ...

    @property
    def timeout_seconds(self) -> float:
        """The operator's maximum duration for one transport call."""
        ...

    def authorize(self, spec: ProcessSpec) -> None:
        """Return for an allowed process request; raise without running otherwise."""
        ...


@dataclass(frozen=True, slots=True)
class _SshBrokerPolicy:
    transport: SlurmSshTransport
    remote_root: PurePosixPath
    local_roots: tuple[Path, ...]
    timeout_seconds: float
    available: bool = True
    unavailable_reason: str | None = None

    def authorize(self, spec: ProcessSpec) -> None:
        ssh = self.transport.ssh_command
        rsync = self.transport.rsync_command
        if spec.argv[: len(ssh)] == ssh:
            suffix = spec.argv[len(ssh) :]
            if len(suffix) != _SSH_SUFFIX_LENGTH or suffix[:2] != (
                "--",
                self.transport.host,
            ):
                raise SlurmTransportRequestError.unauthorized_destination()
            return
        if spec.argv[: len(rsync)] != rsync:
            raise SlurmTransportRequestError.unauthorized_executable()
        self._authorize_rsync(spec.argv[len(rsync) :])

    def _authorize_rsync(self, argv: tuple[str, ...]) -> None:
        try:
            separator = argv.index("--")
        except ValueError as error:
            raise SlurmTransportRequestError.malformed_rsync() from error
        options = argv[:separator]
        operands = argv[separator + 1 :]
        expected_shell = shlex.join(self.transport.ssh_command)
        if len(operands) != _RSYNC_OPERAND_COUNT or not options or options[0] != "-a":
            raise SlurmTransportRequestError.malformed_rsync()
        index = 1
        if index < len(options) and options[index] == "--delete":
            index += 1
        while index < len(options) and options[index].startswith("--exclude="):
            index += 1
        if options[index:] != ("-e", expected_shell):
            raise SlurmTransportRequestError.unauthorized_options()
        remote_indexes = [
            position
            for position, operand in enumerate(operands)
            if operand.startswith(f"{self.transport.host}:")
        ]
        if len(remote_indexes) != 1:
            raise SlurmTransportRequestError.unauthorized_destination()
        remote_index = remote_indexes[0]
        remote = operands[remote_index].split(":", 1)[1].rstrip("/")
        _authorize_remote_path(PurePosixPath(remote), self.remote_root)
        _authorize_local_path(Path(operands[1 - remote_index].rstrip("/")), self.local_roots)


@dataclass(frozen=True, slots=True)
class _ConnectorBrokerPolicy:
    command: tuple[str, ...]
    remote_root: PurePosixPath
    local_roots: tuple[Path, ...]
    timeout_seconds: float
    available: bool = True
    unavailable_reason: str | None = None

    def authorize(self, spec: ProcessSpec) -> None:
        if spec.argv != self.command:
            raise SlurmTransportRequestError.unauthorized_executable()
        request = decode_json(
            spec.input,
            SLURM_CONNECTOR_REQUEST,
            source="Slurm connector request",
            max_bytes=MAX_CONNECTOR_REQUEST_BYTES,
            options=ValidationOptions(strict=True),
        )
        if isinstance(request, Unreadable):
            raise SlurmTransportRequestError.invalid_connector_request(request)
        match request:
            case SlurmConnectorSyncToRequest(local_dir=local, remote_dir=remote):
                _authorize_local_path(Path(local), self.local_roots)
                _authorize_remote_path(PurePosixPath(remote), self.remote_root)
            case SlurmConnectorPutRequest(local_path=local, remote_path=remote):
                _authorize_local_path(Path(local), self.local_roots)
                _authorize_remote_path(PurePosixPath(remote), self.remote_root)
            case SlurmConnectorGetRequest(remote_path=remote, local_path=local):
                _authorize_remote_path(PurePosixPath(remote), self.remote_root)
                _authorize_local_path(Path(local), self.local_roots)
            case _:
                return


@dataclass(frozen=True, slots=True)
class _UnavailableBrokerPolicy:
    timeout_seconds: float
    available: bool = False
    unavailable_reason: str | None = (
        "the local Slurm transport executes an operator shell on the host and cannot be "
        "safely exposed to the editor container"
    )

    def authorize(self, spec: ProcessSpec) -> None:
        del spec
        raise SlurmTransportRequestError(self.unavailable_reason)


def _authorize_remote_path(path: PurePosixPath, root: PurePosixPath) -> None:
    if path != root and root not in path.parents:
        raise SlurmTransportRequestError.remote_path_denied()


def _authorize_local_path(path: Path, roots: tuple[Path, ...]) -> None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise SlurmTransportRequestError.local_path_denied() from error
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise SlurmTransportRequestError.local_path_denied()


def slurm_transport_broker_policy(
    config: SlurmConfig, *, local_roots: Sequence[Path] = ()
) -> SlurmTransportBrokerPolicy:
    """Return the authorization policy for *config*'s transport.

    SSH and connector transports can expose their bounded remote capability. The local
    transport cannot: its shell command is a direct host command, so brokering it would
    turn the profiler capability into arbitrary host execution.
    """
    roots = tuple(path.resolve() for path in local_roots)
    remote = PurePosixPath(config.remote_workspace_root) / config.name
    timeout = float(config.transport_timeout_seconds)
    match config.transport:
        case SlurmSshTransport() as transport:
            return _SshBrokerPolicy(transport, remote, roots, timeout)
        case SlurmConnectorTransport(command=command):
            return _ConnectorBrokerPolicy(command, remote, roots, timeout)
        case SlurmLocalTransport():
            return _UnavailableBrokerPolicy(timeout)


__all__ = [
    "SlurmTransportBrokerPolicy",
    "SlurmTransportRequestError",
    "slurm_transport_broker_policy",
]
