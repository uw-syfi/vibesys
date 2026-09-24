"""Recoverable transaction for one completed agent optimization round.

The agent loop owns one portable ``agent/state.json`` document. A v4 write-
ahead log (WAL) records the exact typed transition for that document before
candidate or framework state is committed. Completing or recovering the
transaction applies that transition and commits it atomically with candidate
edits.

Version 3 journals from older VibeSys releases remain recoverable. Their
completed-round payload and machine-local ``active.json`` transition are
handled only at this compatibility boundary.
"""

# These boundary errors deliberately name the relevant path or transaction.

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
)

from vibesys.run.git_tracker import FrameworkSnapshotStatus
from vs_loop_state.api import RoundRecord, parse_round_record
from vs_project.api import ProjectStateError, StateSlot, StateTransition

if TYPE_CHECKING:
    from vibesys.run.git_tracker import GitTracker
    from vs_project.api import Project

_JOURNAL_SCHEMA_VERSION: Literal[4] = 4
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RoundTransactionError(RuntimeError):
    """Raised when a completed-round transaction cannot proceed safely."""

    @classmethod
    def already_completed(cls, round_number: int) -> Self:
        """Describe a transaction handle completed more than once."""
        return cls(f"Round {round_number} transaction has already completed")

    @classmethod
    def different_project_root(cls) -> Self:
        """Describe project and Git boundaries rooted in different directories."""
        return cls("Round transaction project and Git tracker must use the same project root")

    @classmethod
    def run_id_mismatch(cls, run_id: str, git_run_id: str) -> Self:
        """Describe a tracker bound to another run."""
        return cls(
            f"Round transaction run {run_id!r} does not match Git tracker run {git_run_id!r}"
        )

    @classmethod
    def invalid_round_number(cls, round_number: int) -> Self:
        """Describe a transaction requested for a non-positive round."""
        return cls(f"Round number must be positive, got {round_number}")

    @classmethod
    def unfinished_transaction(cls) -> Self:
        """Describe an existing transaction that must be recovered first."""
        return cls(
            "An unfinished round transaction already exists; recover it before starting another"
        )

    @classmethod
    def already_active(cls) -> Self:
        """Describe a context that already owns an active round transaction."""
        return cls("a completed-round transaction is already active")

    @classmethod
    def begin_required(cls) -> Self:
        """Describe persistence attempted without beginning its transaction."""
        return cls("begin_completed_round must precede project round persistence")

    @classmethod
    def missing_head(cls) -> Self:
        """Describe a repository without an accessible HEAD commit."""
        return cls("Round transactions require an initialized Git HEAD")

    @classmethod
    def history_moved(cls, action: str, pre_commit: str) -> Self:
        """Describe history that no longer contains the transaction's base."""
        return cls(
            f"Cannot {action} round transaction after Git history moved away from its starting commit {pre_commit}"
        )

    @classmethod
    def journal_round_mismatch(cls, actual: int, expected: int) -> Self:
        """Describe a journal belonging to another round."""
        return cls(f"Journal is for round {actual}, not round {expected}")

    @classmethod
    def agent_state_conflict(cls) -> Self:
        """Describe agent state that differs from the durable transaction."""
        return cls("Committed agent state differs from the transaction journal")

    @classmethod
    def agent_state_snapshot_not_exact(cls) -> Self:
        """Describe a Git snapshot that did not commit the agent state."""
        return cls("Git snapshot did not commit the exact agent state")

    @classmethod
    def completed_round_snapshot_not_exact(cls) -> Self:
        """Describe a Git snapshot that did not commit completed-round metadata."""
        return cls("Git snapshot did not commit the exact completed-round metadata")

    @classmethod
    def inaccessible_head(cls) -> Self:
        """Describe a snapshot operation that left no accessible HEAD."""
        return cls("Git snapshot completed without an accessible HEAD")

    @classmethod
    def round_payload_number_mismatch(cls, actual: int, expected: int) -> Self:
        """Describe legacy round metadata that names another round."""
        return cls(f"Round transaction journal payload is for round {actual}, not round {expected}")

    @classmethod
    def round_metadata_conflict(cls) -> Self:
        """Describe committed round metadata that differs from the journal."""
        return cls("Committed round metadata differs from the transaction journal")

    @classmethod
    def journal_missing(cls) -> Self:
        """Describe a transaction journal that is required but absent."""
        return cls("Round transaction journal does not exist")

    @classmethod
    def invalid_journal(cls, details: str) -> Self:
        """Describe a journal that could not be loaded as valid state."""
        return cls(f"Invalid round transaction journal: {details}")

    @classmethod
    def journal_run_mismatch(cls, actual: str, expected: str) -> Self:
        """Describe a journal belonging to another run."""
        return cls(f"Round transaction journal belongs to run {actual!r}, not {expected!r}")

    @classmethod
    def payload_digest_mismatch(cls, payload: str) -> Self:
        """Describe a journal payload whose digest does not match."""
        return cls(f"Round transaction journal {payload} digest does not match")

    @classmethod
    def invalid_active_transition(cls, details: str) -> Self:
        """Describe an invalid legacy active-state transition."""
        return cls(f"Invalid active-state transition in round transaction journal: {details}")

    @classmethod
    def invalid_agent_state_transition(cls, details: str) -> Self:
        """Describe an invalid portable agent-state transition."""
        return cls(f"Invalid agent-state transition in round transaction journal: {details}")

    @classmethod
    def staged_index_changes(cls) -> Self:
        """Describe staged user changes that make the transaction unsafe."""
        return cls("Cannot begin round transaction while the Git index contains staged changes")

    @classmethod
    def invalid_transition(cls, details: str) -> Self:
        """Describe a transition that cannot be applied to agent state."""
        return cls(f"Invalid round transaction agent-state transition: {details}")

    @classmethod
    def invalid_round_payload(cls, source: str, details: str) -> Self:
        """Describe malformed completed-round data from a journal source."""
        return cls(f"Invalid completed-round payload in transaction journal {source}: {details}")

    @classmethod
    def round_payload_not_object(cls, source: str) -> Self:
        """Describe a completed-round JSON payload with the wrong root shape."""
        return cls(
            f"Invalid completed-round payload in transaction journal {source}: payload must be a JSON object"
        )


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
            # lint-waiver: LW-007065 [TRY003]; Pydantic validators need ValueError for structured field errors
            raise ValueError("run_id must not be empty")  # noqa: TRY003
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

    def state_transition(self, slot: StateSlot[BaseModel]) -> StateTransition:
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


class _LegacyActiveState(BaseModel):
    """Lossless compatibility model for removed v3 active checkpoints."""

    model_config = ConfigDict(extra="allow", frozen=True, strict=True)


@dataclass(frozen=True)
class CompletedRound:
    """Durable outputs produced by a successful round transaction."""

    checkpoint: str


class RoundTransaction:
    """A prepared round transition obtained from ``coordinator.begin``."""

    def __init__(self, coordinator: RoundTransactionCoordinator, round_number: int) -> None:
        """Bind this handle to one coordinator and round number."""
        self._coordinator = coordinator
        self.round_number = round_number
        self._closed = False

    def complete(self) -> CompletedRound:
        """Apply and commit the prepared state transition."""
        if self._closed:
            raise RoundTransactionError.already_completed(self.round_number)
        # lint-waiver: LW-007067 [SLF001]; the transaction handle uses its coordinator's private commit seam
        result = self._coordinator._complete(self.round_number)  # noqa: SLF001
        self._closed = True
        return result


class RoundTransactionCoordinator:
    """Coordinate crash-safe agent-state and candidate Git commits.

    ``begin(round_number, state_transition=...)`` durably journals an exact
    typed transition for portable ``agent/state.json``. ``complete()`` applies
    and commits that transition with the candidate worktree. ``recover()`` is
    idempotent and rolls any journaled transition forward.
    """

    def __init__(
        self,
        project: Project,
        git: GitTracker,
        run_id: str,
        *,
        agent_state_model_type: type[BaseModel],
    ) -> None:
        """Validate and bind the project, Git tracker, and run identity."""
        project_root = project.root.resolve()
        if git.root.resolve() != project_root:
            raise RoundTransactionError.different_project_root()
        if git.run_id != run_id:
            raise RoundTransactionError.run_id_mismatch(run_id, git.run_id)

        project.state.load_run(run_id)
        self._project = project
        self._git = git
        self.run_id = run_id
        self._agent_state_slot: StateSlot[BaseModel] = project.state.portable_namespace(
            run_id,
            "agent",
        ).slot("state.json", agent_state_model_type)
        self._legacy_active_slot: StateSlot[BaseModel] = project.state.local_namespace(
            run_id,
            "agent",
        ).slot("active.json", _LegacyActiveState)
        self._journal_slot = project.state.local_namespace(run_id, "transaction").slot(
            "round.json",
            _RoundJournal,
        )

    def begin(
        self,
        round_number: int,
        *,
        state_transition: StateTransition,
    ) -> RoundTransaction:
        """Durably prepare an exact agent-state transition."""
        if round_number < 1:
            raise RoundTransactionError.invalid_round_number(round_number)
        if self._load_optional_journal() is not None:
            raise RoundTransactionError.unfinished_transaction()

        pre_commit = self._git.current_sha()
        if pre_commit is None:
            raise RoundTransactionError.missing_head()
        self._require_clean_index()
        self._validate_state_transition(state_transition)

        transition_payload = self._agent_state_slot.serialize_transition(state_transition)
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
            raise RoundTransactionError.history_moved("recover", journal.pre_commit)
        self._commit_prepared(journal)
        self._clear_journal()
        return RoundRecoveryOutcome.COMMITTED

    def _complete(self, round_number: int) -> CompletedRound:
        journal = self._load_journal()
        if journal.round_number != round_number:
            raise RoundTransactionError.journal_round_mismatch(journal.round_number, round_number)
        if not self._pre_commit_is_ancestor(journal.pre_commit):
            raise RoundTransactionError.history_moved("complete", journal.pre_commit)

        completed = self._commit_prepared(journal)
        self._clear_journal()
        return completed

    def _commit_prepared(self, journal: _Journal) -> CompletedRound:
        if isinstance(journal, _V3RoundJournal):
            return self._commit_legacy_round(journal)
        return self._commit_agent_state(journal)

    def _commit_agent_state(self, journal: _V4RoundJournal) -> CompletedRound:
        transition = journal.state_transition(self._agent_state_slot)
        self._validate_state_transition(transition)
        snapshot = self._agent_state_slot.snapshot_transition(transition)
        status = self._git.framework_snapshot_status(snapshot)
        current_sha = self._git.current_sha()

        if status is FrameworkSnapshotStatus.EXACT:
            self._agent_state_slot.apply(transition)
        elif current_sha == journal.pre_commit:
            self._agent_state_slot.apply(transition)
            self._git.snapshot_with_framework_metadata(
                f"vibesys(round {journal.round_number}): record result",
                snapshot,
            )
        else:
            raise RoundTransactionError.agent_state_conflict()

        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError.agent_state_snapshot_not_exact()
        checkpoint = self._git.current_sha()
        if checkpoint is None:
            raise RoundTransactionError.inaccessible_head()
        return CompletedRound(checkpoint=checkpoint)

    def _commit_legacy_round(self, journal: _V3RoundJournal) -> CompletedRound:
        """Roll a v3 journal forward without importing its removed domain model."""
        round_payload = journal.round_payload()
        record = _parse_round_payload(round_payload, source="round transaction journal")
        if record.round_number != journal.round_number:
            raise RoundTransactionError.round_payload_number_mismatch(
                record.round_number, journal.round_number
            )
        expected_snapshot = self._project.state.prepare_completed_round_snapshot(
            self.run_id,
            record,
        )
        status = self._git.framework_snapshot_status(expected_snapshot)
        if status is FrameworkSnapshotStatus.DIFFERENT:
            raise RoundTransactionError.round_metadata_conflict()
        if status is FrameworkSnapshotStatus.EXACT:
            snapshot = self._project.state.restore_completed_round(self.run_id, record)
        else:
            snapshot = self._project.state.save_round(self.run_id, record)
            self._git.snapshot_with_framework_metadata(
                f"vibesys(round {journal.round_number}): record result",
                snapshot,
            )
        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError.completed_round_snapshot_not_exact()
        self._legacy_active_slot.apply(journal.active_transition(self._legacy_active_slot))
        checkpoint = self._git.current_sha()
        if checkpoint is None:
            raise RoundTransactionError.inaccessible_head()
        return CompletedRound(checkpoint=checkpoint)

    def _load_journal(self) -> _Journal:
        journal = self._load_optional_journal()
        if journal is None:
            raise RoundTransactionError.journal_missing()
        return journal

    def _load_optional_journal(self) -> _Journal | None:
        """Load and validate the WAL while preserving the coordinator error API."""
        try:
            envelope = self._journal_slot.load_optional()
        except ProjectStateError as exc:
            raise RoundTransactionError.invalid_journal(str(exc)) from exc
        if envelope is None:
            return None
        journal = envelope.root
        if journal.run_id != self.run_id:
            raise RoundTransactionError.journal_run_mismatch(journal.run_id, self.run_id)
        if isinstance(journal, _V3RoundJournal):
            self._validate_v3_journal(journal)
        else:
            self._validate_v4_journal(journal)
        return journal

    def _validate_v3_journal(self, journal: _V3RoundJournal) -> None:
        if _sha256(journal.round_payload()) != journal.round_payload_sha256:
            raise RoundTransactionError.payload_digest_mismatch("payload")
        _parse_round_payload(journal.round_payload(), source="round transaction journal")
        try:
            self._legacy_active_slot.validate_transition(
                journal.active_transition(self._legacy_active_slot)
            )
        except (TypeError, ValueError, ProjectStateError) as exc:
            raise RoundTransactionError.invalid_active_transition(str(exc)) from exc

    def _validate_v4_journal(self, journal: _V4RoundJournal) -> None:
        payload = journal.transition_payload()
        if _sha256(payload) != journal.state_transition_sha256:
            raise RoundTransactionError.payload_digest_mismatch("state-transition")
        try:
            self._validate_state_transition(journal.state_transition(self._agent_state_slot))
        except (TypeError, ValueError, ProjectStateError, RoundTransactionError) as exc:
            raise RoundTransactionError.invalid_agent_state_transition(str(exc)) from exc

    def _pre_commit_is_ancestor(self, pre_commit: str) -> bool:
        result = self._git.run(
            ["git", "merge-base", "--is-ancestor", pre_commit, "HEAD"],
            check=False,
        )
        return result.returncode == 0

    def _require_clean_index(self) -> None:
        result = self._git.run(["git", "diff", "--cached", "--quiet"], check=False)
        if result.returncode != 0:
            raise RoundTransactionError.staged_index_changes()

    def _validate_state_transition(self, transition: StateTransition) -> None:
        try:
            self._agent_state_slot.validate_transition(transition)
            self._agent_state_slot.snapshot_transition(transition)
        except ProjectStateError as exc:
            raise RoundTransactionError.invalid_transition(str(exc)) from exc

    def _clear_journal(self) -> None:
        self._journal_slot.save(None)


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        # lint-waiver: LW-007066 [TRY003]; invalid base64 must reach Pydantic as a ValueError
        raise ValueError("must contain canonical base64-encoded bytes") from exc  # noqa: TRY003


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _parse_round_payload(contents: bytes, *, source: str) -> RoundRecord:
    try:
        payload = json.loads(contents)
    except (TypeError, ValueError) as exc:
        raise RoundTransactionError.invalid_round_payload(source, str(exc)) from exc
    if not isinstance(payload, dict):
        raise RoundTransactionError.round_payload_not_object(source)
    try:
        return parse_round_record(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RoundTransactionError.invalid_round_payload(source, str(exc)) from exc
