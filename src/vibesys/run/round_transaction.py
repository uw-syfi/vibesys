"""Recoverable transaction for one completed optimization round.

A v4 write-ahead log (WAL) records the exact typed state transition before
candidate or framework state is committed. Completing or recovering the
transaction applies that transition and commits it atomically with candidate
edits.

Only version 4 journals are accepted. Older journals need an older VibeSys
release to recover them.
"""

# These boundary errors deliberately name the relevant path or transaction.
# ruff: noqa: TRY003

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from vibesys.run.git_tracker import FrameworkSnapshotStatus
from vs_project.api import ProjectStateError, StateSlot, StateTransition

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.run.git_tracker import GitTracker
    from vs_project.api import Project

_JOURNAL_SCHEMA_VERSION: Literal[4] = 4
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RoundTransactionError(RuntimeError):
    """Raised when a completed-round transaction cannot proceed safely."""


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
            raise ValueError("run_id must not be empty")
        return value


class _RoundJournal(_StrictJournal):
    """Exact transition for the canonical portable agent run state."""

    schema_version: Literal[4]
    state_transition_base64: str
    state_transition_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]

    @field_validator("state_transition_base64")
    @classmethod
    def _validate_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def state_transition[StateT: BaseModel](self, slot: StateSlot[StateT]) -> StateTransition:
        """Decode the portable state transition through its typed slot."""
        return slot.deserialize_transition(_decode_base64(self.state_transition_base64))

    def transition_payload(self) -> bytes:
        """Return the serialized typed transition bytes."""
        return _decode_base64(self.state_transition_base64)


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
            raise ValueError("checkpoint must contain at least one state transition")
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


class RoundTransaction[StateT: BaseModel]:
    """A prepared round transition obtained from ``coordinator.begin``."""

    def __init__(self, coordinator: RoundTransactionCoordinator[StateT], round_number: int) -> None:
        """Bind this handle to one coordinator and round number."""
        self._coordinator = coordinator
        self.round_number = round_number
        self._closed = False

    def complete(self) -> CompletedRound:
        """Apply and commit the prepared state transition."""
        if self._closed:
            raise RoundTransactionError(
                f"Round {self.round_number} transaction has already completed"
            )
        result = self._coordinator._complete(  # noqa: SLF001
            self.round_number
        )
        self._closed = True
        return result


class RoundTransactionCoordinator[StateT: BaseModel]:
    """Coordinate crash-safe typed state and candidate Git commits.

    ``begin(round_number, state_transition=...)`` durably journals an exact
    typed transition for the supplied portable slot. ``complete()`` applies
    and commits that transition with the candidate worktree. ``recover()`` is
    idempotent and rolls any journaled transition forward.
    """

    def __init__(
        self,
        project: Project,
        git: GitTracker,
        run_id: str,
        *,
        state_slot: StateSlot[StateT],
    ) -> None:
        """Validate and bind the project, Git tracker, and run identity."""
        project_root = project.root.resolve()
        if git.root.resolve() != project_root:
            raise RoundTransactionError(
                "Round transaction project and Git tracker must use the same project root"
            )
        if git.run_id != run_id:
            raise RoundTransactionError(
                f"Round transaction run {run_id!r} does not match Git tracker run {git.run_id!r}"
            )

        project.state.load_run(run_id)
        self._git = git
        self.run_id = run_id
        self._state_slot = state_slot
        self._journal_slot = project.state.local_namespace(run_id, "transaction").slot(
            "round.json",
            _RoundJournal,
        )

    def begin(
        self,
        round_number: int,
        *,
        state_transition: StateTransition,
    ) -> RoundTransaction[StateT]:
        """Durably prepare an exact typed state transition."""
        if round_number < 1:
            raise RoundTransactionError(f"Round number must be positive, got {round_number}")
        if self._load_optional_journal() is not None:
            raise RoundTransactionError(
                "An unfinished round transaction already exists; recover it before starting another"
            )

        pre_commit = self._git.current_sha()
        if pre_commit is None:
            raise RoundTransactionError("Round transactions require an initialized Git HEAD")
        self._require_clean_index()
        self._validate_state_transition(state_transition)

        transition_payload = self._state_slot.serialize_transition(state_transition)
        journal = _RoundJournal(
            schema_version=_JOURNAL_SCHEMA_VERSION,
            run_id=self.run_id,
            round_number=round_number,
            pre_commit=pre_commit,
            state_transition_base64=base64.b64encode(transition_payload).decode("ascii"),
            state_transition_sha256=_sha256(transition_payload),
        )
        self._journal_slot.save(journal)
        return RoundTransaction(self, round_number)

    def recover(self) -> RoundRecoveryOutcome:
        """Commit any journaled transition and restore its working-tree state."""
        journal = self._load_optional_journal()
        if journal is None:
            return RoundRecoveryOutcome.NO_TRANSACTION

        if not self._pre_commit_is_ancestor(journal.pre_commit):
            raise RoundTransactionError(
                "Cannot recover round transaction after Git history moved away from "
                f"its starting commit {journal.pre_commit}"
            )
        self._commit_state(journal)
        self._clear_journal()
        return RoundRecoveryOutcome.COMMITTED

    def _complete(self, round_number: int) -> CompletedRound:
        journal = self._load_journal()
        if journal.round_number != round_number:
            raise RoundTransactionError(
                f"Journal is for round {journal.round_number}, not round {round_number}"
            )
        if not self._pre_commit_is_ancestor(journal.pre_commit):
            raise RoundTransactionError(
                "Cannot complete round transaction after Git history moved away from "
                f"its starting commit {journal.pre_commit}"
            )

        completed = self._commit_state(journal)
        self._clear_journal()
        return completed

    def _commit_state(self, journal: _RoundJournal) -> CompletedRound:
        transition = journal.state_transition(self._state_slot)
        self._validate_state_transition(transition)
        snapshot = self._state_slot.snapshot_transition(transition)
        status = self._git.framework_snapshot_status(snapshot)
        current_sha = self._git.current_sha()

        # An unchanged cursor may already be exact while other run files are
        # still dirty. The pre-commit HEAD means this transaction has not
        # snapshotted those files yet.
        if current_sha == journal.pre_commit:
            self._state_slot.apply(transition)
            self._git.snapshot_with_framework_metadata(
                f"vibesys(round {journal.round_number}): record result",
                snapshot,
            )
        elif status is FrameworkSnapshotStatus.EXACT:
            self._state_slot.apply(transition)
        else:
            raise RoundTransactionError("Committed state differs from the transaction journal")

        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError("Git snapshot did not commit the exact state")
        checkpoint = self._git.current_sha()
        if checkpoint is None:
            raise RoundTransactionError("Git snapshot completed without an accessible HEAD")
        return CompletedRound(checkpoint=checkpoint)

    def _load_journal(self) -> _RoundJournal:
        journal = self._load_optional_journal()
        if journal is None:
            raise RoundTransactionError("Round transaction journal does not exist")
        return journal

    def _load_optional_journal(self) -> _RoundJournal | None:
        """Load and validate the WAL while preserving the coordinator error API."""
        try:
            envelope = self._journal_slot.load_optional()
        except ProjectStateError as exc:
            raise RoundTransactionError(f"Invalid round transaction journal: {exc}") from exc
        if envelope is None:
            return None
        journal = envelope
        if journal.run_id != self.run_id:
            raise RoundTransactionError(
                f"Round transaction journal belongs to run {journal.run_id!r}, not {self.run_id!r}"
            )
        self._validate_journal(journal)
        return journal

    def _validate_journal(self, journal: _RoundJournal) -> None:
        payload = journal.transition_payload()
        if _sha256(payload) != journal.state_transition_sha256:
            raise RoundTransactionError(
                "Round transaction journal state-transition digest does not match"
            )
        try:
            self._validate_state_transition(journal.state_transition(self._state_slot))
        except (TypeError, ValueError, ProjectStateError, RoundTransactionError) as exc:
            raise RoundTransactionError(
                f"Invalid state transition in round transaction journal: {exc}"
            ) from exc

    def _pre_commit_is_ancestor(self, pre_commit: str) -> bool:
        result = self._git.run(
            ["git", "merge-base", "--is-ancestor", pre_commit, "HEAD"],
            check=False,
        )
        return result.returncode == 0

    def _require_clean_index(self) -> None:
        result = self._git.run(["git", "diff", "--cached", "--quiet"], check=False)
        if result.returncode != 0:
            raise RoundTransactionError(
                "Cannot begin round transaction while the Git index contains staged changes"
            )

    def _validate_state_transition(self, transition: StateTransition) -> None:
        try:
            self._state_slot.validate_transition(transition)
            self._state_slot.snapshot_transition(transition)
        except ProjectStateError as exc:
            raise RoundTransactionError(
                f"Invalid round transaction state transition: {exc}"
            ) from exc

    def _clear_journal(self) -> None:
        self._journal_slot.save(None)


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
            raise RoundTransactionError(f"Checkpoint {self.sequence} has already completed")
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
            raise RoundTransactionError("Checkpoint project, Git tracker, and run must agree")
        if not models:
            raise RoundTransactionError("Checkpoint requires at least one declared typed slot")
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
            raise RoundTransactionError(f"Checkpoint sequence must be positive, got {sequence}")
        if self._journal_slot.load_optional() is not None:
            raise RoundTransactionError("An unfinished checkpoint already exists; recover it first")
        if not writes:
            raise RoundTransactionError("Checkpoint writes must not be empty")
        pre_commit = self._git.current_sha()
        if pre_commit is None:
            raise RoundTransactionError("Checkpoint requires an initialized Git HEAD")
        if candidate:
            staged = self._git.run(["git", "diff", "--cached", "--quiet"], check=False)
            if staged.returncode != 0:
                raise RoundTransactionError(
                    "Cannot checkpoint candidate while the Git index has staged changes"
                )
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
            raise RoundTransactionError(f"Checkpoint journal is not for sequence {sequence}")
        result = self._commit(journal)
        self._journal_slot.save(None)
        return result

    def _serialize_writes(self, writes: Mapping[str, BaseModel]) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for name, value in sorted(writes.items()):
            slot = self._slots.get(name)
            if slot is None:
                raise RoundTransactionError(f"Undeclared checkpoint slot {name!r}")
            transition = slot.transition(value)
            payloads[name] = slot.serialize_transition(transition)
        return payloads

    def _load_journal(self) -> _MultiSlotJournal | None:
        try:
            journal = self._journal_slot.load_optional()
        except ProjectStateError as exc:
            raise RoundTransactionError(f"Invalid checkpoint journal: {exc}") from exc
        if journal is None:
            return None
        if journal.run_id != self.run_id:
            raise RoundTransactionError(f"Checkpoint journal belongs to run {journal.run_id!r}")
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
            raise RoundTransactionError("Checkpoint journal digest does not match")
        for name, payload in namespace_files.items():
            if name in payloads:
                raise RoundTransactionError(f"Checkpoint journal duplicates slot {name!r}")
            self.namespace.snapshot_bytes(name, payload)
        for name, payload in payloads.items():
            slot = self._slots.get(name)
            if slot is None:
                raise RoundTransactionError(f"Checkpoint journal names undeclared slot {name!r}")
            slot.deserialize_transition(payload)
        return journal

    def _commit(self, journal: _MultiSlotJournal) -> CompletedRound:
        ancestor = self._git.run(
            ["git", "merge-base", "--is-ancestor", journal.pre_commit, "HEAD"], check=False
        )
        if ancestor.returncode != 0:
            raise RoundTransactionError(
                f"Git history moved away from checkpoint starting commit {journal.pre_commit}"
            )
        self._apply_journal(journal)
        snapshot = self.namespace.snapshot()
        if self._git.current_sha() == journal.pre_commit:
            label = journal.label or f"vibesys(round {journal.round_number}): record result"
            if journal.candidate:
                self._git.snapshot_with_framework_metadata(label, snapshot)
            else:
                self._git.snapshot_framework_state(label, snapshot)
        elif self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError("Committed state differs from the checkpoint journal")
        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError("Git snapshot did not commit exact checkpoint state")
        revision = self._git.current_sha()
        if revision is None:
            raise RoundTransactionError("Checkpoint completed without an accessible HEAD")
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
        raise ValueError("must contain canonical base64-encoded bytes") from exc


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()
