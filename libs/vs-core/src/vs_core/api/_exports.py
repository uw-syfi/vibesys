"""Statically declared deferred imports for the pure public facade."""

# ruff: noqa: PLC0415
# lint-waiver: LW-602176 [PLC0415]; these facade loaders defer statically declared pure imports.
# > Eager imports load the entire lifecycle for a control admission; importlib would violate
# > core purity. Deferred static imports preserve both boundaries without a dynamic importer.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _load_attempt_track() -> dict[str, object]:
    from vs_core._attempt_track import AttemptTrack, Step

    return {"AttemptTrack": AttemptTrack, "Step": Step}


def _load_comparison() -> dict[str, object]:
    from vs_core._comparison import (
        BASE_MAX_SAMPLES,
        MAX_CORE_BASE_RUNS,
        MIN_SAMPLES_WITHOUT_FLOOR,
        VERDICT_NOISES,
        ComparisonPolicy,
        Measured,
        Moment,
        close_call,
        compare,
        comparison_of,
        comparison_settling,
        comparisons_of,
        effect_resolution,
        effect_verdict,
        evaluation_of,
        latest_comparisons,
        moment_of,
        policy_of,
        samples_of,
        terms_policy,
        verdict_unreachable,
    )

    return {
        "BASE_MAX_SAMPLES": BASE_MAX_SAMPLES,
        "ComparisonPolicy": ComparisonPolicy,
        "MAX_CORE_BASE_RUNS": MAX_CORE_BASE_RUNS,
        "MIN_SAMPLES_WITHOUT_FLOOR": MIN_SAMPLES_WITHOUT_FLOOR,
        "Measured": Measured,
        "Moment": Moment,
        "VERDICT_NOISES": VERDICT_NOISES,
        "close_call": close_call,
        "compare": compare,
        "comparison_of": comparison_of,
        "comparison_settling": comparison_settling,
        "comparisons_of": comparisons_of,
        "effect_resolution": effect_resolution,
        "effect_verdict": effect_verdict,
        "evaluation_of": evaluation_of,
        "latest_comparisons": latest_comparisons,
        "moment_of": moment_of,
        "policy_of": policy_of,
        "samples_of": samples_of,
        "terms_policy": terms_policy,
        "verdict_unreachable": verdict_unreachable,
    }


def _load_continuations() -> dict[str, object]:
    from vs_core._continuations import SuspensionRefusal

    return {"SuspensionRefusal": SuspensionRefusal}


def _load_corevalue() -> dict[str, object]:
    from vs_core._corevalue import CoreValue, FrozenMap, TimestampValue

    return {"CoreValue": CoreValue, "FrozenMap": FrozenMap, "TimestampValue": TimestampValue}


def _load_envelope_migrations() -> dict[str, object]:
    from vs_core._envelope_migrations import MIGRATIONS, OLDEST_SUPPORTED_ENVELOPE_VERSION

    return {
        "MIGRATIONS": MIGRATIONS,
        "OLDEST_SUPPORTED_ENVELOPE_VERSION": OLDEST_SUPPORTED_ENVELOPE_VERSION,
    }


def _load_evidence_views() -> dict[str, object]:
    from vs_core._evidence_views import (
        TurnCandidate,
        TurnCandidateFailed,
        TurnCandidatePending,
        TurnCandidateRetained,
        ledger_refs,
        trusted_keys,
        turn_candidate,
    )

    return {
        "TurnCandidate": TurnCandidate,
        "TurnCandidateFailed": TurnCandidateFailed,
        "TurnCandidatePending": TurnCandidatePending,
        "TurnCandidateRetained": TurnCandidateRetained,
        "ledger_refs": ledger_refs,
        "trusted_keys": trusted_keys,
        "turn_candidate": turn_candidate,
    }


def _load_host_epochs() -> dict[str, object]:
    from vs_core._host_epochs import downtime

    return {"downtime": downtime}


def _load_ids() -> dict[str, object]:
    from vs_core._ids import (
        IdScheme,
        attempt_id,
        attempt_ref,
        component,
        decision_of_operation,
        item_id,
    )

    return {
        "IdScheme": IdScheme,
        "attempt_id": attempt_id,
        "attempt_ref": attempt_ref,
        "component": component,
        "decision_of_operation": decision_of_operation,
        "item_id": item_id,
    }


def _load_ledger_compaction() -> dict[str, object]:
    from vs_core._ledger_compaction import compact_ledger

    return {"compact_ledger": compact_ledger}


def _load_measurement_recipe() -> dict[str, object]:
    from vs_core._measurement_recipe import MeasurementPurpose, MeasurementRecipe, StageBudget

    return {
        "MeasurementPurpose": MeasurementPurpose,
        "MeasurementRecipe": MeasurementRecipe,
        "StageBudget": StageBudget,
    }


def _load_operator_actions() -> dict[str, object]:
    from vs_core._operator_actions import (
        ItemCancellation,
        cancelled_items,
        item_cancellation,
        notice_input_id,
        parallel_limit,
    )

    return {
        "ItemCancellation": ItemCancellation,
        "cancelled_items": cancelled_items,
        "item_cancellation": item_cancellation,
        "notice_input_id": notice_input_id,
        "parallel_limit": parallel_limit,
    }


def _load_ownership() -> dict[str, object]:
    from vs_core._ownership import unreleased_records

    return {"unreleased_records": unreleased_records}


def _load_projection() -> dict[str, object]:
    from vs_core._projection import (
        attempt_view,
        evidence_view,
        pending_requests,
        project,
        scheduling_view,
        session_view,
        settlement_view,
    )

    return {
        "attempt_view": attempt_view,
        "evidence_view": evidence_view,
        "pending_requests": pending_requests,
        "project": project,
        "scheduling_view": scheduling_view,
        "session_view": session_view,
        "settlement_view": settlement_view,
    }


def _load_proofs() -> dict[str, object]:
    from vs_core._proofs import requested_stop_mode, stop_mode

    return {"requested_stop_mode": requested_stop_mode, "stop_mode": stop_mode}


def _load_registry() -> dict[str, object]:
    from vs_core._registry import (
        ENVELOPE_SCHEMA_VERSION,
        ClosedRecordContradictionError,
        ContractError,
        OperationMigration,
        OperationRegistration,
        OperationRegistry,
        UnexpectedEvent,
        decode_stored,
        operation_result,
        upgrade_envelope,
        validate_startup,
    )

    return {
        "ClosedRecordContradictionError": ClosedRecordContradictionError,
        "ContractError": ContractError,
        "ENVELOPE_SCHEMA_VERSION": ENVELOPE_SCHEMA_VERSION,
        "OperationMigration": OperationMigration,
        "OperationRegistration": OperationRegistration,
        "OperationRegistry": OperationRegistry,
        "UnexpectedEvent": UnexpectedEvent,
        "decode_stored": decode_stored,
        "operation_result": operation_result,
        "upgrade_envelope": upgrade_envelope,
        "validate_startup": validate_startup,
    }


def _load_turn_permit() -> dict[str, object]:
    from vs_core._turn_permit import TurnPermit, admitted_work_remains, permit_for_request

    return {
        "TurnPermit": TurnPermit,
        "admitted_work_remains": admitted_work_remains,
        "permit_for_request": permit_for_request,
    }


def _load_run_pause() -> dict[str, object]:
    from vs_core._run_pause import (
        DEADLINE_STOP,
        ENVIRONMENT_STOP,
        ControlRefusedError,
        capacity_wait_stands,
        control_refusal,
        pause_after,
        reopened,
        run_pause,
        stands_after,
    )

    return {
        "ControlRefusedError": ControlRefusedError,
        "DEADLINE_STOP": DEADLINE_STOP,
        "ENVIRONMENT_STOP": ENVIRONMENT_STOP,
        "capacity_wait_stands": capacity_wait_stands,
        "control_refusal": control_refusal,
        "pause_after": pause_after,
        "reopened": reopened,
        "run_pause": run_pause,
        "stands_after": stands_after,
    }


def _load_search_events() -> dict[str, object]:
    from vs_core._search_events import Translated, run_view, search_events, translate

    return {
        "Translated": Translated,
        "run_view": run_view,
        "search_events": search_events,
        "translate": translate,
    }


def _load_search_run() -> dict[str, object]:
    from vs_core._search_run import (
        Advanced,
        CoreStep,
        Inbox,
        SearchDriver,
        SearchHost,
        advance,
        begin,
        check_decisions,
        deferred,
    )

    return {
        "Advanced": Advanced,
        "CoreStep": CoreStep,
        "Inbox": Inbox,
        "SearchDriver": SearchDriver,
        "SearchHost": SearchHost,
        "advance": advance,
        "begin": begin,
        "check_decisions": check_decisions,
        "deferred": deferred,
    }


def _load_session_scope() -> dict[str, object]:
    from vs_core._session_scope import write_turn_authority

    return {"write_turn_authority": write_turn_authority}


def _load_step() -> dict[str, object]:
    from vs_core._step import (
        CoreReducers,
        mid_turn_offers,
        step,
        suspension_refusal,
        validate_terminal_inputs,
    )

    return {
        "CoreReducers": CoreReducers,
        "mid_turn_offers": mid_turn_offers,
        "step": step,
        "suspension_refusal": suspension_refusal,
        "validate_terminal_inputs": validate_terminal_inputs,
    }


def _load_strategy_operations() -> dict[str, object]:
    from vs_core._strategy_operations import (
        CAPTURE_SUFFIX,
        EXPORT_PATCH_SUFFIX,
        INTERPRET_SUFFIX,
        RENDER_SUFFIX,
        RETAIN_SUFFIX,
        VALIDATE_LOCAL_SUFFIX,
        VERIFY_SUFFIX,
        CaptureCommandBase,
        CommandCapture,
        EvidenceReadingsBase,
        ExportPatchBase,
        InterpretEvidenceBase,
        LocalValidationReport,
        OperationKindError,
        ParentVerification,
        PatchExport,
        RenderedArtifacts,
        RenderRoleArtifactsBase,
        RetainVerifiedRevisionBase,
        RetentionReceipt,
        RolePromptContext,
        StrategyOperations,
        ValidateLocalBase,
        VerifyParentRevisionBase,
    )

    return {
        "CAPTURE_SUFFIX": CAPTURE_SUFFIX,
        "CaptureCommandBase": CaptureCommandBase,
        "CommandCapture": CommandCapture,
        "EXPORT_PATCH_SUFFIX": EXPORT_PATCH_SUFFIX,
        "EvidenceReadingsBase": EvidenceReadingsBase,
        "ExportPatchBase": ExportPatchBase,
        "INTERPRET_SUFFIX": INTERPRET_SUFFIX,
        "InterpretEvidenceBase": InterpretEvidenceBase,
        "LocalValidationReport": LocalValidationReport,
        "OperationKindError": OperationKindError,
        "ParentVerification": ParentVerification,
        "PatchExport": PatchExport,
        "RENDER_SUFFIX": RENDER_SUFFIX,
        "RETAIN_SUFFIX": RETAIN_SUFFIX,
        "RenderRoleArtifactsBase": RenderRoleArtifactsBase,
        "RenderedArtifacts": RenderedArtifacts,
        "RetainVerifiedRevisionBase": RetainVerifiedRevisionBase,
        "RetentionReceipt": RetentionReceipt,
        "RolePromptContext": RolePromptContext,
        "StrategyOperations": StrategyOperations,
        "VALIDATE_LOCAL_SUFFIX": VALIDATE_LOCAL_SUFFIX,
        "VERIFY_SUFFIX": VERIFY_SUFFIX,
        "ValidateLocalBase": ValidateLocalBase,
        "VerifyParentRevisionBase": VerifyParentRevisionBase,
    }


def _load_turn_chain() -> dict[str, object]:
    from vs_core._turn_chain import TurnChain

    return {"TurnChain": TurnChain}


def _load_turn_holds() -> dict[str, object]:
    from vs_core._turn_holds import held_limit

    return {"held_limit": held_limit}


def _load_values() -> dict[str, object]:
    from vs_core._values import EncodingMismatchError, encode_json, share_unchanged, verify_encoding

    return {
        "EncodingMismatchError": EncodingMismatchError,
        "encode_json": encode_json,
        "share_unchanged": share_unchanged,
        "verify_encoding": verify_encoding,
    }


def _load_waitable() -> dict[str, object]:
    from vs_core._waitable import (
        SuspendedTurn,
        Waitable,
        WaitableReply,
        WaitingForDependency,
        WaitingForEvaluation,
        turn_reply,
    )

    return {
        "SuspendedTurn": SuspendedTurn,
        "Waitable": Waitable,
        "WaitableReply": WaitableReply,
        "WaitingForDependency": WaitingForDependency,
        "WaitingForEvaluation": WaitingForEvaluation,
        "turn_reply": turn_reply,
    }


def _load_waits() -> dict[str, object]:
    from vs_core._waits import (
        MAY_WAIT_ON,
        RESOLUTIONS,
        OpenWait,
        Outcome,
        Producer,
        Resolution,
        WaitingPhase,
        WaitKind,
        WaitSubject,
        orphan_waits,
        owned_job_subject,
        phase_waits,
        recorded_waits,
        subjects,
        waits,
    )

    return {
        "MAY_WAIT_ON": MAY_WAIT_ON,
        "OpenWait": OpenWait,
        "Outcome": Outcome,
        "Producer": Producer,
        "RESOLUTIONS": RESOLUTIONS,
        "Resolution": Resolution,
        "WaitKind": WaitKind,
        "WaitSubject": WaitSubject,
        "WaitingPhase": WaitingPhase,
        "orphan_waits": orphan_waits,
        "owned_job_subject": owned_job_subject,
        "phase_waits": phase_waits,
        "recorded_waits": recorded_waits,
        "subjects": subjects,
        "waits": waits,
    }


def _load_api_routing() -> dict[str, object]:
    from vs_core.api.routing import EVENT_ROUTES

    return {"EVENT_ROUTES": EVENT_ROUTES}


def _load_attempts() -> dict[str, object]:
    from vs_core.attempts import advance_attempt

    return {"advance_attempt": advance_attempt}


def _load_evaluation() -> dict[str, object]:
    from vs_core.evaluation import advance_evaluation

    return {"advance_evaluation": advance_evaluation}


def _load_flows() -> dict[str, object]:
    from vs_core.flows import (
        ADOPTED,
        INPUT,
        MARKDOWN_PROSE_CONVENTION,
        MAX_CORRECTIONS,
        MAX_LOST_TURNS,
        MAX_RENDER_SECONDS,
        MAX_TURN_SECONDS,
        NOT_ADOPTED,
        PLAIN_LINE_CONVENTION,
        Again,
        Assessment,
        AttemptOutcome,
        CaptureSpec,
        Channel,
        ChannelKey,
        Compared,
        Deadlock,
        Dropped,
        EndReason,
        FailureKind,
        Flow,
        FlowCodeChanged,
        FlowDriver,
        FlowState,
        GaveUp,
        Group,
        Input,
        Kept,
        Label,
        LabelFields,
        Latch,
        LatchKey,
        NotOpened,
        NotReopened,
        NoWinner,
        Parking,
        Pending,
        RestartSettled,
        RestartSettleReason,
        RetryPolicy,
        RevisionReply,
        Role,
        RunTask,
        RunTrace,
        SearchDef,
        Session,
        Source,
        Span,
        SpanAttempt,
        SpanChange,
        SpanFailure,
        SpanId,
        SpanKind,
        SpanLabel,
        SpanLink,
        SpanStatus,
        SpanStop,
        SpanType,
        SpanWaitKind,
        Stopped,
        StopSource,
        Task,
        TaskStopped,
        Timer,
        TimeUp,
        Tries,
        Uncompared,
        UnknownRoleError,
        Waiting,
        WaitingOn,
        WireSpan,
        Workspace,
        flow,
        hours,
        markdown_prose,
        minutes,
        operation_span,
        plain_line,
        reply_errors,
        resource,
        span_changes,
    )

    return {
        "ADOPTED": ADOPTED,
        "Again": Again,
        "Assessment": Assessment,
        "AttemptOutcome": AttemptOutcome,
        "CaptureSpec": CaptureSpec,
        "Channel": Channel,
        "ChannelKey": ChannelKey,
        "Compared": Compared,
        "Deadlock": Deadlock,
        "Dropped": Dropped,
        "EndReason": EndReason,
        "FailureKind": FailureKind,
        "Flow": Flow,
        "FlowCodeChanged": FlowCodeChanged,
        "FlowDriver": FlowDriver,
        "FlowState": FlowState,
        "GaveUp": GaveUp,
        "Group": Group,
        "INPUT": INPUT,
        "Input": Input,
        "Kept": Kept,
        "Label": Label,
        "LabelFields": LabelFields,
        "Latch": Latch,
        "LatchKey": LatchKey,
        "MARKDOWN_PROSE_CONVENTION": MARKDOWN_PROSE_CONVENTION,
        "MAX_CORRECTIONS": MAX_CORRECTIONS,
        "MAX_LOST_TURNS": MAX_LOST_TURNS,
        "MAX_RENDER_SECONDS": MAX_RENDER_SECONDS,
        "MAX_TURN_SECONDS": MAX_TURN_SECONDS,
        "NOT_ADOPTED": NOT_ADOPTED,
        "NoWinner": NoWinner,
        "NotOpened": NotOpened,
        "NotReopened": NotReopened,
        "PLAIN_LINE_CONVENTION": PLAIN_LINE_CONVENTION,
        "Parking": Parking,
        "Pending": Pending,
        "RestartSettleReason": RestartSettleReason,
        "RestartSettled": RestartSettled,
        "RetryPolicy": RetryPolicy,
        "RevisionReply": RevisionReply,
        "Role": Role,
        "RunTask": RunTask,
        "RunTrace": RunTrace,
        "SearchDef": SearchDef,
        "Session": Session,
        "Source": Source,
        "Span": Span,
        "SpanAttempt": SpanAttempt,
        "SpanChange": SpanChange,
        "SpanFailure": SpanFailure,
        "SpanId": SpanId,
        "SpanKind": SpanKind,
        "SpanLabel": SpanLabel,
        "SpanLink": SpanLink,
        "SpanStatus": SpanStatus,
        "SpanStop": SpanStop,
        "SpanType": SpanType,
        "SpanWaitKind": SpanWaitKind,
        "StopSource": StopSource,
        "Stopped": Stopped,
        "Task": Task,
        "TaskStopped": TaskStopped,
        "TimeUp": TimeUp,
        "Timer": Timer,
        "Tries": Tries,
        "Uncompared": Uncompared,
        "UnknownRoleError": UnknownRoleError,
        "Waiting": Waiting,
        "WaitingOn": WaitingOn,
        "WireSpan": WireSpan,
        "Workspace": Workspace,
        "flow": flow,
        "hours": hours,
        "markdown_prose": markdown_prose,
        "minutes": minutes,
        "operation_span": operation_span,
        "plain_line": plain_line,
        "reply_errors": reply_errors,
        "resource": resource,
        "span_changes": span_changes,
    }


def _load_records() -> dict[str, object]:
    from vs_core.records import advance_record

    return {"advance_record": advance_record}


def _load_scheduling() -> dict[str, object]:
    from vs_core.scheduling import schedule

    return {"schedule": schedule}


def _load_sessions() -> dict[str, object]:
    from vs_core.sessions import advance_session

    return {"advance_session": advance_session}


def _load_settlement() -> dict[str, object]:
    from vs_core.settlement import advance_adoption, settle

    return {"advance_adoption": advance_adoption, "settle": settle}


def _load_testing_builders() -> dict[str, object]:
    from vs_core.testing.builders import initial_state

    return {"initial_state": initial_state}


def _load_types_agents() -> dict[str, object]:
    from vs_core.types.agents import (
        Backoff,
        Charge,
        ChargeClass,
        Checkpoint,
        Correctable,
        Correction,
        Declined,
        Exchanges,
        ProtocolContext,
        ReplyCorrection,
        ResumeFacts,
        Retained,
        WaitUnrecorded,
    )

    return {
        "Backoff": Backoff,
        "Charge": Charge,
        "ChargeClass": ChargeClass,
        "Checkpoint": Checkpoint,
        "Correctable": Correctable,
        "Correction": Correction,
        "Declined": Declined,
        "Exchanges": Exchanges,
        "ProtocolContext": ProtocolContext,
        "ReplyCorrection": ReplyCorrection,
        "ResumeFacts": ResumeFacts,
        "Retained": Retained,
        "WaitUnrecorded": WaitUnrecorded,
    }


def _load_types_attempts() -> dict[str, object]:
    from vs_core.types.attempts import (
        AttemptAdmitted,
        AttemptBudget,
        AttemptChargeRefundRequested,
        AttemptCheckpoint,
        AttemptClosure,
        AttemptEvaluationExhausted,
        AttemptEvaluationHistoryUpdated,
        AttemptExhausted,
        AttemptPhase,
        AttemptReacquireRequested,
        AttemptRegistered,
        AttemptSetupFailed,
        AttemptsEvent,
        AttemptsState,
        AttemptView,
        CheckpointDecline,
        CheckpointDeclined,
        CheckpointResolved,
        CloseAttemptScope,
        DiscardWorkspace,
        EnsureWorkspace,
        InitialSessionsFailed,
        InitialSessionsReady,
        InvocationChargeRequested,
        InvocationCheckpointed,
        InvocationCheckpointRequested,
        InvocationEnded,
        ReacquisitionReady,
        ReleaseDependencyBlocked,
        ReleaseDependencyObserved,
        RestoreRevision,
        RetainRevision,
        RetentionRequired,
        RetireRequested,
        RevisionOperationObserved,
        RevisionOperationRequested,
        ScopeAdmissionReopened,
        ScopeReopenAdmitted,
        ScopeReopenRequested,
        SnapshotAndRetain,
        WorkspaceObserved,
        WorkspacePlan,
        WorkspaceRequest,
    )

    return {
        "AttemptAdmitted": AttemptAdmitted,
        "AttemptBudget": AttemptBudget,
        "AttemptChargeRefundRequested": AttemptChargeRefundRequested,
        "AttemptCheckpoint": AttemptCheckpoint,
        "AttemptClosure": AttemptClosure,
        "AttemptEvaluationExhausted": AttemptEvaluationExhausted,
        "AttemptEvaluationHistoryUpdated": AttemptEvaluationHistoryUpdated,
        "AttemptExhausted": AttemptExhausted,
        "AttemptPhase": AttemptPhase,
        "AttemptReacquireRequested": AttemptReacquireRequested,
        "AttemptRegistered": AttemptRegistered,
        "AttemptSetupFailed": AttemptSetupFailed,
        "AttemptView": AttemptView,
        "AttemptsEvent": AttemptsEvent,
        "AttemptsState": AttemptsState,
        "CheckpointDecline": CheckpointDecline,
        "CheckpointDeclined": CheckpointDeclined,
        "CheckpointResolved": CheckpointResolved,
        "CloseAttemptScope": CloseAttemptScope,
        "DiscardWorkspace": DiscardWorkspace,
        "EnsureWorkspace": EnsureWorkspace,
        "InitialSessionsFailed": InitialSessionsFailed,
        "InitialSessionsReady": InitialSessionsReady,
        "InvocationChargeRequested": InvocationChargeRequested,
        "InvocationCheckpointRequested": InvocationCheckpointRequested,
        "InvocationCheckpointed": InvocationCheckpointed,
        "InvocationEnded": InvocationEnded,
        "ReacquisitionReady": ReacquisitionReady,
        "ReleaseDependencyBlocked": ReleaseDependencyBlocked,
        "ReleaseDependencyObserved": ReleaseDependencyObserved,
        "RestoreRevision": RestoreRevision,
        "RetainRevision": RetainRevision,
        "RetentionRequired": RetentionRequired,
        "RetireRequested": RetireRequested,
        "RevisionOperationObserved": RevisionOperationObserved,
        "RevisionOperationRequested": RevisionOperationRequested,
        "ScopeAdmissionReopened": ScopeAdmissionReopened,
        "ScopeReopenAdmitted": ScopeReopenAdmitted,
        "ScopeReopenRequested": ScopeReopenRequested,
        "SnapshotAndRetain": SnapshotAndRetain,
        "WorkspaceObserved": WorkspaceObserved,
        "WorkspacePlan": WorkspacePlan,
        "WorkspaceRequest": WorkspaceRequest,
    }


def _load_types_common() -> dict[str, object]:
    from vs_core.types.common import (
        ACTION_REFUSAL_TEXT,
        IDENTITY_PATTERN,
        MAX_REASK_CHAIN,
        MAX_REQUEST_RETRIES,
        MAX_SUBMISSIONS_PER_MEASUREMENT,
        OPERATOR_ACTIONS,
        THIS_RUN,
        ActionRefusal,
        Area,
        ArtifactId,
        ArtifactRef,
        AssessmentKind,
        AttemptId,
        AttemptRef,
        Capabilities,
        ChargeId,
        ChargeKind,
        ChargeReceipt,
        ChildManifest,
        CompletionStatus,
        ContinuationId,
        ContractValidationError,
        ControlAction,
        ControlId,
        ControlInput,
        Count,
        DecisionId,
        DependencyRef,
        DependencyStatus,
        DigestScheme,
        EventCursor,
        EventId,
        EvidenceId,
        EvidenceKey,
        EvidenceKind,
        Generation,
        HostFence,
        HostId,
        Identity,
        IdentityTypeError,
        InputId,
        InputTarget,
        InvocationId,
        InvocationInputTarget,
        InvocationRef,
        ItemId,
        ItemInputTarget,
        KernelNotImplementedError,
        LifecycleCapability,
        LifecycleClass,
        Limits,
        Observation,
        ObservationStatus,
        OperationDescriptor,
        OperationId,
        OperationNormalizationKind,
        OperationRef,
        OperationSchemaRef,
        OperationWire,
        PauseOwner,
        PoolCapacity,
        PoolId,
        PureOption,
        RejectionCode,
        ReleaseDependency,
        RequestBase,
        RequestId,
        RequestRetries,
        ReservedInputOccurrence,
        ResourceId,
        ResumeWhen,
        RevisionAuthority,
        RevisionId,
        RevisionNumber,
        RevisionRef,
        RoleId,
        RoleInputTarget,
        RunFacts,
        RunId,
        RunPark,
        RunPause,
        RunStatus,
        SchemaRef,
        Scope,
        ScopeInputTarget,
        ScopeReopenNormalization,
        Seconds,
        SessionId,
        SettlementId,
        SetupFailureKind,
        SignalCycleError,
        StopMode,
        StrategyId,
        TaskObjective,
        TaskStage,
        TaskTerms,
        Value,
        WorkspaceMode,
        WorkspaceRef,
        escalate_stop,
    )

    return {
        "ACTION_REFUSAL_TEXT": ACTION_REFUSAL_TEXT,
        "ActionRefusal": ActionRefusal,
        "Area": Area,
        "ArtifactId": ArtifactId,
        "ArtifactRef": ArtifactRef,
        "AssessmentKind": AssessmentKind,
        "AttemptId": AttemptId,
        "AttemptRef": AttemptRef,
        "Capabilities": Capabilities,
        "ChargeId": ChargeId,
        "ChargeKind": ChargeKind,
        "ChargeReceipt": ChargeReceipt,
        "ChildManifest": ChildManifest,
        "CompletionStatus": CompletionStatus,
        "ContinuationId": ContinuationId,
        "ContractValidationError": ContractValidationError,
        "ControlAction": ControlAction,
        "ControlId": ControlId,
        "ControlInput": ControlInput,
        "Count": Count,
        "DecisionId": DecisionId,
        "DependencyRef": DependencyRef,
        "DependencyStatus": DependencyStatus,
        "DigestScheme": DigestScheme,
        "EventCursor": EventCursor,
        "EventId": EventId,
        "EvidenceId": EvidenceId,
        "EvidenceKey": EvidenceKey,
        "EvidenceKind": EvidenceKind,
        "Generation": Generation,
        "HostFence": HostFence,
        "HostId": HostId,
        "IDENTITY_PATTERN": IDENTITY_PATTERN,
        "Identity": Identity,
        "IdentityTypeError": IdentityTypeError,
        "InputId": InputId,
        "InputTarget": InputTarget,
        "InvocationId": InvocationId,
        "InvocationInputTarget": InvocationInputTarget,
        "InvocationRef": InvocationRef,
        "ItemId": ItemId,
        "ItemInputTarget": ItemInputTarget,
        "KernelNotImplementedError": KernelNotImplementedError,
        "LifecycleCapability": LifecycleCapability,
        "LifecycleClass": LifecycleClass,
        "Limits": Limits,
        "MAX_REASK_CHAIN": MAX_REASK_CHAIN,
        "MAX_REQUEST_RETRIES": MAX_REQUEST_RETRIES,
        "MAX_SUBMISSIONS_PER_MEASUREMENT": MAX_SUBMISSIONS_PER_MEASUREMENT,
        "OPERATOR_ACTIONS": OPERATOR_ACTIONS,
        "Observation": Observation,
        "ObservationStatus": ObservationStatus,
        "OperationDescriptor": OperationDescriptor,
        "OperationId": OperationId,
        "OperationNormalizationKind": OperationNormalizationKind,
        "OperationRef": OperationRef,
        "OperationSchemaRef": OperationSchemaRef,
        "OperationWire": OperationWire,
        "PauseOwner": PauseOwner,
        "PoolCapacity": PoolCapacity,
        "PoolId": PoolId,
        "PureOption": PureOption,
        "RejectionCode": RejectionCode,
        "ReleaseDependency": ReleaseDependency,
        "RequestBase": RequestBase,
        "RequestId": RequestId,
        "RequestRetries": RequestRetries,
        "ReservedInputOccurrence": ReservedInputOccurrence,
        "ResourceId": ResourceId,
        "ResumeWhen": ResumeWhen,
        "RevisionAuthority": RevisionAuthority,
        "RevisionId": RevisionId,
        "RevisionNumber": RevisionNumber,
        "RevisionRef": RevisionRef,
        "RoleId": RoleId,
        "RoleInputTarget": RoleInputTarget,
        "RunFacts": RunFacts,
        "RunId": RunId,
        "RunPark": RunPark,
        "RunPause": RunPause,
        "RunStatus": RunStatus,
        "SchemaRef": SchemaRef,
        "Scope": Scope,
        "ScopeInputTarget": ScopeInputTarget,
        "ScopeReopenNormalization": ScopeReopenNormalization,
        "Seconds": Seconds,
        "SessionId": SessionId,
        "SettlementId": SettlementId,
        "SetupFailureKind": SetupFailureKind,
        "SignalCycleError": SignalCycleError,
        "StopMode": StopMode,
        "StrategyId": StrategyId,
        "THIS_RUN": THIS_RUN,
        "TaskObjective": TaskObjective,
        "TaskStage": TaskStage,
        "TaskTerms": TaskTerms,
        "Value": Value,
        "WorkspaceMode": WorkspaceMode,
        "WorkspaceRef": WorkspaceRef,
        "escalate_stop": escalate_stop,
    }


def _load_types_evaluation() -> dict[str, object]:
    from vs_core.types.evaluation import (
        MEASUREMENT_DIAGNOSTIC_LIMIT,
        AgentCall,
        AgentMeasurementRequested,
        AgentRejection,
        CancelOwnedJob,
        CollectEvidence,
        Continuation,
        ContinuationJobsChanged,
        ContinuationPhase,
        ContinuationReopenRequested,
        ContinuationRetireRequested,
        ContinuationScopeReopened,
        DeadlineReached,
        EvaluationEvent,
        EvaluationRequest,
        EvaluationState,
        EvidenceAcceptanceReceipt,
        EvidenceRef,
        HistoricalSubmissionReceipt,
        InspectOwnedJob,
        JobFactsBeforeObservation,
        JobObserved,
        JobQueued,
        JobsDrainRequested,
        JobTerminationRequested,
        JobWaitLapsed,
        MeasurementIdentity,
        MeasurementPlan,
        MeasurementPlanError,
        MeasurementRequested,
        MeasurementResult,
        MeasurementStage,
        MeasurementStageIdentity,
        MeasurementSubmissionEnded,
        MeasurementSubmissionObserved,
        ObservationsDue,
        ObservedJobFacts,
        ObserveOwnedJob,
        ObservePacing,
        OwnedJob,
        PreparedSubmissionReceipt,
        RegisteredJobObserved,
        RegisteredJobRequested,
        RegisteredOwnedJob,
        ResumeAuthorizationReceipt,
        ResumeAuthorized,
        ResumeRefusal,
        ResumeRefused,
        SnapshotResultRef,
        SubmissionBudget,
        SubmissionReceipt,
        SubmitMeasurement,
        TurnSuspended,
        UnobservedJobFacts,
    )

    return {
        "AgentCall": AgentCall,
        "AgentMeasurementRequested": AgentMeasurementRequested,
        "AgentRejection": AgentRejection,
        "CancelOwnedJob": CancelOwnedJob,
        "CollectEvidence": CollectEvidence,
        "Continuation": Continuation,
        "ContinuationJobsChanged": ContinuationJobsChanged,
        "ContinuationPhase": ContinuationPhase,
        "ContinuationReopenRequested": ContinuationReopenRequested,
        "ContinuationRetireRequested": ContinuationRetireRequested,
        "ContinuationScopeReopened": ContinuationScopeReopened,
        "DeadlineReached": DeadlineReached,
        "EvaluationEvent": EvaluationEvent,
        "EvaluationRequest": EvaluationRequest,
        "EvaluationState": EvaluationState,
        "EvidenceAcceptanceReceipt": EvidenceAcceptanceReceipt,
        "EvidenceRef": EvidenceRef,
        "HistoricalSubmissionReceipt": HistoricalSubmissionReceipt,
        "InspectOwnedJob": InspectOwnedJob,
        "JobFactsBeforeObservation": JobFactsBeforeObservation,
        "JobObserved": JobObserved,
        "JobQueued": JobQueued,
        "JobTerminationRequested": JobTerminationRequested,
        "JobWaitLapsed": JobWaitLapsed,
        "JobsDrainRequested": JobsDrainRequested,
        "MEASUREMENT_DIAGNOSTIC_LIMIT": MEASUREMENT_DIAGNOSTIC_LIMIT,
        "MeasurementIdentity": MeasurementIdentity,
        "MeasurementPlan": MeasurementPlan,
        "MeasurementPlanError": MeasurementPlanError,
        "MeasurementRequested": MeasurementRequested,
        "MeasurementResult": MeasurementResult,
        "MeasurementStage": MeasurementStage,
        "MeasurementStageIdentity": MeasurementStageIdentity,
        "MeasurementSubmissionEnded": MeasurementSubmissionEnded,
        "MeasurementSubmissionObserved": MeasurementSubmissionObserved,
        "ObservationsDue": ObservationsDue,
        "ObserveOwnedJob": ObserveOwnedJob,
        "ObservePacing": ObservePacing,
        "ObservedJobFacts": ObservedJobFacts,
        "OwnedJob": OwnedJob,
        "PreparedSubmissionReceipt": PreparedSubmissionReceipt,
        "RegisteredJobObserved": RegisteredJobObserved,
        "RegisteredJobRequested": RegisteredJobRequested,
        "RegisteredOwnedJob": RegisteredOwnedJob,
        "ResumeAuthorizationReceipt": ResumeAuthorizationReceipt,
        "ResumeAuthorized": ResumeAuthorized,
        "ResumeRefusal": ResumeRefusal,
        "ResumeRefused": ResumeRefused,
        "SnapshotResultRef": SnapshotResultRef,
        "SubmissionBudget": SubmissionBudget,
        "SubmissionReceipt": SubmissionReceipt,
        "SubmitMeasurement": SubmitMeasurement,
        "TurnSuspended": TurnSuspended,
        "UnobservedJobFacts": UnobservedJobFacts,
    }


def _load_types_evaluation_history() -> dict[str, object]:
    from vs_core.types.evaluation_history import (
        AttemptEvaluationHistory,
        AttemptEvaluationRecord,
        AttemptTerminalReason,
        BenchmarkFailure,
        EvaluationHistoryAvailability,
        EvaluationHistoryCursor,
        EvaluationStageOutcome,
        EvaluationStageResult,
        EvaluationTerminalFacts,
        RepeatedFailureGuidance,
    )

    return {
        "AttemptEvaluationHistory": AttemptEvaluationHistory,
        "AttemptEvaluationRecord": AttemptEvaluationRecord,
        "AttemptTerminalReason": AttemptTerminalReason,
        "BenchmarkFailure": BenchmarkFailure,
        "EvaluationHistoryAvailability": EvaluationHistoryAvailability,
        "EvaluationHistoryCursor": EvaluationHistoryCursor,
        "EvaluationStageOutcome": EvaluationStageOutcome,
        "EvaluationStageResult": EvaluationStageResult,
        "EvaluationTerminalFacts": EvaluationTerminalFacts,
        "RepeatedFailureGuidance": RepeatedFailureGuidance,
    }


def _load_types_job_observations() -> dict[str, object]:
    from vs_core.types.job_observations import (
        JobProgress,
        JobTimeout,
        MeasurementFailure,
        TimedOut,
        WaitEnd,
    )

    return {
        "JobProgress": JobProgress,
        "JobTimeout": JobTimeout,
        "MeasurementFailure": MeasurementFailure,
        "TimedOut": TimedOut,
        "WaitEnd": WaitEnd,
    }


def _load_types_kernel() -> dict[str, object]:
    from vs_core.types.kernel import (
        AreaChange,
        AreaContext,
        AttemptsContext,
        ControlChanged,
        CoreEvent,
        CoreState,
        CoreView,
        DecisionCompleted,
        DecisionReceipt,
        DecisionSubmitted,
        EpochResumed,
        EvaluationContext,
        ProposalSubmitted,
        RecordsContext,
        RunControlEvent,
        RunEnded,
        RunEnvelope,
        RunState,
        RunSummary,
        SchedulingContext,
        SessionsContext,
        SettlementContext,
        Signal,
        StopLiftRecorded,
        StrategyEvent,
        Transition,
    )

    return {
        "AreaChange": AreaChange,
        "AreaContext": AreaContext,
        "AttemptsContext": AttemptsContext,
        "ControlChanged": ControlChanged,
        "CoreEvent": CoreEvent,
        "CoreState": CoreState,
        "CoreView": CoreView,
        "DecisionCompleted": DecisionCompleted,
        "DecisionReceipt": DecisionReceipt,
        "DecisionSubmitted": DecisionSubmitted,
        "EpochResumed": EpochResumed,
        "EvaluationContext": EvaluationContext,
        "ProposalSubmitted": ProposalSubmitted,
        "RecordsContext": RecordsContext,
        "RunControlEvent": RunControlEvent,
        "RunEnded": RunEnded,
        "RunEnvelope": RunEnvelope,
        "RunState": RunState,
        "RunSummary": RunSummary,
        "SchedulingContext": SchedulingContext,
        "SessionsContext": SessionsContext,
        "SettlementContext": SettlementContext,
        "Signal": Signal,
        "StopLiftRecorded": StopLiftRecorded,
        "StrategyEvent": StrategyEvent,
        "Transition": Transition,
    }


def _load_types_kinds() -> dict[str, object]:
    from vs_core.types.kinds import EventKind, WorkKind

    return {"EventKind": EventKind, "WorkKind": WorkKind}


def _load_types_measurement() -> dict[str, object]:
    from vs_core.types.measurement import (
        BaseClaim,
        BaseRef,
        Comparison,
        ComparisonSettled,
        ComparisonVerdict,
        CoreRun,
        EffectVerdict,
        Reading,
        Runaway,
        Sample,
        StatedComparison,
    )

    return {
        "BaseClaim": BaseClaim,
        "BaseRef": BaseRef,
        "Comparison": Comparison,
        "ComparisonSettled": ComparisonSettled,
        "ComparisonVerdict": ComparisonVerdict,
        "CoreRun": CoreRun,
        "EffectVerdict": EffectVerdict,
        "Reading": Reading,
        "Runaway": Runaway,
        "Sample": Sample,
        "StatedComparison": StatedComparison,
    }


def _load_types_records() -> dict[str, object]:
    from vs_core.types.records import (
        Attribution,
        BlockedRecordStalled,
        BlockRequest,
        CancelOwnedResource,
        CapacityHold,
        ChildLease,
        ChildObservationWatermark,
        DispatchAuthorized,
        ExecuteRegisteredOperation,
        HostEpoch,
        HostRenewed,
        InspectRequest,
        OperationResult,
        OperationRetireRequested,
        OperationView,
        Outbound,
        ReconciliationRequest,
        RecordsEvent,
        RecordsState,
        RecoveryBarrier,
        RecoveryCheck,
        RecoveryCheckWaitLapsed,
        RecoveryPhase,
        RecoveryReady,
        RecoveryStarted,
        RegisteredOperationRequest,
        ReissueProof,
        Request,
        RequestObserved,
        RequestPhase,
        RequestPrepared,
        RequestRecord,
        RequestUndispatchable,
        RetryDue,
        TargetObservation,
        TurnHeld,
        inherit,
        within,
    )

    return {
        "Attribution": Attribution,
        "BlockRequest": BlockRequest,
        "BlockedRecordStalled": BlockedRecordStalled,
        "CancelOwnedResource": CancelOwnedResource,
        "CapacityHold": CapacityHold,
        "ChildLease": ChildLease,
        "ChildObservationWatermark": ChildObservationWatermark,
        "DispatchAuthorized": DispatchAuthorized,
        "ExecuteRegisteredOperation": ExecuteRegisteredOperation,
        "HostEpoch": HostEpoch,
        "HostRenewed": HostRenewed,
        "InspectRequest": InspectRequest,
        "OperationResult": OperationResult,
        "OperationRetireRequested": OperationRetireRequested,
        "OperationView": OperationView,
        "Outbound": Outbound,
        "ReconciliationRequest": ReconciliationRequest,
        "RecordsEvent": RecordsEvent,
        "RecordsState": RecordsState,
        "RecoveryBarrier": RecoveryBarrier,
        "RecoveryCheck": RecoveryCheck,
        "RecoveryCheckWaitLapsed": RecoveryCheckWaitLapsed,
        "RecoveryPhase": RecoveryPhase,
        "RecoveryReady": RecoveryReady,
        "RecoveryStarted": RecoveryStarted,
        "RegisteredOperationRequest": RegisteredOperationRequest,
        "ReissueProof": ReissueProof,
        "Request": Request,
        "RequestObserved": RequestObserved,
        "RequestPhase": RequestPhase,
        "RequestPrepared": RequestPrepared,
        "RequestRecord": RequestRecord,
        "RequestUndispatchable": RequestUndispatchable,
        "RetryDue": RetryDue,
        "TargetObservation": TargetObservation,
        "TurnHeld": TurnHeld,
        "inherit": inherit,
        "within": within,
    }


def _load_types_scheduling() -> dict[str, object]:
    from vs_core.types.scheduling import (
        AdmissionControl,
        AdmissionRequest,
        AdmitAttempt,
        AdoptionFenceLifted,
        AttemptReady,
        AttemptReopenRequest,
        AttemptReopenRequested,
        AttemptRequest,
        AttemptRequested,
        ClockAdvanced,
        CloseAdmission,
        ParallelLimitChanged,
        QueueEntryRetired,
        RegisterAttempt,
        RunDrained,
        SchedulingEvent,
        SchedulingState,
        SchedulingView,
        Slot,
        SlotChargeEnded,
        SlotReleased,
    )

    return {
        "AdmissionControl": AdmissionControl,
        "AdmissionRequest": AdmissionRequest,
        "AdmitAttempt": AdmitAttempt,
        "AdoptionFenceLifted": AdoptionFenceLifted,
        "AttemptReady": AttemptReady,
        "AttemptReopenRequest": AttemptReopenRequest,
        "AttemptReopenRequested": AttemptReopenRequested,
        "AttemptRequest": AttemptRequest,
        "AttemptRequested": AttemptRequested,
        "ClockAdvanced": ClockAdvanced,
        "CloseAdmission": CloseAdmission,
        "ParallelLimitChanged": ParallelLimitChanged,
        "QueueEntryRetired": QueueEntryRetired,
        "RegisterAttempt": RegisterAttempt,
        "RunDrained": RunDrained,
        "SchedulingEvent": SchedulingEvent,
        "SchedulingState": SchedulingState,
        "SchedulingView": SchedulingView,
        "Slot": Slot,
        "SlotChargeEnded": SlotChargeEnded,
        "SlotReleased": SlotReleased,
    }


def _load_types_scope_reopen() -> dict[str, object]:
    from vs_core.types.scope_reopen import ScopedAdmissionReopen, ScopedAdmissionReopenOutcome

    return {
        "ScopedAdmissionReopen": ScopedAdmissionReopen,
        "ScopedAdmissionReopenOutcome": ScopedAdmissionReopenOutcome,
    }


def _load_types_search() -> dict[str, object]:
    from vs_core.types.search import (
        AdoptionFinished,
        AttemptSummary,
        CheckpointKnown,
        Discarded,
        DiscardReason,
        Event,
        Interrupted,
        MeasurementFinished,
        MessageDelivered,
        MessageDropped,
        OperationFinished,
        ResumeFailed,
        ResumeReady,
        RunView,
        StopLifted,
        StopRequested,
        TimeReached,
        TurnEnded,
        TurnFinished,
        TurnYielded,
        WorkspaceClosed,
        WorkspaceOpened,
        YieldFailed,
    )

    return {
        "AdoptionFinished": AdoptionFinished,
        "AttemptSummary": AttemptSummary,
        "CheckpointKnown": CheckpointKnown,
        "DiscardReason": DiscardReason,
        "Discarded": Discarded,
        "Event": Event,
        "Interrupted": Interrupted,
        "MeasurementFinished": MeasurementFinished,
        "MessageDelivered": MessageDelivered,
        "MessageDropped": MessageDropped,
        "OperationFinished": OperationFinished,
        "ResumeFailed": ResumeFailed,
        "ResumeReady": ResumeReady,
        "RunView": RunView,
        "StopLifted": StopLifted,
        "StopRequested": StopRequested,
        "TimeReached": TimeReached,
        "TurnEnded": TurnEnded,
        "TurnFinished": TurnFinished,
        "TurnYielded": TurnYielded,
        "WorkspaceClosed": WorkspaceClosed,
        "WorkspaceOpened": WorkspaceOpened,
        "YieldFailed": YieldFailed,
    }


def _load_types_session_inputs() -> dict[str, object]:
    from vs_core.types.session_inputs import (
        InputDelivered,
        InputDropped,
        InputDropReason,
        InputMode,
        InputReceipt,
        InputRecord,
        MidTurnOffer,
        SessionInput,
    )

    return {
        "InputDelivered": InputDelivered,
        "InputDropReason": InputDropReason,
        "InputDropped": InputDropped,
        "InputMode": InputMode,
        "InputReceipt": InputReceipt,
        "InputRecord": InputRecord,
        "MidTurnOffer": MidTurnOffer,
        "SessionInput": SessionInput,
    }


def _load_types_sessions() -> dict[str, object]:
    from vs_core.types.sessions import (
        TURN_FAILURE_DETAIL_LIMIT,
        Access,
        AcquisitionStalled,
        CancelTurn,
        CheckpointFailure,
        CheckpointStalled,
        CloseSession,
        DispatchTurn,
        EnsureSession,
        InputAcceptanceObserved,
        InputMidTurnReleased,
        InputOfferedMidTurn,
        InputReservationReleased,
        InputReservationRequested,
        InputTakenMidTurn,
        InspectTurn,
        InterruptClaim,
        InterruptCompleted,
        InterruptFailed,
        InterruptRequested,
        Invocation,
        InvocationCancellationRequested,
        InvocationChargeRefunded,
        InvocationChargesAuthorized,
        InvocationCheckpointAvailable,
        InvocationCheckpointPending,
        RegisteredTurnRequested,
        ResumeSessionTurn,
        RunInvocationCheckpoint,
        RunInvocationCheckpointObserved,
        RunInvocationCheckpointRequested,
        RunSessionsDrainRequested,
        SessionAcquisitionGroup,
        SessionDrainRequested,
        SessionInputReceived,
        SessionObserved,
        SessionPhase,
        SessionProjection,
        SessionRequest,
        SessionsAcquireRequested,
        SessionsEvent,
        SessionSpec,
        SessionsState,
        SessionView,
        SessionWaitLapsed,
        SnapshotAndRetainRun,
        SteerReceived,
        TurnFailureKind,
        TurnInputsReserved,
        TurnObserved,
        TurnRequested,
        TurnResult,
        TurnSpec,
        YieldPublicationFailed,
    )

    return {
        "Access": Access,
        "AcquisitionStalled": AcquisitionStalled,
        "CancelTurn": CancelTurn,
        "CheckpointFailure": CheckpointFailure,
        "CheckpointStalled": CheckpointStalled,
        "CloseSession": CloseSession,
        "DispatchTurn": DispatchTurn,
        "EnsureSession": EnsureSession,
        "InputAcceptanceObserved": InputAcceptanceObserved,
        "InputMidTurnReleased": InputMidTurnReleased,
        "InputOfferedMidTurn": InputOfferedMidTurn,
        "InputReservationReleased": InputReservationReleased,
        "InputReservationRequested": InputReservationRequested,
        "InputTakenMidTurn": InputTakenMidTurn,
        "InspectTurn": InspectTurn,
        "InterruptClaim": InterruptClaim,
        "InterruptCompleted": InterruptCompleted,
        "InterruptFailed": InterruptFailed,
        "InterruptRequested": InterruptRequested,
        "Invocation": Invocation,
        "InvocationCancellationRequested": InvocationCancellationRequested,
        "InvocationChargeRefunded": InvocationChargeRefunded,
        "InvocationChargesAuthorized": InvocationChargesAuthorized,
        "InvocationCheckpointAvailable": InvocationCheckpointAvailable,
        "InvocationCheckpointPending": InvocationCheckpointPending,
        "RegisteredTurnRequested": RegisteredTurnRequested,
        "ResumeSessionTurn": ResumeSessionTurn,
        "RunInvocationCheckpoint": RunInvocationCheckpoint,
        "RunInvocationCheckpointObserved": RunInvocationCheckpointObserved,
        "RunInvocationCheckpointRequested": RunInvocationCheckpointRequested,
        "RunSessionsDrainRequested": RunSessionsDrainRequested,
        "SessionAcquisitionGroup": SessionAcquisitionGroup,
        "SessionDrainRequested": SessionDrainRequested,
        "SessionInputReceived": SessionInputReceived,
        "SessionObserved": SessionObserved,
        "SessionPhase": SessionPhase,
        "SessionProjection": SessionProjection,
        "SessionRequest": SessionRequest,
        "SessionSpec": SessionSpec,
        "SessionView": SessionView,
        "SessionWaitLapsed": SessionWaitLapsed,
        "SessionsAcquireRequested": SessionsAcquireRequested,
        "SessionsEvent": SessionsEvent,
        "SessionsState": SessionsState,
        "SnapshotAndRetainRun": SnapshotAndRetainRun,
        "SteerReceived": SteerReceived,
        "TURN_FAILURE_DETAIL_LIMIT": TURN_FAILURE_DETAIL_LIMIT,
        "TurnFailureKind": TurnFailureKind,
        "TurnInputsReserved": TurnInputsReserved,
        "TurnObserved": TurnObserved,
        "TurnRequested": TurnRequested,
        "TurnResult": TurnResult,
        "TurnSpec": TurnSpec,
        "YieldPublicationFailed": YieldPublicationFailed,
    }


def _load_types_settlement() -> dict[str, object]:
    from vs_core.types.settlement import (
        Adoption,
        AdoptionFailed,
        AdoptionFailureReason,
        AdoptionObserved,
        AdoptionRequest,
        AdoptionResult,
        AdoptionView,
        AdoptRevision,
        AssessmentAuthority,
        AssessmentProposal,
        AssessmentSubmitted,
        AttemptSettled,
        EvidenceRequirement,
        EvidenceRequirements,
        OwnershipSettled,
        RetainedCandidate,
        RunResultProposal,
        Selection,
        Settlement,
        SettlementEvent,
        SettlementState,
        TrustedBaseline,
        VerifyAdoption,
        WinnerProposed,
    )

    return {
        "AdoptRevision": AdoptRevision,
        "Adoption": Adoption,
        "AdoptionFailed": AdoptionFailed,
        "AdoptionFailureReason": AdoptionFailureReason,
        "AdoptionObserved": AdoptionObserved,
        "AdoptionRequest": AdoptionRequest,
        "AdoptionResult": AdoptionResult,
        "AdoptionView": AdoptionView,
        "AssessmentAuthority": AssessmentAuthority,
        "AssessmentProposal": AssessmentProposal,
        "AssessmentSubmitted": AssessmentSubmitted,
        "AttemptSettled": AttemptSettled,
        "EvidenceRequirement": EvidenceRequirement,
        "EvidenceRequirements": EvidenceRequirements,
        "OwnershipSettled": OwnershipSettled,
        "RetainedCandidate": RetainedCandidate,
        "RunResultProposal": RunResultProposal,
        "Selection": Selection,
        "Settlement": Settlement,
        "SettlementEvent": SettlementEvent,
        "SettlementState": SettlementState,
        "TrustedBaseline": TrustedBaseline,
        "VerifyAdoption": VerifyAdoption,
        "WinnerProposed": WinnerProposed,
    }


def _load_types_strategy() -> dict[str, object]:
    from vs_core.types.strategy import (
        Accepted,
        Cancel,
        Command,
        Decision,
        DecisionBase,
        DecisionFeedback,
        Disposition,
        Finish,
        Interrupt,
        Measure,
        Operation,
        OperationCodecError,
        OperationRequest,
        Park,
        ProposeWinner,
        Rejected,
        RequestTurn,
        SearchDeclaration,
        Settle,
        StartAttempt,
        StrategyState,
        Submission,
        Wait,
        WakeAt,
        Withdraw,
        Work,
    )

    return {
        "Accepted": Accepted,
        "Cancel": Cancel,
        "Command": Command,
        "Decision": Decision,
        "DecisionBase": DecisionBase,
        "DecisionFeedback": DecisionFeedback,
        "Disposition": Disposition,
        "Finish": Finish,
        "Interrupt": Interrupt,
        "Measure": Measure,
        "Operation": Operation,
        "OperationCodecError": OperationCodecError,
        "OperationRequest": OperationRequest,
        "Park": Park,
        "ProposeWinner": ProposeWinner,
        "Rejected": Rejected,
        "RequestTurn": RequestTurn,
        "SearchDeclaration": SearchDeclaration,
        "Settle": Settle,
        "StartAttempt": StartAttempt,
        "StrategyState": StrategyState,
        "Submission": Submission,
        "Wait": Wait,
        "WakeAt": WakeAt,
        "Withdraw": Withdraw,
        "Work": Work,
    }


def _load_types_until() -> dict[str, object]:
    from vs_core.types.until import (
        REPLY_ERROR_MESSAGE_LIMIT,
        Failed,
        FailureReason,
        ReplyError,
        ReplyErrorCode,
        Until,
        WaitRefusal,
        WhileAlive,
        WhileOpen,
        Within,
    )

    return {
        "Failed": Failed,
        "FailureReason": FailureReason,
        "REPLY_ERROR_MESSAGE_LIMIT": REPLY_ERROR_MESSAGE_LIMIT,
        "ReplyError": ReplyError,
        "ReplyErrorCode": ReplyErrorCode,
        "Until": Until,
        "WaitRefusal": WaitRefusal,
        "WhileAlive": WhileAlive,
        "WhileOpen": WhileOpen,
        "Within": Within,
    }


def _load_types_waits() -> dict[str, object]:
    from vs_core.types.waits import WaitOn

    return {"WaitOn": WaitOn}


def _load_testing_operations() -> dict[str, object]:
    from vs_core.testing.operations import operation_trace

    return {"operation_trace": operation_trace}


def _load_testing_traces() -> dict[str, object]:
    from vs_core.testing.traces import (
        AttemptsChange,
        EvaluationChange,
        RecordsChange,
        ReducerTrace,
        SchedulingChange,
        SessionsChange,
        SettlementChange,
        TraceChange,
        TraceFrame,
        trace_step,
    )

    return {
        "AttemptsChange": AttemptsChange,
        "EvaluationChange": EvaluationChange,
        "RecordsChange": RecordsChange,
        "ReducerTrace": ReducerTrace,
        "SchedulingChange": SchedulingChange,
        "SessionsChange": SessionsChange,
        "SettlementChange": SettlementChange,
        "TraceChange": TraceChange,
        "TraceFrame": TraceFrame,
        "trace_step": trace_step,
    }


EXPORT_MODULES: dict[str, Callable[[], dict[str, object]]] = {
    "vs_core._attempt_track": _load_attempt_track,
    "vs_core._comparison": _load_comparison,
    "vs_core._continuations": _load_continuations,
    "vs_core._corevalue": _load_corevalue,
    "vs_core._envelope_migrations": _load_envelope_migrations,
    "vs_core._evidence_views": _load_evidence_views,
    "vs_core._host_epochs": _load_host_epochs,
    "vs_core._ids": _load_ids,
    "vs_core._ledger_compaction": _load_ledger_compaction,
    "vs_core._measurement_recipe": _load_measurement_recipe,
    "vs_core._operator_actions": _load_operator_actions,
    "vs_core._ownership": _load_ownership,
    "vs_core._projection": _load_projection,
    "vs_core._proofs": _load_proofs,
    "vs_core._registry": _load_registry,
    "vs_core._turn_permit": _load_turn_permit,
    "vs_core._run_pause": _load_run_pause,
    "vs_core._search_events": _load_search_events,
    "vs_core._search_run": _load_search_run,
    "vs_core._session_scope": _load_session_scope,
    "vs_core._step": _load_step,
    "vs_core._strategy_operations": _load_strategy_operations,
    "vs_core._turn_chain": _load_turn_chain,
    "vs_core._turn_holds": _load_turn_holds,
    "vs_core._values": _load_values,
    "vs_core._waitable": _load_waitable,
    "vs_core._waits": _load_waits,
    "vs_core.api.routing": _load_api_routing,
    "vs_core.attempts": _load_attempts,
    "vs_core.evaluation": _load_evaluation,
    "vs_core.flows": _load_flows,
    "vs_core.records": _load_records,
    "vs_core.scheduling": _load_scheduling,
    "vs_core.sessions": _load_sessions,
    "vs_core.settlement": _load_settlement,
    "vs_core.testing.builders": _load_testing_builders,
    "vs_core.types.agents": _load_types_agents,
    "vs_core.types.attempts": _load_types_attempts,
    "vs_core.types.common": _load_types_common,
    "vs_core.types.evaluation": _load_types_evaluation,
    "vs_core.types.evaluation_history": _load_types_evaluation_history,
    "vs_core.types.job_observations": _load_types_job_observations,
    "vs_core.types.kernel": _load_types_kernel,
    "vs_core.types.kinds": _load_types_kinds,
    "vs_core.types.measurement": _load_types_measurement,
    "vs_core.types.records": _load_types_records,
    "vs_core.types.scheduling": _load_types_scheduling,
    "vs_core.types.scope_reopen": _load_types_scope_reopen,
    "vs_core.types.search": _load_types_search,
    "vs_core.types.session_inputs": _load_types_session_inputs,
    "vs_core.types.sessions": _load_types_sessions,
    "vs_core.types.settlement": _load_types_settlement,
    "vs_core.types.strategy": _load_types_strategy,
    "vs_core.types.until": _load_types_until,
    "vs_core.types.waits": _load_types_waits,
    "vs_core.testing.operations": _load_testing_operations,
    "vs_core.testing.traces": _load_testing_traces,
}
