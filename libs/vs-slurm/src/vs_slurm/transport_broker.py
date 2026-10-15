"""Authorization policy for exposing a configured Slurm transport to a host broker."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, assert_never

from vs_decode.api import Unreadable, ValidationOptions, decode_json

from .config import SlurmConfig, SlurmConnectorTransport, SlurmLocalTransport, SlurmSshTransport
from .connector_protocol import (
    MAX_CONNECTOR_REQUEST_BYTES,
    MAX_TRANSPORT_STREAM_BYTES,
    SLURM_CONNECTOR_REQUEST,
    SlurmConnectorGetRequest,
    SlurmConnectorPutRequest,
    SlurmConnectorRequest,
    SlurmConnectorSyncToRequest,
)
from .staging import (
    _authorize_local_path,
    _LocalTransfer,
    _LocalTransferError,
    _run_staged_transfer,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import Processes, ProcessRun, ProcessSpec

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

    @classmethod
    def unavailable_local_transport(cls) -> SlurmTransportRequestError:
        """Reject exposing the local operator shell through a host broker."""
        return cls(
            "the local Slurm transport executes an operator shell on the host and cannot be "
            "safely exposed to the editor container"
        )


class SlurmTransportBrokerPolicy(Protocol):
    """Atomically authorize and run one confined client's host transport request."""

    @property
    def timeout_seconds(self) -> float:
        """The operator's maximum duration for one transport call."""
        ...

    def run(self, spec: ProcessSpec, *, processes: Processes, timeout: float) -> ProcessRun:
        """Run the exact authorized request; raise before side effects when it is refused."""
        ...


@dataclass(frozen=True, slots=True)
class _SshBrokerPolicy:
    transport: SlurmSshTransport
    remote_root: PurePosixPath
    local_roots: tuple[Path, ...]
    timeout_seconds: float

    def run(self, spec: ProcessSpec, *, processes: Processes, timeout: float) -> ProcessRun:
        ssh = self.transport.ssh_command
        rsync = self.transport.rsync_command
        if spec.argv[: len(ssh)] == ssh:
            suffix = spec.argv[len(ssh) :]
            if len(suffix) != _SSH_SUFFIX_LENGTH or suffix[:2] != (
                "--",
                self.transport.host,
            ):
                raise SlurmTransportRequestError.unauthorized_destination()
            return _run_bounded(spec, processes=processes, timeout=timeout)
        if spec.argv[: len(rsync)] != rsync:
            raise SlurmTransportRequestError.unauthorized_executable()
        try:
            transfer = self._authorize_rsync(spec.argv[len(rsync) :])
            return _run_staged_transfer(
                transfer=transfer,
                processes=processes,
                timeout=timeout,
                rewrite=lambda local: replace(
                    spec,
                    argv=(
                        *spec.argv[: transfer.operand_index],
                        local,
                        *spec.argv[transfer.operand_index + 1 :],
                    ),
                ),
            )
        except _LocalTransferError as error:
            raise SlurmTransportRequestError.local_path_denied() from error

    def _authorize_rsync(self, argv: tuple[str, ...]) -> _LocalTransfer:
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
        _authorize_remote_path(remote, self.remote_root)
        local_index = 1 - remote_index
        local_operand = operands[local_index]
        tree = local_operand.endswith("/") or operands[remote_index].endswith("/")
        target = _authorize_local_path(local_operand.rstrip("/"), self.local_roots)
        return _LocalTransfer(
            target=target,
            direction="upload" if remote_index == 1 else "download",
            kind="tree" if tree else "file",
            operand_index=len(self.transport.rsync_command) + separator + 1 + local_index,
            trailing_slash=local_operand.endswith("/"),
        )


@dataclass(frozen=True, slots=True)
class _ConnectorBrokerPolicy:
    command: tuple[str, ...]
    remote_root: PurePosixPath
    local_roots: tuple[Path, ...]
    timeout_seconds: float

    def run(self, spec: ProcessSpec, *, processes: Processes, timeout: float) -> ProcessRun:
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
        try:
            match request:
                case SlurmConnectorSyncToRequest(local_dir=local, remote_dir=remote):
                    _authorize_remote_path(remote, self.remote_root)
                    transfer = _LocalTransfer(
                        _authorize_local_path(local, self.local_roots), "upload", "tree"
                    )
                case SlurmConnectorPutRequest(local_path=local, remote_path=remote):
                    _authorize_remote_path(remote, self.remote_root)
                    transfer = _LocalTransfer(
                        _authorize_local_path(local, self.local_roots), "upload", "file"
                    )
                case SlurmConnectorGetRequest(remote_path=remote, local_path=local):
                    _authorize_remote_path(remote, self.remote_root)
                    transfer = _LocalTransfer(
                        _authorize_local_path(local, self.local_roots), "download", request.kind
                    )
                case _:
                    return _run_bounded(spec, processes=processes, timeout=timeout)
            return _run_staged_transfer(
                transfer=transfer,
                processes=processes,
                timeout=timeout,
                rewrite=lambda local: replace(
                    spec,
                    input=_connector_with_local_path(request, local).model_dump_json().encode(),
                ),
            )
        except _LocalTransferError as error:
            raise SlurmTransportRequestError.local_path_denied() from error


def _run_bounded(spec: ProcessSpec, *, processes: Processes, timeout: float) -> ProcessRun:
    bounded = replace(
        spec,
        max_output_bytes=min(spec.max_output_bytes, MAX_TRANSPORT_STREAM_BYTES),
    )
    return processes.run(bounded, timeout=timeout)


def _connector_with_local_path(request: SlurmConnectorRequest, local: str) -> SlurmConnectorRequest:
    match request:
        case SlurmConnectorSyncToRequest():
            return request.model_copy(update={"local_dir": local})
        case SlurmConnectorPutRequest():
            return request.model_copy(update={"local_path": local})
        case SlurmConnectorGetRequest():
            return request.model_copy(update={"local_path": local})
        case _:
            raise AssertionError


def _authorize_remote_path(value: str, root: PurePosixPath) -> None:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or (path != root and root not in path.parents)
    ):
        raise SlurmTransportRequestError.remote_path_denied()


def slurm_transport_broker_available(config: SlurmConfig) -> bool:
    """Return whether *config* has a bounded transport capability safe to broker."""
    match config.transport:
        case SlurmSshTransport() | SlurmConnectorTransport():
            return True
        case SlurmLocalTransport():
            return False
        case _ as unreachable:
            assert_never(unreachable)


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
            raise SlurmTransportRequestError.unavailable_local_transport()
        case _ as unreachable:
            assert_never(unreachable)


__all__ = [
    "SlurmTransportBrokerPolicy",
    "SlurmTransportRequestError",
    "slurm_transport_broker_available",
    "slurm_transport_broker_policy",
]
