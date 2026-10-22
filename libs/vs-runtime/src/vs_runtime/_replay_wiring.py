"""Compose native replay owners from the run's resources, without positional state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_proto.api import replay_agent, replay_evaluation, replay_workspace
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger
from vs_runtime._replay_evaluation import EvaluationReplayExecutor
from vs_runtime._replay_executors import ReplayExecutors
from vs_runtime._replay_operations import ReplayOperationError
from vs_runtime._replay_sessions import SessionReplayExecutor
from vs_runtime._replay_workspaces import WorkspaceReplayExecutor
from vs_runtime._session_lifecycle_requests import ReleasedRunInvocations
from vs_runtime._session_requests import JournalRunInvocations
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from protobuf import Message

    from vs_evaluation.api import RecoverableEvaluationExecutor
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._replay_operations import ReplayOperationHost
    from vs_runtime._replay_runtime import ReplayExecutor
    from vs_runtime._replay_sessions import ReplayAgentReply, ReplaySessionServices
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._workspaces import RuntimeWorkspaces
    from vs_runtime._writer_reconciliation import WriterRegistry


@dataclass(frozen=True)
class ReplayOwnerServices:
    """One host's native effect services and explicit owner execution bounds.

    Rebind these services on each host over the same durable journals and receipts.
    Explicit operator Resume sets ``allow_adoption=False``, preserving the existing
    conservative project-edit protection. Writer recovery may retain authority.
    """

    workspaces: RuntimeWorkspaces
    sessions: ReplaySessionServices
    evaluation: RecoverableEvaluationExecutor
    workspace_within_seconds: float
    evaluation_within_seconds: float
    allow_adoption: bool
    evaluation_control: RunControlChannel | None = None
    writer_registry: WriterRegistry | None = None


def replay_bindings(
    services: ReplayOwnerServices,
    receipts: ReceiptStore,
    host: ReplayOperationHost,
    replies: Sequence[ReplayAgentReply],
    *,
    operations: Mapping[type[Message], ReplayExecutor] | None = None,
) -> ReplayExecutors:
    """Build the existing lifecycle owners over one receipt and access authority.

    Registered operations may add native request classes but cannot replace these
    lifecycle owners. No CoreState, positional driver or strategy is constructed.
    """
    sessions = services.sessions
    workspace = RuntimeWorkspaceRequests(
        services.workspaces,
        receipts,
        ReleasedRunInvocations(
            JournalRunInvocations(sessions.sessions, receipts), sessions.sessions, receipts
        ),
        sessions.blocking,
        ReceiptEvidenceLedger(receipts),
    )
    if not services.allow_adoption:
        workspace.protect_adoption()
    owners: dict[type[Message], ReplayExecutor] = {
        replay_workspace.ReplayWorkspaceRequest: WorkspaceReplayExecutor(
            workspace,
            receipts,
            host,
            within_seconds=services.workspace_within_seconds,
            writers=services.writer_registry,
        ),
        replay_agent.ReplayAgentRequest: SessionReplayExecutor(sessions, receipts, host, replies),
        replay_evaluation.ReplayEvaluationRequest: EvaluationReplayExecutor(
            services.evaluation,
            receipts,
            host,
            within_seconds=services.evaluation_within_seconds,
            control=services.evaluation_control,
        ),
    }
    for request, owner in (operations or {}).items():
        if request in owners:
            problem = (
                f"registered operation cannot replace lifecycle owner: {request.desc().type_name}"
            )
            raise ReplayOperationError(problem)
        owners[request] = owner
    return ReplayExecutors(owners)
