"""Agent-owned v3 recovery and constructor compatibility for round transactions."""

# The legacy journal diagnostics identify exact failed payloads.
# ruff: noqa: TRY003

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from vibesys.run.agent_round_compat import LegacyAgentRoundStore
from vibesys.run.git_tracker import FrameworkSnapshotStatus
from vibesys.run.round_transaction import (
    CompletedRound,
    RoundTransactionCoordinator,
    RoundTransactionError,
    _V3RoundJournal,
)
from vs_loop_state.api import RoundRecord, parse_round_record
from vs_project.api import ProjectStateError, StateSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.git_tracker import GitTracker
    from vs_project.api import Project


class _LegacyActiveState(BaseModel):
    """Lossless compatibility model for removed v3 active checkpoints."""

    model_config = ConfigDict(extra="allow", frozen=True, strict=True)


class LegacyAgentRoundRecovery:
    """Validate and roll forward the agent policy's version 3 journal."""

    def __init__(self, project: Project, git: GitTracker, run_id: str) -> None:
        """Bind the old portable rounds and machine-local active state."""
        self._rounds = LegacyAgentRoundStore(project, run_id)
        self._active_slot: StateSlot[BaseModel] = project.state.local_namespace(
            run_id, "agent"
        ).slot("active.json", _LegacyActiveState)
        self._git = git

    def validate(self, journal: _V3RoundJournal) -> None:
        """Reject changed round bytes or malformed active-state transitions."""
        payload = journal.round_payload()
        if hashlib.sha256(payload).hexdigest() != journal.round_payload_sha256:
            raise RoundTransactionError("Round transaction journal payload digest does not match")
        _parse_round_payload(payload, source="round transaction journal")
        try:
            self._active_slot.validate_transition(journal.active_transition(self._active_slot))
        except (TypeError, ValueError, ProjectStateError) as exc:
            raise RoundTransactionError(
                f"Invalid active-state transition in round transaction journal: {exc}"
            ) from exc

    def commit(self, journal: _V3RoundJournal) -> CompletedRound:
        """Roll a validated v3 journal forward without a removed domain model."""
        record = _parse_round_payload(journal.round_payload(), source="round transaction journal")
        if record.round_number != journal.round_number:
            raise RoundTransactionError(
                f"Round transaction journal payload is for round {record.round_number}, "
                f"not round {journal.round_number}"
            )
        expected_snapshot = self._rounds.prepare_snapshot(record)
        status = self._git.framework_snapshot_status(expected_snapshot)
        if status is FrameworkSnapshotStatus.DIFFERENT:
            raise RoundTransactionError(
                "Committed round metadata differs from the transaction journal"
            )
        if status is FrameworkSnapshotStatus.EXACT:
            snapshot = self._rounds.restore(record)
        else:
            snapshot = self._rounds.save(record)
            self._git.snapshot_with_framework_metadata(
                f"vibesys(round {journal.round_number}): record result",
                snapshot,
            )
        if self._git.framework_snapshot_status(snapshot) is not FrameworkSnapshotStatus.EXACT:
            raise RoundTransactionError(
                "Git snapshot did not commit the exact completed-round metadata"
            )
        self._active_slot.apply(journal.active_transition(self._active_slot))
        checkpoint = self._git.current_sha()
        if checkpoint is None:
            raise RoundTransactionError("Git snapshot completed without an accessible HEAD")
        return CompletedRound(checkpoint=checkpoint)


class AgentRoundTransactionCoordinator(RoundTransactionCoordinator[BaseModel]):
    """Compatibility constructor for agent-owned state and v3 recovery."""

    def __init__(
        self,
        project: Project,
        git: GitTracker,
        run_id: str,
        *,
        agent_state_model_type: type[BaseModel],
    ) -> None:
        """Bind the canonical agent state slot and its legacy recovery."""
        state_slot: StateSlot[BaseModel] = project.state.portable_namespace(run_id, "agent").slot(
            "state.json", agent_state_model_type
        )
        super().__init__(
            project,
            git,
            run_id,
            state_slot=state_slot,
            legacy_recovery=LegacyAgentRoundRecovery(project, git, run_id),
        )


def agent_round_transaction_factory(
    agent_state_model_type: type[BaseModel],
) -> Callable[[Project, GitTracker, str], RoundTransactionCoordinator[BaseModel]]:
    """Bind agent state schema while leaving context construction policy-neutral."""

    def open_coordinator(
        project: Project, git: GitTracker, run_id: str
    ) -> RoundTransactionCoordinator[BaseModel]:
        return AgentRoundTransactionCoordinator(
            project, git, run_id, agent_state_model_type=agent_state_model_type
        )

    return open_coordinator


def _parse_round_payload(contents: bytes, *, source: str) -> RoundRecord:
    try:
        payload = json.loads(contents)
    except (TypeError, ValueError) as exc:
        raise RoundTransactionError(
            f"Invalid completed-round payload in transaction journal {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RoundTransactionError(
            f"Invalid completed-round payload in transaction journal {source}: "
            "payload must be a JSON object"
        )
    try:
        return parse_round_record(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RoundTransactionError(
            f"Invalid completed-round payload in transaction journal {source}: {exc}"
        ) from exc
