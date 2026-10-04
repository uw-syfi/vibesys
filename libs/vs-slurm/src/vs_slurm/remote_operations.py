"""Atomic scheduler-wide claims and immutable operation evidence."""

from __future__ import annotations

import hashlib
import json
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from .config import SlurmConfig


class _Response(Protocol):
    @property
    def stdout(self) -> str: ...


class _Transport(Protocol):
    def exec(self, command: str) -> _Response: ...
    def put(self, local: Path, remote: PurePosixPath) -> None: ...


class RemoteOperationError(RuntimeError):
    """Remote identity evidence could not be safely interpreted."""

    @classmethod
    def invalid_evidence(cls) -> RemoteOperationError:
        """Describe malformed identity evidence without exposing its contents."""
        return cls("remote cluster operation evidence is missing or malformed")


class RemoteOperationEvidence(BaseModel):
    """Authoritative shape of a remote identity observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: dict[str, object] | None
    accepted: dict[str, object] | None
    rejected: str | None
    cancelled: bool


class RemoteOperationClaim(BaseModel):
    """Only a new, fully persisted claim authorizes one scheduler submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["created", "existing", "unknown"]
    evidence: RemoteOperationEvidence


def operation_job_name(config: SlurmConfig, operation_id: str) -> str:
    """Use bounded scheduler names scoped to the remote workspace namespace."""
    namespace = hashlib.sha256(
        (config.remote_workspace_root + "\0" + config.name).encode()
    ).hexdigest()[:16]
    operation = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
    return f"vs-{namespace}-{operation}"


class RemoteOperations:
    """Persist immutable intents, acceptance evidence and cancellation markers."""

    def __init__(
        self, transport: _Transport, config: SlurmConfig, scratch_root: Path | None
    ) -> None:
        """Connect the remote filesystem mechanism to the existing transport."""
        self._transport = transport
        self._root = PurePosixPath(config.remote_workspace_root) / config.name
        self._scratch_root = scratch_root

    def claim(self, operation_id: str, intent: str) -> RemoteOperationClaim:
        """Claim an operation atomically; an interrupted claim stays Unknown."""
        base = self._root / operation_id
        directory = base / ".cluster-operation"
        response = self._transport.exec(
            f"mkdir -p {shlex.quote(base.as_posix())} && "
            f"if mkdir {shlex.quote(directory.as_posix())} 2>/dev/null; then printf CREATED; else printf EXISTS; fi"
        ).stdout.strip()
        if response == "CREATED":
            self._write(directory / "intent.json", intent)
        elif response != "EXISTS":
            raise RemoteOperationError.invalid_evidence()
        evidence = self.inspect(operation_id)
        kind = "created" if response == "CREATED" else "existing"
        if evidence.intent is None:
            kind = "unknown"
        return RemoteOperationClaim(kind=kind, evidence=evidence)

    def inspect(self, operation_id: str) -> RemoteOperationEvidence:
        """Observe only atomically published records and cancellation intent."""
        base = self._root / operation_id
        directory = base / ".cluster-operation"
        pieces = []
        for index, name in enumerate(("intent", "accepted", "rejected")):
            path = shlex.quote((directory / (name + ".json")).as_posix())
            prefix = ("{" if index == 0 else ",") + json.dumps(name) + ":"
            pieces.append(
                f"printf '%s' {shlex.quote(prefix)}; if test -f {path}; then cat {path}; else printf null; fi"
            )
        marker = shlex.quote((base / ".cluster-cancelled").as_posix())
        pieces.append(
            f"printf '%s' ',\"cancelled\":'; if test -f {marker}; then printf true; else printf false; fi; printf '}}'"
        )
        response = self._transport.exec("; ".join(pieces)).stdout
        try:
            return RemoteOperationEvidence.model_validate_json(response, strict=True)
        except ValidationError as exc:
            raise RemoteOperationError.invalid_evidence() from exc

    def accepted(self, operation_id: str, record: str) -> None:
        """Publish the accepted locator separately from cancellation intent."""
        self._write(
            self._root / operation_id / ".cluster-operation" / "accepted.json",
            record,
            replace=False,
        )

    def rejected(self, operation_id: str, reason: str) -> None:
        """Publish a known rejection separately from the original intent."""
        self._write(
            self._root / operation_id / ".cluster-operation" / "rejected.json", json.dumps(reason)
        )

    def cancel(self, operation_id: str) -> None:
        """Retain a cancellation tombstone even before an operation is claimed."""
        base = self._root / operation_id
        self._transport.exec(f"mkdir -p {shlex.quote(base.as_posix())}")
        self._write(base / ".cluster-cancelled", "cancelled")

    def _write(self, path: PurePosixPath, content: str, *, replace: bool = True) -> None:
        with tempfile.TemporaryDirectory(
            prefix="vs-slurm-intent-", dir=self._scratch_root
        ) as temporary:
            local = Path(temporary) / "record"
            local.write_text(content, encoding="utf-8")
            pending = path.with_name(path.name + ".pending." + Path(temporary).name)
            self._transport.put(local, pending)
            quoted = shlex.quote(path.as_posix())
            temporary_remote = shlex.quote(pending.as_posix())
            publish = (
                f"mv {temporary_remote} {quoted}"
                if replace
                else f"if ln {temporary_remote} {quoted} 2>/dev/null; then rm {temporary_remote}; "
                f"else rm {temporary_remote}; test -f {quoted}; fi"
            )
            self._transport.exec(
                f"{publish} && sync -f {quoted} && sync -f {shlex.quote(path.parent.as_posix())}"
            )
