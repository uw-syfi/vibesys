"""The evaluation ledger on the cluster's shared filesystem, reached through the job transport."""

from __future__ import annotations

import base64
import binascii
import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_sim.api import ProcessTimeoutError

from .ledger import (
    FirstReads,
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    UnreadableLease,
    WithdrawalMark,
    WithdrawalRevision,
    WithdrawalUndone,
    check_transition,
    check_withdrawal,
    decode_lease,
    decode_record,
    encode,
    entry_of,
    read_withdrawal_revision,
    validate_holder,
    validate_key,
    validate_project,
)
from .runner import SlurmError, _make_transport

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import Clock, Processes

    from .config import SlurmConfig

_LEDGER_DIRECTORY = "ledger"
_END = "VS-LEDGER-END"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(
    r"(?P<kind>attempt|withdrawal)-(?P<attempt>[0-9]+)\.(?P<revision>[0-9]+)\.json"
)


class RemoteEvaluationLedger:
    """The ledger of one project, stored next to the jobs it names.

    It reaches the cluster through the same transport as the jobs (the operator's
    configuration and ``processes``) and lives under the operator's staging root,
    outside any agent's mounts. Every write is an atomic rename (a lease) or
    create-only link (a record or withdrawal revision) of a complete file, so a
    reader never sees a partial document and a revision is never overwritten.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-320601 [PLR0913]; each argument is an independent operator setting, injected effect or local resource.
        # > A carrier type would only rename this signature, and vs_sim's Platform is for
        # > components that use more than three interfaces; this one uses two.
        self,
        config: SlurmConfig,
        *,
        project: str,
        processes: Processes,
        clock: Clock,
        scratch_root: Path | None = None,
        host_environment: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """Bind the project's ledger directory on the cluster.

        ``clock`` is this reader's wall clock: a withdrawal revision whose file
        time the cluster cannot read is held from when this object first read
        it (``FirstReads``).
        """
        self._first_reads = FirstReads(clock)
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
        """The request's current attempt and its leases, in one remote command.

        A document that vanishes between the listing and the read (a released
        lease) is absent, never malformed: each file is read in one ``cat`` whose
        failure skips it. Each file's bytes travel base64-encoded, so a document
        with newlines or any other bytes is one line of the reply. A lease that
        is there but cannot be decoded is reported in ``unreadable_leases``,
        with its file's modification time; it never fails the read. A
        withdrawal revision that cannot be decoded is a withdrawal under way
        (``read_withdrawal_revision``). A file's time is read by ``stat``, or
        by ``date -r`` where ``stat`` has no ``-c`` (BSD); one neither reads is
        infinite: the lease holds, and the withdrawal holds from when this
        reader first read it (``FirstReads``).
        """
        directory = self._key_directory(key)
        quoted = shlex.quote(directory.as_posix())
        listing = (
            f"for f in {quoted}/attempt-*.json {quoted}/withdrawal-*.json {quoted}/leases/*.json; do "
            f'if b=$(base64 < "$f" 2>/dev/null); then '
            f'm=$(stat -c %Y "$f" 2>/dev/null) || m=$(date -r "$f" +%s 2>/dev/null) || m=inf; '
            f'printf "%s %s %s\\n" "$m" "$f" "$(printf %s "$b" | tr -d "\\n")"; fi; done; '
            f"printf '%s\\n' {_END}"
        )
        lines = self._exec(listing).splitlines()
        if not lines or lines[-1] != _END:
            raise LedgerError.malformed_reply("read")
        revisions: list[tuple[int, int, LedgerRecord]] = []
        withdrawals: list[tuple[int, int, WithdrawalRevision]] = []
        leases: list[LedgerLease] = []
        unreadable: list[UnreadableLease] = []
        for line in lines[:-1]:
            written, _, rest = line.partition(" ")
            path, _, encoded = rest.partition(" ")
            document = _document(encoded)
            name = PurePosixPath(path)
            if name.parent.name == "leases":
                try:
                    leases.append(decode_lease(document, path=path))
                except LedgerError:
                    unreadable.append(UnreadableLease(name.stem, _written_at(written)))
                continue
            numbered = _REVISION.fullmatch(name.name)
            if numbered is None:
                raise LedgerError.malformed_reply("read")
            attempt, revision = int(numbered["attempt"]), int(numbered["revision"])
            if numbered["kind"] == "withdrawal":
                withdrawal = read_withdrawal_revision(
                    document,
                    path=path,
                    attempt=attempt,
                    file_time=_written_at(written),
                    first_reads=self._first_reads,
                )
                withdrawals.append((attempt, revision, withdrawal))
                continue
            record = decode_record(document, path=path)
            if record.key != key or record.attempt != attempt:
                raise LedgerError.unreadable(path, "the record names another key or attempt")
            revisions.append((attempt, revision, record))
        return entry_of(revisions, withdrawals, leases, unreadable)

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        """Create the record's attempt unless one exists; return the stored attempt."""
        path = self._revision_path(record, 0)
        outcome = self._publish(path, encode(record), create_only=True)
        if outcome == "CREATED":
            return record
        entry = self.read(record.key)
        stored = None if entry is None else entry.record
        if stored is None or stored.attempt < record.attempt:
            raise LedgerError.claim_unreadable(record.key)
        return stored

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        """Write ``record`` as the next revision of ``current``; False if another writer won."""
        check_transition(current.record, record)
        path = self._revision_path(record, current.revision + 1)
        return self._publish(path, encode(record), create_only=True) == "CREATED"

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        """Write ``document`` as the withdrawal revision after ``current``'s; False if another won."""
        check_withdrawal(current, document)
        name = f"withdrawal-{document.attempt}.{current.withdrawal_revision + 1}.json"
        path = self._key_directory(current.record.key) / name
        return self._publish(path, encode(document), create_only=True) == "CREATED"

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

    def _revision_path(self, record: LedgerRecord, revision: int) -> PurePosixPath:
        return self._key_directory(record.key) / f"attempt-{record.attempt}.{revision}.json"

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


def _document(field: str) -> str:
    """A file's text from its base64 bytes; bytes that are not UTF-8 decode to a damaged document."""
    try:
        data = base64.b64decode(field, validate=True)
    except binascii.Error:
        raise LedgerError.malformed_reply("read") from None
    return data.decode("utf-8", errors="replace")


def _written_at(field: str) -> float:
    """A file's modification time as the listing printed it; a reply that is not one is malformed."""
    try:
        return float(field)
    except ValueError:
        raise LedgerError.malformed_reply("read") from None


__all__ = ["RemoteEvaluationLedger"]
