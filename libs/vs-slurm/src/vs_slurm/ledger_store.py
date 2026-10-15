"""The evaluation ledger on the cluster's shared filesystem, reached through the job transport."""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_sim.api import ProcessTimeoutError

from .ledger import (
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    decode_lease,
    decode_record,
    encode,
    validate_holder,
    validate_key,
    validate_project,
)
from .runner import SlurmError, _make_transport

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import Processes

    from .config import SlurmConfig

_LEDGER_DIRECTORY = "ledger"
_END = "VS-LEDGER-END"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RemoteEvaluationLedger:
    """The ledger of one project, stored next to the jobs it names.

    It reaches the cluster through the same transport as the jobs (the operator's
    configuration and ``processes``) and lives under the operator's staging root,
    outside any agent's mounts. Every write is an atomic rename or create-only
    link of a complete file, so a reader never sees a partial document.
    """

    def __init__(
        self,
        config: SlurmConfig,
        *,
        project: str,
        processes: Processes,
        scratch_root: Path | None = None,
        host_environment: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """Bind the project's ledger directory on the cluster."""
        self._transport = _make_transport(
            config,
            processes=processes,
            host_environment=host_environment or (lambda: os.environ),
        )
        self._root = (
            PurePosixPath(config.remote_workspace_root)
            / config.name
            / _LEDGER_DIRECTORY
            / validate_project(project)
        )
        self._scratch_root = scratch_root

    def read(self, key: str) -> LedgerEntry | None:
        """The request's current attempt and its leases, in one remote command."""
        directory = self._key_directory(key)
        quoted = shlex.quote(directory.as_posix())
        listing = (
            f'for f in {quoted}/attempt-*.json; do if test -f "$f"; then printf "A %s " "$f"; cat "$f"; fi; done; '
            f'for f in {quoted}/leases/*.json; do if test -f "$f"; then printf "L %s " "$f"; cat "$f"; fi; done; '
            f"printf '%s\\n' {_END}"
        )
        lines = self._exec(listing).splitlines()
        if not lines or lines[-1] != _END:
            raise LedgerError.malformed_reply("read")
        records: list[LedgerRecord] = []
        leases: list[LedgerLease] = []
        for line in lines[:-1]:
            kind, _, rest = line.partition(" ")
            path, _, document = rest.partition(" ")
            if kind == "A":
                records.append(decode_record(document, path=path))
            elif kind == "L":
                leases.append(decode_lease(document, path=path))
            else:
                raise LedgerError.malformed_reply("read")
        if not records:
            return None
        for record in records:
            if record.key != key:
                raise LedgerError.unreadable(directory.as_posix(), "record names another key")
        current = max(records, key=lambda record: record.attempt)
        return LedgerEntry(current, tuple(sorted(leases, key=lambda lease: lease.holder)))

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        """Create the record's attempt unless one exists; return the stored attempt."""
        path = self._attempt_path(record)
        outcome = self._publish(path, encode(record), create_only=True)
        if outcome == "CREATED":
            return record
        entry = self.read(record.key)
        stored = None if entry is None else entry.record
        if stored is None or stored.attempt < record.attempt:
            raise LedgerError.claim_unreadable(record.key)
        return stored

    def update(self, record: LedgerRecord) -> None:
        """Replace the state of an attempt that exists."""
        self._publish(self._attempt_path(record), encode(record), create_only=False)

    def renew(self, key: str, lease: LedgerLease) -> None:
        """Write this holder's lease; only this holder ever writes its file."""
        path = self._key_directory(key) / "leases" / f"{validate_holder(lease.holder)}.json"
        self._publish(path, encode(lease), create_only=False)

    def release(self, key: str, holder: str) -> None:
        """Drop this holder's lease, so only the others' and the grace period keep the job."""
        path = self._key_directory(key) / "leases" / f"{validate_holder(holder)}.json"
        self._exec(f"rm -f {shlex.quote(path.as_posix())}")

    def operator_file_sha256(self, path: str) -> str | None:
        """The SHA-256 of a file on the cluster's filesystem, or None when there is no such file."""
        quoted = shlex.quote(PurePosixPath(path).as_posix())
        lines = self._exec(
            f"if test -f {quoted}; then sha256sum < {quoted}; else printf '%s\\n' ABSENT; fi; "
            f"printf '%s\\n' {_END}"
        ).splitlines()
        match lines:
            case ["ABSENT", end] if end == _END:
                return None
            case [reply, end] if end == _END:
                digest = reply.split(" ", 1)[0]
            case _:
                raise LedgerError.malformed_reply("sha256sum")
        if _SHA256.fullmatch(digest) is None:
            raise LedgerError.malformed_reply("sha256sum")
        return digest

    def list_keys(self) -> tuple[str, ...]:
        """Every request the project's ledger holds."""
        quoted = shlex.quote(self._root.as_posix())
        lines = self._exec(
            f"if test -d {quoted}; then ls -1 {quoted}; fi; printf '%s\\n' {_END}"
        ).splitlines()
        if not lines or lines[-1] != _END:
            raise LedgerError.malformed_reply("listing")
        found = []
        for name in lines[:-1]:
            try:
                found.append(validate_key(name.strip()))
            except LedgerError:
                continue  # a stray entry is not a request
        return tuple(sorted(found))

    def _key_directory(self, key: str) -> PurePosixPath:
        return self._root / validate_key(key)

    def _attempt_path(self, record: LedgerRecord) -> PurePosixPath:
        return self._key_directory(record.key) / f"attempt-{record.attempt}.json"

    def _publish(self, path: PurePosixPath, content: str, *, create_only: bool) -> str:
        with tempfile.TemporaryDirectory(prefix="vs-ledger-", dir=self._scratch_root) as temporary:
            local = Path(temporary) / "document"
            local.write_text(content, encoding="utf-8")
            pending = path.with_name(f"{path.name}.pending.{Path(temporary).name}")
            directory = shlex.quote(path.parent.as_posix())
            self._exec(f"mkdir -p {directory}")
            try:
                self._transport.put(local, pending)
            except (OSError, SlurmError, ProcessTimeoutError) as error:
                raise LedgerError.transport(error) from error
            source = shlex.quote(pending.as_posix())
            target = shlex.quote(path.as_posix())
            publish = (
                f"if ln {source} {target} 2>/dev/null; then rm {source}; printf CREATED; "
                f"else rm {source}; printf EXISTS; fi"
                if create_only
                else f"mv {source} {target} && printf CREATED"
            )
            outcome = self._exec(f"{publish}; sync {directory}").strip()
        if outcome not in {"CREATED", "EXISTS"}:
            raise LedgerError.malformed_reply("publish")
        return outcome

    def _exec(self, command: str) -> str:
        try:
            return self._transport.exec(command).stdout
        except (OSError, SlurmError, ProcessTimeoutError, UnicodeError) as error:
            raise LedgerError.transport(error) from error


__all__ = ["RemoteEvaluationLedger"]
