"""Recoverable checkpoint for a policy's portable state namespace.

A v4 write-ahead log records exact typed state transitions and other namespace
files before candidate or framework state is committed. Completing or
recovering the checkpoint applies those bytes and commits the namespace with
candidate edits when requested.

Only version 4 journals are accepted. Older journals need an older VibeSys
release to recover them.
"""

# These boundary errors deliberately name the relevant path or transaction.

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from vibesys.run.git_tracker import FrameworkSnapshotStatus
from vs_project.api import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.run.git_tracker import GitTracker
    from vs_project.api import Project

_JOURNAL_SCHEMA_VERSION: Literal[4] = 4
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RoundTransactionError(RuntimeError):
    """Raised when a completed-round transaction cannot proceed safely."""

    @classmethod
    def already_completed(cls, sequence: int) -> Self:
        """Describe a transaction handle completed more than once."""
        return cls(f"Checkpoint {sequence} has already completed")

    @classmethod
    def participants_disagree(cls) -> Self:
        """Describe project, Git tracker, and run identities that do not match."""
        return cls("Checkpoint project, Git tracker, and run must agree")

    @classmethod
    def no_declared_slots(cls) -> Self:
        """Describe a coordinator created without typed slots."""
        return cls("Checkpoint requires at least one declared typed slot")

    @classmethod
    def invalid_sequence(cls, sequence: int) -> Self:
        """Describe a checkpoint requested for a non-positive sequence."""
        return cls(f"Checkpoint sequence must be positive, got {sequence}")

    @classmethod
    def unfinished_checkpoint(cls) -> Self:
        """Describe an existing checkpoint that must be recovered first."""
        return cls("An unfinished checkpoint already exists; recover it first")

    @classmethod
    def empty_writes(cls) -> Self:
        """Describe a checkpoint requested without any writes."""
        return cls("Checkpoint writes must not be empty")

    @classmethod
    def missing_head(cls) -> Self:
        """Describe a repository without an accessible HEAD commit."""
        return cls("Checkpoint requires an initialized Git HEAD")

    @classmethod
    def staged_index_changes(cls) -> Self:
        """Describe staged user changes that make a candidate checkpoint unsafe."""
        return cls("Cannot checkpoint candidate while the Git index has staged changes")

    @classmethod
    def journal_sequence_mismatch(cls, sequence: int) -> Self:
        """Describe a journal that belongs to another sequence."""
        return cls(f"Checkpoint journal is not for sequence {sequence}")

    @classmethod
    def undeclared_slot(cls, name: str) -> Self:
        """Describe a write to a slot the coordinator does not declare."""
        return cls(f"Undeclared checkpoint slot {name!r}")

    @classmethod
    def invalid_journal(cls, error: Exception) -> Self:
        """Describe a journal that could not be loaded as valid state."""
        return cls(f"Invalid checkpoint journal: {error}")

    @classmethod
    def journal_run_mismatch(cls, run_id: str) -> Self:
        """Describe a journal belonging to another run."""
        return cls(f"Checkpoint journal belongs to run {run_id!r}")

    @classmethod
    def journal_digest_mismatch(cls) -> Self:
        """Describe a journal whose payload digest does not match."""
        return cls("Checkpoint journal digest does not match")

    @classmethod
    def journal_duplicate_slot(cls, name: str) -> Self:
        """Describe a journal that lists one slot as both transition and file."""
        return cls(f"Checkpoint journal duplicates slot {name!r}")

    @classmethod
    def journal_undeclared_slot(cls, name: str) -> Self:
        """Describe a journal naming a slot the coordinator does not declare."""
        return cls(f"Checkpoint journal names undeclared slot {name!r}")

    @classmethod
    def history_moved(cls, pre_commit: str) -> Self:
        """Describe history that no longer contains the checkpoint's base."""
        return cls(f"Git history moved away from checkpoint starting commit {pre_commit}")

    @classmethod
    def committed_state_conflict(cls) -> Self:
        """Describe committed state that differs from the journal."""
        return cls("Committed state differs from the checkpoint journal")

    @classmethod
    def snapshot_not_exact(cls) -> Self:
        """Describe a Git snapshot that did not commit the exact state."""
        return cls("Git snapshot did not commit exact checkpoint state")

    @classmethod
    def inaccessible_head(cls) -> Self:
        """Describe a snapshot operation that left no accessible HEAD."""
        return cls("Checkpoint completed without an accessible HEAD")


class RoundRecoveryOutcome(StrEnum):
    """Observable result of checking for an interrupted round transaction."""

    NO_TRANSACTION = "no-transaction"
    COMMITTED = "committed"


class _StrictJournal(BaseModel):
    """Shared strict contract for versioned round transaction journals."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    round_number: int = Field(ge=1)
    pre_commit: Annotated[str, Field(pattern=_GIT_OBJECT_ID_PATTERN)]

    @field_validator("run_id")
    @classmethod
    def _require_run_id(cls, value: str) -> str:
        if not value:
            message = "run_id must not be empty"
            raise ValueError(message)
        return value


class _MultiSlotJournal(_StrictJournal):
    """Exact typed replacements for one portable policy namespace."""

    schema_version: Literal[4]
    transitions_base64: dict[str, str]
    namespace_files_base64: dict[str, str]
    transitions_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    candidate: bool = True
    label: str | None = None

    @field_validator("transitions_base64")
    @classmethod
    def _validate_transitions(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            message = "checkpoint must contain at least one state transition"
            raise ValueError(message)
        for payload in value.values():
            _decode_base64(payload)
        return value

    @field_validator("namespace_files_base64")
    @classmethod
    def _validate_namespace_files(cls, value: dict[str, str]) -> dict[str, str]:
        for payload in value.values():
            _decode_base64(payload)
        return value


@dataclass(frozen=True)
class CompletedRound:
    """Durable outputs produced by a successful round transaction."""

    checkpoint: str


class MultiSlotRoundTransaction:
    """Prepared replacement of several typed files in one policy namespace."""

    def __init__(self, coordinator: MultiSlotRoundTransactionCoordinator, sequence: int) -> None:
        """Bind the transaction to one coordinator and sequence."""
        self._coordinator = coordinator
        self.sequence = sequence
        self._closed = False

    def complete(self) -> CompletedRound:
        """Apply and commit the prepared replacements exactly once."""
        if self._closed:
            raise RoundTransactionError.already_completed(self.sequence)
        # lint-waiver: LW-007067 [SLF001]; the transaction handle uses its coordinator's private commit seam
        result = self._coordinator._complete(self.sequence)  # noqa: SLF001
        self._closed = True
        return result


class MultiSlotRoundTransactionCoordinator:
    """Journal typed writes before committing their namespace with candidate edits.

    Policy-owned schemas are supplied at run setup so recovery can validate every
    journaled document before replay. Interim writes to the portable namespace
    remain outside this completed-round transaction, for policies that keep a
    recoverable paid-work cursor between checkpoints.
    """

    def __init__(
        self,
        project: Project,
        git: GitTracker,
        run_id: str,
        *,
        namespace: str,
        models: Mapping[str, type[BaseModel]],
    ) -> None:
        """Validate the run identity and bind its declared typed slots."""
        if project.root.resolve() != git.root.resolve() or git.run_id != run_id:
            raise RoundTransactionError.participants_disagree()
        if not models:
            raise RoundTransactionError.no_declared_slots()
        project.state.load_run(run_id)
        self._git = git
        self.run_id = run_id
        self.namespace = project.state.portable_namespace(run_id, namespace)
        self._slots = {name: self.namespace.slot(name, model) for name, model in models.items()}
        self._journal_slot = project.state.local_namespace(run_id, "transaction").slot(
            "checkpoint.json", _MultiSlotJournal
        )

    def begin(
        self,
        sequence: int,
        *,
        writes: Mapping[str, BaseModel],
        candidate: bool = True,
        label: str | None = None,
    ) -> MultiSlotRoundTransaction:
        """Validate all requested writes, then durably journal their transitions."""
        if sequence < 1:
            raise RoundTransactionError.invalid_sequence(sequence)
        if self._load_journal() is not None:
            raise RoundTransactionError.unfinished_checkpoint()
        if not writes:
            raise RoundTransactionError.empty_writes()
        pre_commit = self._git.current_sha()
        if pre_commit is None:
            raise RoundTransactionError.missing_head()
        if candidate:
            staged = self._git.run(["git", "diff", "--cached", "--quiet"], check=False)
            if staged.returncode != 0:
                raise RoundTransactionError.staged_index_changes()
        payloads = self._serialize_writes(writes)
        namespace_files = {
            item.relative_path.as_posix(): item.contents
            for item in self.namespace.snapshot().files
            if item.relative_path.as_posix() not in writes
        }
        journal = _MultiSlotJournal(
            schema_version=_JOURNAL_SCHEMA_VERSION,
            run_id=self.run_id,
            round_number=sequence,
            pre_commit=pre_commit,
            transitions_base64={
                name: base64.b64encode(payload).decode("ascii")
                for name, payload in payloads.items()
            },
            namespace_files_base64={
                name: base64.b64encode(payload).decode("ascii")
                for name, payload in namespace_files.items()
            },
            transitions_sha256=self._payload_digest(
                payloads, namespace_files, candidate=candidate, label=label
            ),
            candidate=candidate,
            label=label,
        )
        self._journal_slot.save(journal)
        return MultiSlotRoundTransaction(self, sequence)

    def recover(self) -> RoundRecoveryOutcome:
        """Replay a journaled checkpoint after an interrupted process."""
        journal = self._load_journal()
        if journal is None:
            return RoundRecoveryOutcome.NO_TRANSACTION
        self._commit(journal)
        self._journal_slot.save(None)
        return RoundRecoveryOutcome.COMMITTED

    def _complete(self, sequence: int) -> CompletedRound:
        journal = self._load_journal()
        if journal is None or journal.round_number != sequence:
            raise RoundTransactionError.journal_sequence_mismatch(sequence)
        result = self._commit(journal)
        self._journal_slot.save(None)
        return result

    def _serialize_writes(self, writes: Mapping[str, BaseModel]) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for name, value in sorted(writes.items()):
            slot = self._slots.get(name)
            if slot is None:
                raise RoundTransactionError.undeclared_slot(name)
            transition = slot.transition(value)
            payloads[name] = slot.serialize_transition(transition)
        return payloads

    def _load_journal(self) -> _MultiSlotJournal | None:
        try:
            journal = self._journal_slot.load_optional()
        except ProjectStateError as exc:
            raise RoundTransactionError.invalid_journal(exc) from exc
        if journal is None:
            return None
        if journal.run_id != self.run_id:
            raise RoundTransactionError.journal_run_mismatch(journal.run_id)
        payloads = {
            name: _decode_base64(payload) for name, payload in journal.transitions_base64.items()
        }
        namespace_files = {
            name: _decode_base64(payload)
            for name, payload in journal.namespace_files_base64.items()
        }
        if (
            self._payload_digest(
                payloads,
                namespace_files,
                candidate=journal.candidate,
                label=journal.label,
            )
            != journal.transitions_sha256
        ):
            raise RoundTransactionError.journal_digest_mismatch()
        for name, payload in namespace_files.items():
            if name in payloads:
                raise RoundTransactionError.journal_duplicate_slot(name)
            self.namespace.snapshot_bytes(name, payload)
        for name, payload in payloads.items():
            slot = self._slots.get(name)
            if slot is None:
                raise RoundTransactionError.journal_undeclared_slot(name)
            slot.deserialize_transition(payload)
        return journal

    def _commit(self, journal: _MultiSlotJournal) -> CompletedRound:
        ancestor = self._git.run(
            ["git", "merge-base", "--is-ancestor", journal.pre_commit, "HEAD"], check=False
        )
        if ancestor.returncode != 0:
            raise RoundTransactionError.history_moved(journal.pre_commit)
        self._apply_journal(journal)
        snapshot = self.namespace.snapshot()
        if self._git.current_sha() == journal.pre_commit:
            label = journal.label or f"vibesys(round {journal.round_number}): record result"
            if journal.candidate:
                self._git.snapshot_with_framework_metadata(label, snapshot)
            else:
                self._git.snapshot_framework_state(label, snapshot)
        elif self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError.committed_state_conflict()
        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError.snapshot_not_exact()
        revision = self._git.current_sha()
        if revision is None:
            raise RoundTransactionError.inaccessible_head()
        return CompletedRound(checkpoint=revision)

    def _apply_journal(self, journal: _MultiSlotJournal) -> None:
        """Restore exact namespace bytes before creating or checking the commit."""
        expected = set(journal.namespace_files_base64) | set(journal.transitions_base64)
        for item in self.namespace.snapshot().files:
            name = item.relative_path.as_posix()
            if name not in expected:
                self.namespace.delete(name)
        for name, payload in journal.namespace_files_base64.items():
            self.namespace.write_bytes(name, _decode_base64(payload))
        for name, payload in journal.transitions_base64.items():
            slot = self._slots[name]
            slot.apply(slot.deserialize_transition(_decode_base64(payload)))

    @staticmethod
    def _payload_digest(
        payloads: Mapping[str, bytes],
        namespace_files: Mapping[str, bytes],
        *,
        candidate: bool,
        label: str | None,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(b"candidate\0" if candidate else b"state-only\0")
        digest.update((label or "").encode("utf-8"))
        digest.update(b"\0")
        for kind, files in ((b"transition", payloads), (b"namespace", namespace_files)):
            for name, payload in sorted(files.items()):
                digest.update(kind)
                digest.update(b"\0")
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(payload)
                digest.update(b"\0")
        return digest.hexdigest()


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        message = "must contain canonical base64-encoded bytes"
        raise ValueError(message) from exc
