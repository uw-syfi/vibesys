"""Published pure lifecycle API. Area implementations land in wave 1.

Exports load on first use so host control commands do not load run execution machinery.
"""

# ruff: noqa: PLC0415
# lint-waiver: LW-602176 [PLC0415]; these facade loaders defer statically declared pure imports.
# > Eager imports load the entire lifecycle for a control admission; importlib would violate
# > core purity. Deferred static imports preserve both boundaries without a dynamic importer.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core._attempt_track import AttemptTrack, Step
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
    from vs_core._continuations import SuspensionRefusal
    from vs_core._corevalue import CoreValue, FrozenMap, TimestampValue
    from vs_core._envelope_migrations import MIGRATIONS as ENVELOPE_MIGRATIONS
    from vs_core._envelope_migrations import OLDEST_SUPPORTED_ENVELOPE_VERSION
    from vs_core._evidence_views import (
        TurnCandidate,
        TurnCandidateFailed,
        TurnCandidatePending,
        TurnCandidateRetained,
        ledger_refs,
        trusted_keys,
        turn_candidate,
    )
    from vs_core._host_epochs import downtime
    from vs_core._ids import (
        IdScheme,
        attempt_id,
        attempt_ref,
        component,
        decision_of_operation,
        item_id,
    )
    from vs_core._ledger_compaction import compact_ledger
    from vs_core._measurement_recipe import MeasurementPurpose, MeasurementRecipe, StageBudget
    from vs_core._operator_actions import (
        ItemCancellation,
        cancelled_items,
        item_cancellation,
        notice_input_id,
        parallel_limit,
    )
    from vs_core._ownership import unreleased_records
    from vs_core._projection import (
        attempt_view,
        evidence_view,
        pending_requests,
        project,
        scheduling_view,
        session_view,
        settlement_view,
    )
    from vs_core._proofs import requested_stop_mode, stop_mode
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
    from vs_core._run_pause import (
        DEADLINE_STOP,
        ENVIRONMENT_STOP,
        ControlRefusedError,
        capacity_wait_stands,
        control_refusal,
        pause_after,
        reopened,
        reopens_after,
        reopens_on_resume,
        run_pause,
        stands_after,
    )
    from vs_core._search_events import Translated, run_view, search_events, translate
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
    from vs_core._session_scope import write_turn_authority
    from vs_core._step import (
        CoreReducers,
        mid_turn_offers,
        step,
        suspension_refusal,
        validate_terminal_inputs,
    )
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
    from vs_core._turn_chain import TurnChain
    from vs_core._turn_holds import held_limit
    from vs_core._values import EncodingMismatchError, encode_json, share_unchanged, verify_encoding
    from vs_core._waitable import (
        SuspendedTurn,
        Waitable,
        WaitableReply,
        WaitingForDependency,
        WaitingForEvaluation,
        turn_reply,
    )
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
    from vs_core.api.routing import EVENT_ROUTES
    from vs_core.attempts import advance_attempt
    from vs_core.evaluation import advance_evaluation
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
    from vs_core.records import advance_record, recover
    from vs_core.scheduling import schedule
    from vs_core.sessions import advance_session
    from vs_core.settlement import advance_adoption, settle
    from vs_core.testing.builders import initial_state
    from vs_core.testing.operations import operation_trace
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
    from vs_core.types.job_observations import (
        JobProgress,
        JobTimeout,
        MeasurementFailure,
        TimedOut,
        WaitEnd,
    )
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
    from vs_core.types.kinds import EventKind, WorkKind
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
        ReconciliationCommandLapsed,
        ReconciliationDeadline,
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
    from vs_core.types.scope_reopen import ScopedAdmissionReopen, ScopedAdmissionReopenOutcome
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
    from vs_core.types.waits import WaitOn

_MODULES: dict[str, str] = {
    name: module
    for module, names in (
        ("vs_core._attempt_track", ("AttemptTrack Step ")),
        (
            "vs_core._comparison",
            (
                "BASE_MAX_SAMPLES MAX_CORE_BASE_RUNS MIN_SAMPLES_WITHOUT_FLOOR VERDICT_NOISES "
                "ComparisonPolicy Measured Moment close_call compare comparison_of comparison_settling "
                "comparisons_of effect_resolution effect_verdict evaluation_of latest_comparisons "
                "moment_of policy_of samples_of terms_policy verdict_unreachable "
            ),
        ),
        ("vs_core._continuations", ("SuspensionRefusal ")),
        ("vs_core._corevalue", ("CoreValue FrozenMap TimestampValue ")),
        (
            "vs_core._envelope_migrations",
            ("ENVELOPE_MIGRATIONS OLDEST_SUPPORTED_ENVELOPE_VERSION "),
        ),
        (
            "vs_core._evidence_views",
            (
                "TurnCandidate TurnCandidateFailed TurnCandidatePending TurnCandidateRetained "
                "ledger_refs trusted_keys turn_candidate "
            ),
        ),
        ("vs_core._host_epochs", ("downtime ")),
        (
            "vs_core._ids",
            ("IdScheme attempt_id attempt_ref component decision_of_operation item_id "),
        ),
        ("vs_core._ledger_compaction", ("compact_ledger ")),
        ("vs_core._measurement_recipe", ("MeasurementPurpose MeasurementRecipe StageBudget ")),
        (
            "vs_core._operator_actions",
            ("ItemCancellation cancelled_items item_cancellation notice_input_id parallel_limit "),
        ),
        ("vs_core._ownership", ("unreleased_records ")),
        (
            "vs_core._projection",
            (
                "attempt_view evidence_view pending_requests project scheduling_view session_view "
                "settlement_view "
            ),
        ),
        ("vs_core._proofs", ("requested_stop_mode stop_mode ")),
        (
            "vs_core._registry",
            (
                "ENVELOPE_SCHEMA_VERSION ClosedRecordContradictionError ContractError "
                "OperationMigration OperationRegistration OperationRegistry UnexpectedEvent "
                "decode_stored operation_result upgrade_envelope validate_startup "
            ),
        ),
        (
            "vs_core._run_pause",
            (
                "DEADLINE_STOP ENVIRONMENT_STOP ControlRefusedError capacity_wait_stands "
                "control_refusal pause_after reopened reopens_after reopens_on_resume run_pause "
                "stands_after "
            ),
        ),
        ("vs_core._search_events", ("Translated run_view search_events translate ")),
        (
            "vs_core._search_run",
            (
                "Advanced CoreStep Inbox SearchDriver SearchHost advance begin check_decisions "
                "deferred "
            ),
        ),
        ("vs_core._session_scope", ("write_turn_authority ")),
        (
            "vs_core._step",
            ("CoreReducers mid_turn_offers step suspension_refusal validate_terminal_inputs "),
        ),
        (
            "vs_core._strategy_operations",
            (
                "CAPTURE_SUFFIX EXPORT_PATCH_SUFFIX INTERPRET_SUFFIX RENDER_SUFFIX RETAIN_SUFFIX "
                "VALIDATE_LOCAL_SUFFIX VERIFY_SUFFIX CaptureCommandBase CommandCapture "
                "EvidenceReadingsBase ExportPatchBase InterpretEvidenceBase LocalValidationReport "
                "OperationKindError ParentVerification PatchExport RenderedArtifacts "
                "RenderRoleArtifactsBase RetainVerifiedRevisionBase RetentionReceipt RolePromptContext "
                "StrategyOperations ValidateLocalBase VerifyParentRevisionBase "
            ),
        ),
        ("vs_core._turn_chain", ("TurnChain ")),
        ("vs_core._turn_holds", ("held_limit ")),
        ("vs_core._values", ("EncodingMismatchError encode_json share_unchanged verify_encoding ")),
        (
            "vs_core._waitable",
            (
                "SuspendedTurn Waitable WaitableReply WaitingForDependency WaitingForEvaluation "
                "turn_reply "
            ),
        ),
        (
            "vs_core._waits",
            (
                "MAY_WAIT_ON RESOLUTIONS OpenWait Outcome Producer Resolution WaitingPhase WaitKind "
                "WaitSubject orphan_waits owned_job_subject phase_waits recorded_waits subjects waits "
            ),
        ),
        ("vs_core.api.routing", ("EVENT_ROUTES ")),
        ("vs_core.attempts", ("advance_attempt ")),
        ("vs_core.evaluation", ("advance_evaluation ")),
        (
            "vs_core.flows",
            (
                "ADOPTED INPUT MARKDOWN_PROSE_CONVENTION MAX_CORRECTIONS MAX_LOST_TURNS "
                "MAX_RENDER_SECONDS MAX_TURN_SECONDS NOT_ADOPTED PLAIN_LINE_CONVENTION Again "
                "Assessment AttemptOutcome CaptureSpec Channel ChannelKey Compared Deadlock Dropped "
                "EndReason FailureKind Flow FlowCodeChanged FlowDriver FlowState GaveUp Group Input "
                "Kept Label LabelFields Latch LatchKey NotOpened NotReopened NoWinner Parking Pending "
                "RestartSettled RestartSettleReason RetryPolicy RevisionReply Role RunTask RunTrace "
                "SearchDef Session Source Span SpanAttempt SpanChange SpanFailure SpanId SpanKind "
                "SpanLabel SpanLink SpanStatus SpanStop SpanType SpanWaitKind Stopped StopSource Task "
                "TaskStopped Timer TimeUp Tries Uncompared UnknownRoleError Waiting WaitingOn WireSpan "
                "Workspace flow hours markdown_prose minutes operation_span plain_line reply_errors "
                "resource span_changes "
            ),
        ),
        ("vs_core.records", ("advance_record recover ")),
        ("vs_core.scheduling", ("schedule ")),
        ("vs_core.sessions", ("advance_session ")),
        ("vs_core.settlement", ("advance_adoption settle ")),
        ("vs_core.testing.builders", ("initial_state ")),
        (
            "vs_core.types.agents",
            (
                "Backoff Charge ChargeClass Checkpoint Correctable Correction Declined Exchanges "
                "ProtocolContext ReplyCorrection ResumeFacts Retained WaitUnrecorded "
            ),
        ),
        (
            "vs_core.types.attempts",
            (
                "AttemptAdmitted AttemptBudget AttemptChargeRefundRequested AttemptCheckpoint "
                "AttemptClosure AttemptEvaluationExhausted AttemptEvaluationHistoryUpdated "
                "AttemptExhausted AttemptPhase AttemptReacquireRequested AttemptRegistered "
                "AttemptSetupFailed AttemptsEvent AttemptsState AttemptView CheckpointDecline "
                "CheckpointDeclined CheckpointResolved CloseAttemptScope DiscardWorkspace "
                "EnsureWorkspace InitialSessionsFailed InitialSessionsReady InvocationChargeRequested "
                "InvocationCheckpointed InvocationCheckpointRequested InvocationEnded "
                "ReacquisitionReady ReleaseDependencyBlocked ReleaseDependencyObserved RestoreRevision "
                "RetainRevision RetentionRequired RetireRequested RevisionOperationObserved "
                "RevisionOperationRequested ScopeAdmissionReopened ScopeReopenAdmitted "
                "ScopeReopenRequested SnapshotAndRetain WorkspaceObserved WorkspacePlan "
                "WorkspaceRequest "
            ),
        ),
        (
            "vs_core.types.common",
            (
                "ACTION_REFUSAL_TEXT ActionRefusal "
                "IDENTITY_PATTERN MAX_REASK_CHAIN MAX_REQUEST_RETRIES MAX_SUBMISSIONS_PER_MEASUREMENT "
                "OPERATOR_ACTIONS THIS_RUN Area ArtifactId ArtifactRef AssessmentKind AttemptId "
                "AttemptRef Capabilities ChargeId ChargeKind ChargeReceipt ChildManifest "
                "CompletionStatus ContinuationId ContractValidationError ControlAction ControlId "
                "ControlInput Count DecisionId DependencyRef DependencyStatus DigestScheme EventCursor "
                "EventId EvidenceId EvidenceKey EvidenceKind Generation HostFence HostId Identity "
                "IdentityTypeError InputId InputTarget InvocationId InvocationInputTarget "
                "InvocationRef ItemId ItemInputTarget KernelNotImplementedError LifecycleCapability "
                "LifecycleClass Limits Observation ObservationStatus OperationDescriptor OperationId "
                "OperationNormalizationKind OperationRef OperationSchemaRef OperationWire PauseOwner "
                "PoolCapacity PoolId PureOption RejectionCode ReleaseDependency RequestBase RequestId "
                "RequestRetries ReservedInputOccurrence ResourceId ResumeWhen RevisionAuthority "
                "RevisionId RevisionNumber RevisionRef RoleId RoleInputTarget RunFacts RunId RunPark "
                "RunPause RunStatus SchemaRef Scope ScopeInputTarget ScopeReopenNormalization Seconds "
                "SessionId SettlementId SetupFailureKind SignalCycleError StopMode StrategyId "
                "TaskObjective TaskStage TaskTerms Value WorkspaceMode WorkspaceRef escalate_stop "
            ),
        ),
        (
            "vs_core.types.evaluation",
            (
                "MEASUREMENT_DIAGNOSTIC_LIMIT AgentCall AgentMeasurementRequested AgentRejection "
                "CancelOwnedJob CollectEvidence Continuation ContinuationJobsChanged ContinuationPhase "
                "ContinuationReopenRequested ContinuationRetireRequested ContinuationScopeReopened "
                "DeadlineReached EvaluationEvent EvaluationRequest EvaluationState "
                "EvidenceAcceptanceReceipt EvidenceRef HistoricalSubmissionReceipt InspectOwnedJob "
                "JobFactsBeforeObservation JobObserved JobQueued JobsDrainRequested "
                "JobTerminationRequested JobWaitLapsed MeasurementIdentity MeasurementPlan "
                "MeasurementPlanError MeasurementRequested MeasurementResult MeasurementStage "
                "MeasurementStageIdentity MeasurementSubmissionEnded MeasurementSubmissionObserved "
                "ObservationsDue ObservedJobFacts ObserveOwnedJob ObservePacing OwnedJob "
                "PreparedSubmissionReceipt RegisteredJobObserved RegisteredJobRequested "
                "RegisteredOwnedJob ResumeAuthorizationReceipt ResumeAuthorized ResumeRefusal "
                "ResumeRefused SnapshotResultRef SubmissionBudget SubmissionReceipt SubmitMeasurement "
                "TurnSuspended UnobservedJobFacts "
            ),
        ),
        (
            "vs_core.types.evaluation_history",
            (
                "AttemptEvaluationHistory AttemptEvaluationRecord AttemptTerminalReason "
                "BenchmarkFailure EvaluationHistoryAvailability EvaluationHistoryCursor "
                "EvaluationStageOutcome EvaluationStageResult EvaluationTerminalFacts "
                "RepeatedFailureGuidance "
            ),
        ),
        (
            "vs_core.types.job_observations",
            ("JobProgress JobTimeout MeasurementFailure TimedOut WaitEnd "),
        ),
        (
            "vs_core.types.kernel",
            (
                "AreaChange AreaContext AttemptsContext ControlChanged CoreEvent CoreState CoreView "
                "DecisionCompleted DecisionReceipt DecisionSubmitted EvaluationContext "
                "ProposalSubmitted RecordsContext RunControlEvent RunEnded RunEnvelope RunState "
                "RunSummary SchedulingContext SessionsContext SettlementContext Signal "
                "StopLiftRecorded StrategyEvent Transition "
            ),
        ),
        ("vs_core.types.kinds", ("EventKind WorkKind ")),
        (
            "vs_core.types.measurement",
            (
                "BaseClaim BaseRef Comparison ComparisonSettled ComparisonVerdict CoreRun "
                "EffectVerdict Reading Runaway Sample StatedComparison "
            ),
        ),
        (
            "vs_core.types.records",
            (
                "Attribution BlockedRecordStalled BlockRequest CancelOwnedResource CapacityHold "
                "ChildLease ChildObservationWatermark DispatchAuthorized ExecuteRegisteredOperation "
                "HostEpoch HostRenewed InspectRequest OperationResult OperationRetireRequested "
                "OperationView Outbound ReconciliationCommandLapsed ReconciliationDeadline "
                "ReconciliationRequest RecordsEvent RecordsState RecoveryBarrier RecoveryCheck "
                "RecoveryCheckWaitLapsed RecoveryPhase RecoveryReady RecoveryStarted "
                "RegisteredOperationRequest ReissueProof Request RequestObserved RequestPhase "
                "RequestPrepared RequestRecord RequestUndispatchable RetryDue TargetObservation "
                "TurnHeld inherit within "
            ),
        ),
        (
            "vs_core.types.scheduling",
            (
                "AdmissionControl AdmissionRequest AdmitAttempt AdoptionFenceLifted AttemptReady "
                "AttemptReopenRequest AttemptReopenRequested AttemptRequest AttemptRequested "
                "ClockAdvanced CloseAdmission ParallelLimitChanged QueueEntryRetired RegisterAttempt "
                "RunDrained SchedulingEvent SchedulingState SchedulingView Slot SlotChargeEnded "
                "SlotReleased "
            ),
        ),
        ("vs_core.types.scope_reopen", ("ScopedAdmissionReopen ScopedAdmissionReopenOutcome ")),
        (
            "vs_core.types.search",
            (
                "AdoptionFinished AttemptSummary CheckpointKnown Discarded DiscardReason Event "
                "Interrupted MeasurementFinished MessageDelivered MessageDropped OperationFinished "
                "ResumeFailed ResumeReady RunView StopLifted StopRequested TimeReached TurnEnded "
                "TurnFinished TurnYielded WorkspaceClosed WorkspaceOpened YieldFailed "
            ),
        ),
        (
            "vs_core.types.session_inputs",
            (
                "InputDelivered InputDropped InputDropReason InputMode InputReceipt InputRecord "
                "MidTurnOffer SessionInput "
            ),
        ),
        (
            "vs_core.types.sessions",
            (
                "TURN_FAILURE_DETAIL_LIMIT Access AcquisitionStalled CancelTurn CheckpointFailure "
                "CheckpointStalled CloseSession DispatchTurn EnsureSession InputAcceptanceObserved "
                "InputMidTurnReleased InputOfferedMidTurn InputReservationReleased "
                "InputReservationRequested InputTakenMidTurn InspectTurn InterruptClaim "
                "InterruptCompleted InterruptFailed InterruptRequested Invocation "
                "InvocationCancellationRequested InvocationChargeRefunded InvocationChargesAuthorized "
                "InvocationCheckpointAvailable InvocationCheckpointPending RegisteredTurnRequested "
                "ResumeSessionTurn RunInvocationCheckpoint RunInvocationCheckpointObserved "
                "RunInvocationCheckpointRequested RunSessionsDrainRequested SessionAcquisitionGroup "
                "SessionDrainRequested SessionInputReceived SessionObserved SessionPhase "
                "SessionProjection SessionRequest SessionsAcquireRequested SessionsEvent SessionSpec "
                "SessionsState SessionView SessionWaitLapsed SnapshotAndRetainRun SteerReceived "
                "TurnFailureKind TurnInputsReserved TurnObserved TurnRequested TurnResult TurnSpec "
                "YieldPublicationFailed "
            ),
        ),
        (
            "vs_core.types.settlement",
            (
                "Adoption AdoptionFailed AdoptionFailureReason AdoptionObserved AdoptionRequest "
                "AdoptionResult AdoptionView AdoptRevision AssessmentAuthority AssessmentProposal "
                "AssessmentSubmitted AttemptSettled EvidenceRequirement EvidenceRequirements "
                "OwnershipSettled RetainedCandidate RunResultProposal Selection Settlement "
                "SettlementEvent SettlementState TrustedBaseline VerifyAdoption WinnerProposed "
            ),
        ),
        (
            "vs_core.types.strategy",
            (
                "Accepted Cancel Command Decision DecisionBase DecisionFeedback Disposition Finish "
                "Interrupt Measure Operation OperationCodecError OperationRequest Park ProposeWinner "
                "Rejected RequestTurn SearchDeclaration Settle StartAttempt StrategyState Submission "
                "Wait WakeAt Withdraw Work "
            ),
        ),
        (
            "vs_core.types.until",
            (
                "REPLY_ERROR_MESSAGE_LIMIT Failed FailureReason ReplyError ReplyErrorCode Until "
                "WaitRefusal WhileAlive WhileOpen Within "
            ),
        ),
        ("vs_core.types.waits", ("WaitOn ")),
        ("vs_core.testing.operations", ("operation_trace ")),
        (
            "vs_core.testing.traces",
            (
                "AttemptsChange EvaluationChange RecordsChange ReducerTrace SchedulingChange "
                "SessionsChange SettlementChange TraceChange TraceFrame trace_step "
            ),
        ),
    )
    for name in names.split()
}

_ALIASES: dict[str, str] = {"ENVELOPE_MIGRATIONS": "MIGRATIONS"}


def _load_attempt_track() -> object:
    from vs_core import _attempt_track as module

    return module


def _load_comparison() -> object:
    from vs_core import _comparison as module

    return module


def _load_continuations() -> object:
    from vs_core import _continuations as module

    return module


def _load_corevalue() -> object:
    from vs_core import _corevalue as module

    return module


def _load_envelope_migrations() -> object:
    from vs_core import _envelope_migrations as module

    return module


def _load_evidence_views() -> object:
    from vs_core import _evidence_views as module

    return module


def _load_host_epochs() -> object:
    from vs_core import _host_epochs as module

    return module


def _load_ids() -> object:
    from vs_core import _ids as module

    return module


def _load_ledger_compaction() -> object:
    from vs_core import _ledger_compaction as module

    return module


def _load_measurement_recipe() -> object:
    from vs_core import _measurement_recipe as module

    return module


def _load_operator_actions() -> object:
    from vs_core import _operator_actions as module

    return module


def _load_ownership() -> object:
    from vs_core import _ownership as module

    return module


def _load_projection() -> object:
    from vs_core import _projection as module

    return module


def _load_proofs() -> object:
    from vs_core import _proofs as module

    return module


def _load_registry() -> object:
    from vs_core import _registry as module

    return module


def _load_run_pause() -> object:
    from vs_core import _run_pause as module

    return module


def _load_search_events() -> object:
    from vs_core import _search_events as module

    return module


def _load_search_run() -> object:
    from vs_core import _search_run as module

    return module


def _load_session_scope() -> object:
    from vs_core import _session_scope as module

    return module


def _load_step() -> object:
    from vs_core import _step as module

    return module


def _load_strategy_operations() -> object:
    from vs_core import _strategy_operations as module

    return module


def _load_turn_chain() -> object:
    from vs_core import _turn_chain as module

    return module


def _load_turn_holds() -> object:
    from vs_core import _turn_holds as module

    return module


def _load_values() -> object:
    from vs_core import _values as module

    return module


def _load_waitable() -> object:
    from vs_core import _waitable as module

    return module


def _load_waits() -> object:
    from vs_core import _waits as module

    return module


def _load_api_routing() -> object:
    from vs_core.api import routing as module

    return module


def _load_attempts() -> object:
    from vs_core import attempts as module

    return module


def _load_evaluation() -> object:
    from vs_core import evaluation as module

    return module


def _load_flows() -> object:
    from vs_core import flows as module

    return module


def _load_records() -> object:
    from vs_core import records as module

    return module


def _load_scheduling() -> object:
    from vs_core import scheduling as module

    return module


def _load_sessions() -> object:
    from vs_core import sessions as module

    return module


def _load_settlement() -> object:
    from vs_core import settlement as module

    return module


def _load_testing_builders() -> object:
    from vs_core.testing import builders as module

    return module


def _load_testing_operations() -> object:
    from vs_core.testing import operations as module

    return module


def _load_testing_traces() -> object:
    from vs_core.testing import traces as module

    return module


def _load_types_agents() -> object:
    from vs_core.types import agents as module

    return module


def _load_types_attempts() -> object:
    from vs_core.types import attempts as module

    return module


def _load_types_common() -> object:
    from vs_core.types import common as module

    return module


def _load_types_evaluation() -> object:
    from vs_core.types import evaluation as module

    return module


def _load_types_evaluation_history() -> object:
    from vs_core.types import evaluation_history as module

    return module


def _load_types_job_observations() -> object:
    from vs_core.types import job_observations as module

    return module


def _load_types_kernel() -> object:
    from vs_core.types import kernel as module

    return module


def _load_types_kinds() -> object:
    from vs_core.types import kinds as module

    return module


def _load_types_measurement() -> object:
    from vs_core.types import measurement as module

    return module


def _load_types_records() -> object:
    from vs_core.types import records as module

    return module


def _load_types_scheduling() -> object:
    from vs_core.types import scheduling as module

    return module


def _load_types_scope_reopen() -> object:
    from vs_core.types import scope_reopen as module

    return module


def _load_types_search() -> object:
    from vs_core.types import search as module

    return module


def _load_types_session_inputs() -> object:
    from vs_core.types import session_inputs as module

    return module


def _load_types_sessions() -> object:
    from vs_core.types import sessions as module

    return module


def _load_types_settlement() -> object:
    from vs_core.types import settlement as module

    return module


def _load_types_strategy() -> object:
    from vs_core.types import strategy as module

    return module


def _load_types_until() -> object:
    from vs_core.types import until as module

    return module


def _load_types_waits() -> object:
    from vs_core.types import waits as module

    return module


_LOADERS: dict[str, Callable[[], object]] = {
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
    "vs_core.testing.operations": _load_testing_operations,
    "vs_core.testing.traces": _load_testing_traces,
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
}


def __getattr__(name: str) -> object:
    """Load a declared export through a static pure-module import."""
    module = _MODULES.get(name)
    if module is None:
        message = f"module vs_core.api has no attribute {name!r}"
        raise AttributeError(message)
    return getattr(_LOADERS[module](), _ALIASES.get(name, name))


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "ACTION_REFUSAL_TEXT",
    "ADOPTED",
    "BASE_MAX_SAMPLES",
    "CAPTURE_SUFFIX",
    "DEADLINE_STOP",
    "ENVELOPE_MIGRATIONS",
    "ENVELOPE_SCHEMA_VERSION",
    "ENVIRONMENT_STOP",
    "EVENT_ROUTES",
    "EXPORT_PATCH_SUFFIX",
    "IDENTITY_PATTERN",
    "INPUT",
    "INTERPRET_SUFFIX",
    "MARKDOWN_PROSE_CONVENTION",
    "MAX_CORE_BASE_RUNS",
    "MAX_CORRECTIONS",
    "MAX_LOST_TURNS",
    "MAX_REASK_CHAIN",
    "MAX_RENDER_SECONDS",
    "MAX_REQUEST_RETRIES",
    "MAX_SUBMISSIONS_PER_MEASUREMENT",
    "MAX_TURN_SECONDS",
    "MAY_WAIT_ON",
    "MEASUREMENT_DIAGNOSTIC_LIMIT",
    "MIN_SAMPLES_WITHOUT_FLOOR",
    "NOT_ADOPTED",
    "OLDEST_SUPPORTED_ENVELOPE_VERSION",
    "OPERATOR_ACTIONS",
    "PLAIN_LINE_CONVENTION",
    "RENDER_SUFFIX",
    "REPLY_ERROR_MESSAGE_LIMIT",
    "RESOLUTIONS",
    "RETAIN_SUFFIX",
    "THIS_RUN",
    "TURN_FAILURE_DETAIL_LIMIT",
    "VALIDATE_LOCAL_SUFFIX",
    "VERDICT_NOISES",
    "VERIFY_SUFFIX",
    "Accepted",
    "Access",
    "AcquisitionStalled",
    "ActionRefusal",
    "AdmissionControl",
    "AdmissionRequest",
    "AdmitAttempt",
    "AdoptRevision",
    "Adoption",
    "AdoptionFailed",
    "AdoptionFailureReason",
    "AdoptionFenceLifted",
    "AdoptionFinished",
    "AdoptionObserved",
    "AdoptionRequest",
    "AdoptionResult",
    "AdoptionView",
    "Advanced",
    "Again",
    "AgentCall",
    "AgentMeasurementRequested",
    "AgentRejection",
    "Area",
    "AreaChange",
    "AreaContext",
    "ArtifactId",
    "ArtifactRef",
    "Assessment",
    "AssessmentAuthority",
    "AssessmentKind",
    "AssessmentProposal",
    "AssessmentSubmitted",
    "AttemptAdmitted",
    "AttemptBudget",
    "AttemptChargeRefundRequested",
    "AttemptCheckpoint",
    "AttemptClosure",
    "AttemptEvaluationExhausted",
    "AttemptEvaluationHistory",
    "AttemptEvaluationHistoryUpdated",
    "AttemptEvaluationRecord",
    "AttemptExhausted",
    "AttemptId",
    "AttemptOutcome",
    "AttemptPhase",
    "AttemptReacquireRequested",
    "AttemptReady",
    "AttemptRef",
    "AttemptRegistered",
    "AttemptReopenRequest",
    "AttemptReopenRequested",
    "AttemptRequest",
    "AttemptRequested",
    "AttemptSettled",
    "AttemptSetupFailed",
    "AttemptSummary",
    "AttemptTerminalReason",
    "AttemptTrack",
    "AttemptView",
    "AttemptsChange",
    "AttemptsContext",
    "AttemptsEvent",
    "AttemptsState",
    "Attribution",
    "Backoff",
    "BaseClaim",
    "BaseRef",
    "BenchmarkFailure",
    "BlockRequest",
    "BlockedRecordStalled",
    "Cancel",
    "CancelOwnedJob",
    "CancelOwnedResource",
    "CancelTurn",
    "Capabilities",
    "CapacityHold",
    "CaptureCommandBase",
    "CaptureSpec",
    "Channel",
    "ChannelKey",
    "Charge",
    "ChargeClass",
    "ChargeId",
    "ChargeKind",
    "ChargeReceipt",
    "Checkpoint",
    "CheckpointDecline",
    "CheckpointDeclined",
    "CheckpointFailure",
    "CheckpointKnown",
    "CheckpointResolved",
    "CheckpointStalled",
    "ChildLease",
    "ChildManifest",
    "ChildObservationWatermark",
    "ClockAdvanced",
    "CloseAdmission",
    "CloseAttemptScope",
    "CloseSession",
    "ClosedRecordContradictionError",
    "CollectEvidence",
    "Command",
    "CommandCapture",
    "Compared",
    "Comparison",
    "ComparisonPolicy",
    "ComparisonSettled",
    "ComparisonVerdict",
    "CompletionStatus",
    "Continuation",
    "ContinuationId",
    "ContinuationJobsChanged",
    "ContinuationPhase",
    "ContinuationReopenRequested",
    "ContinuationRetireRequested",
    "ContinuationScopeReopened",
    "ContractError",
    "ContractValidationError",
    "ControlAction",
    "ControlChanged",
    "ControlId",
    "ControlInput",
    "ControlRefusedError",
    "CoreEvent",
    "CoreReducers",
    "CoreRun",
    "CoreState",
    "CoreStep",
    "CoreValue",
    "CoreView",
    "Correctable",
    "Correction",
    "Count",
    "DeadlineReached",
    "Deadlock",
    "Decision",
    "DecisionBase",
    "DecisionCompleted",
    "DecisionFeedback",
    "DecisionId",
    "DecisionReceipt",
    "DecisionSubmitted",
    "Declined",
    "DependencyRef",
    "DependencyStatus",
    "DigestScheme",
    "DiscardReason",
    "DiscardWorkspace",
    "Discarded",
    "DispatchAuthorized",
    "DispatchTurn",
    "Disposition",
    "Dropped",
    "EffectVerdict",
    "EncodingMismatchError",
    "EndReason",
    "EnsureSession",
    "EnsureWorkspace",
    "EvaluationChange",
    "EvaluationContext",
    "EvaluationEvent",
    "EvaluationHistoryAvailability",
    "EvaluationHistoryCursor",
    "EvaluationRequest",
    "EvaluationStageOutcome",
    "EvaluationStageResult",
    "EvaluationState",
    "EvaluationTerminalFacts",
    "Event",
    "EventCursor",
    "EventId",
    "EventKind",
    "EvidenceAcceptanceReceipt",
    "EvidenceId",
    "EvidenceKey",
    "EvidenceKind",
    "EvidenceReadingsBase",
    "EvidenceRef",
    "EvidenceRequirement",
    "EvidenceRequirements",
    "Exchanges",
    "ExecuteRegisteredOperation",
    "ExportPatchBase",
    "Failed",
    "FailureKind",
    "FailureReason",
    "Finish",
    "Flow",
    "FlowCodeChanged",
    "FlowDriver",
    "FlowState",
    "FrozenMap",
    "GaveUp",
    "Generation",
    "Group",
    "HistoricalSubmissionReceipt",
    "HostEpoch",
    "HostFence",
    "HostId",
    "HostRenewed",
    "IdScheme",
    "Identity",
    "IdentityTypeError",
    "Inbox",
    "InitialSessionsFailed",
    "InitialSessionsReady",
    "Input",
    "InputAcceptanceObserved",
    "InputDelivered",
    "InputDropReason",
    "InputDropped",
    "InputId",
    "InputMidTurnReleased",
    "InputMode",
    "InputOfferedMidTurn",
    "InputReceipt",
    "InputRecord",
    "InputReservationReleased",
    "InputReservationRequested",
    "InputTakenMidTurn",
    "InputTarget",
    "InspectOwnedJob",
    "InspectRequest",
    "InspectTurn",
    "InterpretEvidenceBase",
    "Interrupt",
    "InterruptClaim",
    "InterruptCompleted",
    "InterruptFailed",
    "InterruptRequested",
    "Interrupted",
    "Invocation",
    "InvocationCancellationRequested",
    "InvocationChargeRefunded",
    "InvocationChargeRequested",
    "InvocationChargesAuthorized",
    "InvocationCheckpointAvailable",
    "InvocationCheckpointPending",
    "InvocationCheckpointRequested",
    "InvocationCheckpointed",
    "InvocationEnded",
    "InvocationId",
    "InvocationInputTarget",
    "InvocationRef",
    "ItemCancellation",
    "ItemId",
    "ItemInputTarget",
    "JobFactsBeforeObservation",
    "JobObserved",
    "JobProgress",
    "JobQueued",
    "JobTerminationRequested",
    "JobTimeout",
    "JobWaitLapsed",
    "JobsDrainRequested",
    "Kept",
    "KernelNotImplementedError",
    "Label",
    "LabelFields",
    "Latch",
    "LatchKey",
    "LifecycleCapability",
    "LifecycleClass",
    "Limits",
    "LocalValidationReport",
    "Measure",
    "Measured",
    "MeasurementFailure",
    "MeasurementFinished",
    "MeasurementIdentity",
    "MeasurementPlan",
    "MeasurementPlanError",
    "MeasurementPurpose",
    "MeasurementRecipe",
    "MeasurementRequested",
    "MeasurementResult",
    "MeasurementStage",
    "MeasurementStageIdentity",
    "MeasurementSubmissionEnded",
    "MeasurementSubmissionObserved",
    "MessageDelivered",
    "MessageDropped",
    "MidTurnOffer",
    "Moment",
    "NoWinner",
    "NotOpened",
    "NotReopened",
    "Observation",
    "ObservationStatus",
    "ObservationsDue",
    "ObserveOwnedJob",
    "ObservePacing",
    "ObservedJobFacts",
    "OpenWait",
    "Operation",
    "OperationCodecError",
    "OperationDescriptor",
    "OperationFinished",
    "OperationId",
    "OperationKindError",
    "OperationMigration",
    "OperationNormalizationKind",
    "OperationRef",
    "OperationRegistration",
    "OperationRegistry",
    "OperationRequest",
    "OperationResult",
    "OperationRetireRequested",
    "OperationSchemaRef",
    "OperationView",
    "OperationWire",
    "Outbound",
    "Outcome",
    "OwnedJob",
    "OwnershipSettled",
    "ParallelLimitChanged",
    "ParentVerification",
    "Park",
    "Parking",
    "PatchExport",
    "PauseOwner",
    "Pending",
    "PoolCapacity",
    "PoolId",
    "PreparedSubmissionReceipt",
    "Producer",
    "ProposalSubmitted",
    "ProposeWinner",
    "ProtocolContext",
    "PureOption",
    "QueueEntryRetired",
    "ReacquisitionReady",
    "Reading",
    "ReconciliationCommandLapsed",
    "ReconciliationDeadline",
    "ReconciliationRequest",
    "RecordsChange",
    "RecordsContext",
    "RecordsEvent",
    "RecordsState",
    "RecoveryBarrier",
    "RecoveryCheck",
    "RecoveryCheckWaitLapsed",
    "RecoveryPhase",
    "RecoveryReady",
    "RecoveryStarted",
    "ReducerTrace",
    "RegisterAttempt",
    "RegisteredJobObserved",
    "RegisteredJobRequested",
    "RegisteredOperationRequest",
    "RegisteredOwnedJob",
    "RegisteredTurnRequested",
    "ReissueProof",
    "Rejected",
    "RejectionCode",
    "ReleaseDependency",
    "ReleaseDependencyBlocked",
    "ReleaseDependencyObserved",
    "RenderRoleArtifactsBase",
    "RenderedArtifacts",
    "RepeatedFailureGuidance",
    "ReplyCorrection",
    "ReplyError",
    "ReplyErrorCode",
    "Request",
    "RequestBase",
    "RequestId",
    "RequestObserved",
    "RequestPhase",
    "RequestPrepared",
    "RequestRecord",
    "RequestRetries",
    "RequestTurn",
    "RequestUndispatchable",
    "ReservedInputOccurrence",
    "Resolution",
    "ResourceId",
    "RestartSettleReason",
    "RestartSettled",
    "RestoreRevision",
    "ResumeAuthorizationReceipt",
    "ResumeAuthorized",
    "ResumeFacts",
    "ResumeFailed",
    "ResumeReady",
    "ResumeRefusal",
    "ResumeRefused",
    "ResumeSessionTurn",
    "ResumeWhen",
    "RetainRevision",
    "RetainVerifiedRevisionBase",
    "Retained",
    "RetainedCandidate",
    "RetentionReceipt",
    "RetentionRequired",
    "RetireRequested",
    "RetryDue",
    "RetryPolicy",
    "RevisionAuthority",
    "RevisionId",
    "RevisionNumber",
    "RevisionOperationObserved",
    "RevisionOperationRequested",
    "RevisionRef",
    "RevisionReply",
    "Role",
    "RoleId",
    "RoleInputTarget",
    "RolePromptContext",
    "RunControlEvent",
    "RunDrained",
    "RunEnded",
    "RunEnvelope",
    "RunFacts",
    "RunId",
    "RunInvocationCheckpoint",
    "RunInvocationCheckpointObserved",
    "RunInvocationCheckpointRequested",
    "RunPark",
    "RunPause",
    "RunResultProposal",
    "RunSessionsDrainRequested",
    "RunState",
    "RunStatus",
    "RunSummary",
    "RunTask",
    "RunTrace",
    "RunView",
    "Runaway",
    "Sample",
    "SchedulingChange",
    "SchedulingContext",
    "SchedulingEvent",
    "SchedulingState",
    "SchedulingView",
    "SchemaRef",
    "Scope",
    "ScopeAdmissionReopened",
    "ScopeInputTarget",
    "ScopeReopenAdmitted",
    "ScopeReopenNormalization",
    "ScopeReopenRequested",
    "ScopedAdmissionReopen",
    "ScopedAdmissionReopenOutcome",
    "SearchDeclaration",
    "SearchDef",
    "SearchDriver",
    "SearchHost",
    "Seconds",
    "Selection",
    "Session",
    "SessionAcquisitionGroup",
    "SessionDrainRequested",
    "SessionId",
    "SessionInput",
    "SessionInputReceived",
    "SessionObserved",
    "SessionPhase",
    "SessionProjection",
    "SessionRequest",
    "SessionSpec",
    "SessionView",
    "SessionWaitLapsed",
    "SessionsAcquireRequested",
    "SessionsChange",
    "SessionsContext",
    "SessionsEvent",
    "SessionsState",
    "Settle",
    "Settlement",
    "SettlementChange",
    "SettlementContext",
    "SettlementEvent",
    "SettlementId",
    "SettlementState",
    "SetupFailureKind",
    "Signal",
    "SignalCycleError",
    "Slot",
    "SlotChargeEnded",
    "SlotReleased",
    "SnapshotAndRetain",
    "SnapshotAndRetainRun",
    "SnapshotResultRef",
    "Source",
    "Span",
    "SpanAttempt",
    "SpanChange",
    "SpanFailure",
    "SpanId",
    "SpanKind",
    "SpanLabel",
    "SpanLink",
    "SpanStatus",
    "SpanStop",
    "SpanType",
    "SpanWaitKind",
    "StageBudget",
    "StartAttempt",
    "StatedComparison",
    "SteerReceived",
    "Step",
    "StopLiftRecorded",
    "StopLifted",
    "StopMode",
    "StopRequested",
    "StopSource",
    "Stopped",
    "StrategyEvent",
    "StrategyId",
    "StrategyOperations",
    "StrategyState",
    "Submission",
    "SubmissionBudget",
    "SubmissionReceipt",
    "SubmitMeasurement",
    "SuspendedTurn",
    "SuspensionRefusal",
    "TargetObservation",
    "Task",
    "TaskObjective",
    "TaskStage",
    "TaskStopped",
    "TaskTerms",
    "TimeReached",
    "TimeUp",
    "TimedOut",
    "Timer",
    "TimestampValue",
    "TraceChange",
    "TraceFrame",
    "Transition",
    "Translated",
    "Tries",
    "TrustedBaseline",
    "TurnCandidate",
    "TurnCandidateFailed",
    "TurnCandidatePending",
    "TurnCandidateRetained",
    "TurnChain",
    "TurnEnded",
    "TurnFailureKind",
    "TurnFinished",
    "TurnHeld",
    "TurnInputsReserved",
    "TurnObserved",
    "TurnRequested",
    "TurnResult",
    "TurnSpec",
    "TurnSuspended",
    "TurnYielded",
    "Uncompared",
    "UnexpectedEvent",
    "UnknownRoleError",
    "UnobservedJobFacts",
    "Until",
    "ValidateLocalBase",
    "Value",
    "VerifyAdoption",
    "VerifyParentRevisionBase",
    "Wait",
    "WaitEnd",
    "WaitKind",
    "WaitOn",
    "WaitRefusal",
    "WaitSubject",
    "WaitUnrecorded",
    "Waitable",
    "WaitableReply",
    "Waiting",
    "WaitingForDependency",
    "WaitingForEvaluation",
    "WaitingOn",
    "WaitingPhase",
    "WakeAt",
    "WhileAlive",
    "WhileOpen",
    "WinnerProposed",
    "WireSpan",
    "Withdraw",
    "Within",
    "Work",
    "WorkKind",
    "Workspace",
    "WorkspaceClosed",
    "WorkspaceMode",
    "WorkspaceObserved",
    "WorkspaceOpened",
    "WorkspacePlan",
    "WorkspaceRef",
    "WorkspaceRequest",
    "YieldFailed",
    "YieldPublicationFailed",
    "advance",
    "advance_adoption",
    "advance_attempt",
    "advance_evaluation",
    "advance_record",
    "advance_session",
    "attempt_id",
    "attempt_ref",
    "attempt_view",
    "begin",
    "cancelled_items",
    "capacity_wait_stands",
    "check_decisions",
    "close_call",
    "compact_ledger",
    "compare",
    "comparison_of",
    "comparison_settling",
    "comparisons_of",
    "component",
    "control_refusal",
    "decision_of_operation",
    "decode_stored",
    "deferred",
    "downtime",
    "effect_resolution",
    "effect_verdict",
    "encode_json",
    "escalate_stop",
    "evaluation_of",
    "evidence_view",
    "flow",
    "held_limit",
    "hours",
    "inherit",
    "initial_state",
    "item_cancellation",
    "item_id",
    "latest_comparisons",
    "ledger_refs",
    "markdown_prose",
    "mid_turn_offers",
    "minutes",
    "moment_of",
    "notice_input_id",
    "operation_result",
    "operation_span",
    "operation_trace",
    "orphan_waits",
    "owned_job_subject",
    "parallel_limit",
    "pause_after",
    "pending_requests",
    "phase_waits",
    "plain_line",
    "policy_of",
    "project",
    "recorded_waits",
    "recover",
    "reopened",
    "reopens_after",
    "reopens_on_resume",
    "reply_errors",
    "requested_stop_mode",
    "resource",
    "run_pause",
    "run_view",
    "samples_of",
    "schedule",
    "scheduling_view",
    "search_events",
    "session_view",
    "settle",
    "settlement_view",
    "share_unchanged",
    "span_changes",
    "stands_after",
    "step",
    "stop_mode",
    "subjects",
    "suspension_refusal",
    "terms_policy",
    "trace_step",
    "translate",
    "trusted_keys",
    "turn_candidate",
    "turn_reply",
    "unreleased_records",
    "upgrade_envelope",
    "validate_startup",
    "validate_terminal_inputs",
    "verdict_unreachable",
    "verify_encoding",
    "waits",
    "within",
    "write_turn_authority",
]
