"""Recoverable transaction for one completed optimization round.

A v4 write-ahead log (WAL) records the exact typed state transition before
candidate or framework state is committed. Completing or recovering the
transaction applies that transition and commits it atomically with candidate
edits.

Version 3 journals from older VibeSys releases remain recoverable. Their
completed-round payload and machine-local ``active.json`` transition are
handled by an injected compatibility adapter.
"""

# These boundary errors deliberately name the relevant path or transaction.
# ruff: noqa: TRY003

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
)

from vibesys.run.git_tracker import FrameworkSnapshotStatus
from vs_project.api import ProjectStateError, StateSlot, StateTransition

if TYPE_CHECKING:
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


class _V3RoundJournal(_StrictJournal):
    """Compatibility schema for the former split round/active transaction."""

    schema_version: Literal[3]
    active_transition_base64: str
    round_payload_base64: str
    round_payload_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]

    @field_validator("active_transition_base64", "round_payload_base64")
    @classmethod
    def _validate_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def active_transition(self, slot: StateSlot[BaseModel]) -> StateTransition:
        """Decode the legacy local active-state transition through its slot."""
        return slot.deserialize_transition(_decode_base64(self.active_transition_base64))

    def round_payload(self) -> bytes:
        """Return the exact legacy portable completed-round payload."""
        return _decode_base64(self.round_payload_base64)


class _V4RoundJournal(_StrictJournal):
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


_Journal = Annotated[
    _V3RoundJournal | _V4RoundJournal,
    Field(discriminator="schema_version"),
]


class _RoundJournal(RootModel[_Journal]):
    """Discriminated persisted journal envelope."""

    model_config = ConfigDict(frozen=True, strict=True)


@dataclass(frozen=True)
class CompletedRound:
    """Durable outputs produced by a successful round transaction."""

    checkpoint: str


class LegacyRoundRecovery(Protocol):
    """Policy adapter for journals written before typed v4 state transitions."""

    def validate(self, journal: _V3RoundJournal) -> None:
        """Validate one legacy journal before any mutation."""
        ...

    def commit(self, journal: _V3RoundJournal) -> CompletedRound:
        """Apply one legacy journal and return its committed checkpoint."""
        ...


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
        legacy_recovery: LegacyRoundRecovery | None = None,
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
        self._legacy_recovery = legacy_recovery
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
        journal = _V4RoundJournal(
            schema_version=_JOURNAL_SCHEMA_VERSION,
            run_id=self.run_id,
            round_number=round_number,
            pre_commit=pre_commit,
            state_transition_base64=base64.b64encode(transition_payload).decode("ascii"),
            state_transition_sha256=_sha256(transition_payload),
        )
        self._journal_slot.save(_RoundJournal(root=journal))
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
        self._commit_prepared(journal)
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

        completed = self._commit_prepared(journal)
        self._clear_journal()
        return completed

    def _commit_prepared(self, journal: _Journal) -> CompletedRound:
        if isinstance(journal, _V3RoundJournal):
            if self._legacy_recovery is None:
                raise RoundTransactionError("Legacy round recovery is unavailable")
            return self._legacy_recovery.commit(journal)
        return self._commit_state(journal)

    def _commit_state(self, journal: _V4RoundJournal) -> CompletedRound:
        transition = journal.state_transition(self._state_slot)
        self._validate_state_transition(transition)
        snapshot = self._state_slot.snapshot_transition(transition)
        status = self._git.framework_snapshot_status(snapshot)
        current_sha = self._git.current_sha()

        if status is FrameworkSnapshotStatus.EXACT:
            self._state_slot.apply(transition)
        elif current_sha == journal.pre_commit:
            self._state_slot.apply(transition)
            self._git.snapshot_with_framework_metadata(
                f"vibesys(round {journal.round_number}): record result",
                snapshot,
            )
        else:
            raise RoundTransactionError("Committed state differs from the transaction journal")

        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError("Git snapshot did not commit the exact state")
        checkpoint = self._git.current_sha()
        if checkpoint is None:
            raise RoundTransactionError("Git snapshot completed without an accessible HEAD")
        return CompletedRound(checkpoint=checkpoint)

    def _load_journal(self) -> _Journal:
        journal = self._load_optional_journal()
        if journal is None:
            raise RoundTransactionError("Round transaction journal does not exist")
        return journal

    def _load_optional_journal(self) -> _Journal | None:
        """Load and validate the WAL while preserving the coordinator error API."""
        try:
            envelope = self._journal_slot.load_optional()
        except ProjectStateError as exc:
            raise RoundTransactionError(f"Invalid round transaction journal: {exc}") from exc
        if envelope is None:
            return None
        journal = envelope.root
        if journal.run_id != self.run_id:
            raise RoundTransactionError(
                f"Round transaction journal belongs to run {journal.run_id!r}, not {self.run_id!r}"
            )
        if isinstance(journal, _V3RoundJournal):
            if self._legacy_recovery is None:
                raise RoundTransactionError("Legacy round recovery is unavailable")
            self._legacy_recovery.validate(journal)
        else:
            self._validate_v4_journal(journal)
        return journal

    def _validate_v4_journal(self, journal: _V4RoundJournal) -> None:
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


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("must contain canonical base64-encoded bytes") from exc


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()
