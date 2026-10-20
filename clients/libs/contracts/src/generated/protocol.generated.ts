/* Generated from the Python protocol models. Do not edit. */

export type Request =
  | PauseCommand
  | ResumeCommand
  | SteerCommand
  | StopCommand
  | SnapshotQuery
  | ChatQuery
  | ChatThreadCreateQuery
  | HistoryQuery
  | PerformanceQuery
  | ExperimentQuery
  | IterationQuery
  | DesignQuery
  | DesignPatchQuery
  | EventsQuery
  | SubscribeRequest
  | ControlCommand;
export type ProtocolVersion = 1;
export type RequestId = string;
export type ClientId = string;
export type Timestamp = string;
export type Type = "command.pause";
export type Mode = "after_current_agent_call";
export type ProtocolVersion1 = 1;
export type RequestId1 = string;
export type ClientId1 = string;
export type Timestamp1 = string;
export type Type1 = "command.resume";
export type Fallback = boolean;
export type When = "now" | "capacity_returns";
export type ProtocolVersion2 = 1;
export type RequestId2 = string;
export type ClientId2 = string;
export type Timestamp2 = string;
export type Type2 = "command.steer";
export type Text = string;
export type ProtocolVersion3 = 1;
export type RequestId3 = string;
export type ClientId3 = string;
export type Timestamp3 = string;
export type Type3 = "command.stop";
export type Mode1 = "after_current_agent_call";
export type ProtocolVersion4 = 1;
export type RequestId4 = string;
export type ClientId4 = string;
export type Timestamp4 = string;
export type Type4 = "query.snapshot";
export type ExpectedStoreId = string | null;
export type ProtocolVersion5 = 1;
export type RequestId5 = string;
export type ClientId5 = string;
export type Timestamp5 = string;
export type Type5 = "query.chat";
export type Text1 = string;
export type ChatThreadId = string;
export type ProtocolVersion6 = 1;
export type RequestId6 = string;
export type ClientId6 = string;
export type Timestamp6 = string;
export type Type6 = "query.chat_thread_create";
export type Provider = string;
export type Model = string;
export type Title = string | null;
export type ProtocolVersion7 = 1;
export type RequestId7 = string;
export type ClientId7 = string;
export type Timestamp7 = string;
export type Type7 = "query.history";
export type ProtocolVersion8 = 1;
export type RequestId8 = string;
export type ClientId8 = string;
export type Timestamp8 = string;
export type Type8 = "query.performance";
export type ProtocolVersion9 = 1;
export type RequestId9 = string;
export type ClientId9 = string;
export type Timestamp9 = string;
export type Type9 = "query.experiments";
export type RunId = string;
export type ProjectionId = string;
export type Revision = number;
export type ProtocolVersion10 = 1;
export type RequestId10 = string;
export type ClientId10 = string;
export type Timestamp10 = string;
export type Type10 = "query.iterations";
export type ExpectedStoreId1 = string | null;
export type ParentId = string | null;
export type Cursor = string | null;
export type Limit = number;
export type ProtocolVersion11 = 1;
export type RequestId11 = string;
export type ClientId11 = string;
export type Timestamp11 = string;
export type Type11 = "query.design";
export type ProtocolVersion12 = 1;
export type RequestId12 = string;
export type ClientId12 = string;
export type Timestamp12 = string;
export type Type12 = "query.design_patch";
export type Base = string;
export type Head = string;
export type Path = string;
export type ProtocolVersion13 = 1;
export type RequestId13 = string;
export type ClientId13 = string;
export type Timestamp13 = string;
export type Type13 = "query.events";
export type ExpectedStoreId2 = string | null;
export type AfterSequence = number;
export type BeforeSequence = number | null;
export type TimeoutMs = number;
export type ProtocolVersion14 = 1;
export type RequestId14 = string;
export type ClientId14 = string;
export type Timestamp14 = string;
export type Type14 = "subscribe";
export type AfterSequence1 = number;
export type Tail = number | null;
export type StoreId = string;
export type ProtocolVersion15 = 1;
export type RequestId15 = string;
export type ClientId15 = string;
export type Timestamp15 = string;
export type Type15 = "command.control";
export type ControlRequest =
  | SteerRequest
  | PauseRequest
  | ResumeRequest
  | StopRequest
  | WithdrawRequest
  | CancelAttemptRequest
  | CancelItemRequest
  | SetMaxParallelRequest;
export type ControlId = string;
export type Action = "steer";
export type Text2 = string;
/**
 * What a steer is addressed to.
 */
export type SteerTargetKind = "run" | "role" | "attempt" | "item";
export type SubjectId = string;
/**
 * How a steer reaches the turns of its target.
 */
export type SteerMode = "next_turn" | "standing" | "interrupt";
export type ControlId1 = string;
export type Action1 = "pause";
export type ControlId2 = string;
export type Action2 = "resume";
export type Fallback1 = boolean;
export type When1 = "now" | "capacity_returns";
export type ControlId3 = string;
export type Action3 = "stop";
/**
 * How a stop treats work in flight: let it finish, or cancel it.
 */
export type StopMode = "drain" | "cancel";
export type ControlId4 = string;
export type Action4 = "withdraw";
export type Withdraws = string;
export type ControlId5 = string;
export type Action5 = "cancel_attempt";
export type ControlId6 = string;
export type Action6 = "cancel_item";
export type ControlId7 = string;
export type Action7 = "set_max_parallel";
export type MaxParallel = number;
export type ProtocolVersion16 = 1;
export type RequestId16 = string;
export type ClientId16 = string;
export type Timestamp16 = string;
export type Ok = boolean;
export type Error = string | null;
export type Id = string;
export type Code = string;
export type Summary = string;
export type Detail = string | null;
export type Hint = string | null;
/**
 * Boundary at which a diagnostic was raised.
 */
export type DiagnosticScope = "configuration" | "invocation" | "phase" | "run" | "request" | "protocol" | "transport";
/**
 * Operator-visible seriousness of a diagnostic.
 */
export type DiagnosticSeverity = "warning" | "error" | "fatal";
/**
 * Whether retrying the failed operation is expected to help.
 */
export type DiagnosticRetryability = "automatic" | "manual" | "never" | "unknown";
export type CauseId = string | null;
export type DebugRef = string | null;
export type Source = string | null;
export type ValidationPaths = (string | number)[][] | null;
/**
 * What a control request asks for; the tag of the ``ControlRequest`` union.
 */
export type ControlAction =
  "steer" | "pause" | "resume" | "stop" | "withdraw" | "cancel_attempt" | "cancel_item" | "set_max_parallel";
export type Status = "pending" | "consumed";
export type Question = string;
export type Answer = string;
export type Effect = "none";
export type Title1 = string;
export type Provider1 = string;
export type Model1 = string;
export type ProtocolVersion17 = 1;
export type RunId1 = string;
export type ProjectScope = string | null;
export type Sequence = number;
/**
 * Lifecycle status of one run, as frontends observe it.
 *
 * This is the authoritative closed set for the ``status`` field of
 * ``RunSnapshot`` and of ``RunStatusChangedData``; the generated TypeScript
 * protocol types derive their union from it.
 *
 * ``PAUSING`` and ``PAUSED`` are distinct because a pause is only applied at
 * an invocation boundary: ``/pause`` records the request, and the run keeps
 * executing the call already in flight until it reaches that boundary.
 * ``STOPPING`` and ``STOPPED`` split the same way for ``/stop``, whose
 * boundary is where the run ends instead of where it parks.
 */
export type RunStatus = "starting" | "running" | "pausing" | "paused" | "stopping" | "stopped" | "completed" | "failed";
export type AgentKind = string | null;
export type RoundLabel = string | null;
export type ExecutionId = string;
export type AgentKind1 = string;
export type RoundLabel1 = string;
export type Stage = string;
export type Attempt = number | null;
export type Assignment = string;
export type StartedAt = string;
export type Kind = "agent_execution_activity_changed";
export type Mode2 = "thinking" | "responding" | "tool" | "waiting";
export type Summary1 = string;
export type Tool = string | null;
export type Provider2 = string | null;
export type Model2 = string | null;
export type ActiveExecutions = ActiveAgentExecution[];
export type ChatThreads = ChatThreadInfo[];
export type SpanId = string;
/**
 * What a span stands for. Closed.
 */
export type SpanType = "flow" | "operation";
export type Name = string;
/**
 * Where a span is in its lifecycle. Closed.
 */
export type SpanStatus = "pending" | "running" | "waiting" | "done" | "failed" | "stopped";
/**
 * What a flow waits on. Closed.
 */
export type SpanWaitKind = "operation" | "slot" | "time" | "stop";
export type Ref = string;
export type Seconds = number;
export type WaitingOn = WaitingOn1[];
export type N = number;
export type Of = number;
/**
 * How one try of ``ctx.retry`` ended. Closed.
 */
export type AttemptOutcome = "done" | "again" | "gave_up";
export type Reason = string | null;
export type Attempts = SpanAttempt[];
/**
 * flows.md's failure kinds a span can end with. Closed.
 *
 * A defect crashes the step that raised it, which is never persisted, so no trace holds one.
 */
export type FailureKind = "outcome" | "interruption";
export type Message = string;
export type Landed = boolean;
export type Invocation = string | null;
export type Kind1 = string;
export type Title2 = string | null;
export type Rel = string;
export type Links = SpanLink[];
export type AttrsJson = string | null;
export type Verdict = string | null;
export type Reason1 = string | null;
export type Spans = Span[];
export type OverviewVersion = 1;
export type Text3 = string;
export type Metric = string | null;
export type Unit = string | null;
export type Direction = ("max" | "min") | null;
export type Name1 = string;
export type Direction1 = "max" | "min";
export type Objectives = TaskObjective[];
export type CorrectnessCheck = string[];
export type Name2 = string;
export type Version = string;
export type ProtocolVersion18 = 1;
export type Sequence1 = number;
export type RunId2 = string;
export type Timestamp17 = string;
/**
 * Wire event kinds persisted in the event log.
 */
export type EventType =
  | "server_started"
  | "server_ready"
  | "configuration_failed"
  | "run_started"
  | "run_startup_progress"
  | "experiments_changed"
  | "iterations_changed"
  | "run_interrupted"
  | "run_status_changed"
  | "chat"
  | "chat_thread_created"
  | "steer_proposed"
  | "steer_delivery"
  | "status_query"
  | "control"
  | "invocation_started"
  | "invocation_finished"
  | "agent_execution_started"
  | "agent_execution_activity_changed"
  | "agent_execution_finished"
  | "phase_started"
  | "phase_finished"
  | "agent_output_chunk"
  | "subprocess_output"
  | "judge_result"
  | "benchmark_result"
  | "round_finished"
  | "run_finished"
  | "run_failed"
  | "run_ending"
  | "output"
  | "tool_call"
  | "tool_result"
  | "todo_update"
  | "usage_update"
  | "rate_limit_update"
  | "quota_paused"
  | "quota_resumed"
  | "quota_abandoned"
  | "quota_waiting"
  | "quota_wait_cancelled"
  | "provider_switched"
  | "gate_started"
  | "gate_finished"
  | "workspace_snapshot"
  | "run_configured"
  | "framework_warning"
  | "winner_selected"
  | "measurement_compared"
  | "comparisons_read"
  | "workstream_finished"
  | "turn_usage_recorded"
  | "span_started"
  | "span_updated"
  | "span_ended";
export type Text4 = string;
/**
 * Lifecycle and command states reported in events.
 */
export type EventStatus =
  "active" | "answered" | "pending" | "consumed" | "delivered" | "completed" | "failed" | "cancelled" | "interrupted";
export type RoundLabel2 = string | null;
export type AgentKind2 = string | null;
export type InvocationId = string | null;
export type ExecutionId1 = string | null;
export type Data =
  | (
      | ChatData
      | ChatThreadCreatedData
      | InvocationStartedData
      | InvocationFinishedData
      | AgentExecutionStartedData
      | AgentExecutionActivityData
      | AgentExecutionFinishedData
      | OutputData
      | ServerReadyData
      | RunStartedData
      | RunStartupProgressData
      | RunFailedData
      | RunEndingData
      | RunInterruptedData
      | RunStatusChangedData
      | ExperimentsChangedData
      | IterationsChangedData
      | ConfigurationFailedData
      | PhaseData
      | AgentOutputChunkData
      | SubprocessOutputData
      | JudgeResultData
      | BenchmarkResultData
      | RoundFinishedData
      | ToolCallData
      | ToolResultData
      | SteerProposedData
      | SteerDeliveryData
      | TodoUpdateData
      | UsageUpdateData
      | RateLimitUpdateData
      | QuotaPausedData
      | QuotaResumedData
      | QuotaAbandonedData
      | QuotaWaitingData
      | QuotaWaitCancelledData
      | ProviderSwitchedData
      | GateStartedData
      | GateFinishedData
      | WorkspaceSnapshotData
      | RunConfiguredData
      | FrameworkWarningData
      | WinnerSelectedData
      | MeasurementComparedData
      | ComparisonsReadData
      | WorkstreamFinishedData
      | TurnUsageData
      | SpanStartedData
      | SpanUpdatedData
      | SpanEndedData
    )
  | null;
export type Kind2 = "chat";
export type Answer1 = string;
export type ThreadTitle = string | null;
export type InvocationId1 = string | null;
export type Kind3 = "chat_thread_created";
export type Title3 = string;
export type Provider3 = string;
export type Model3 = string;
export type CreatedAt = string;
export type Kind4 = "invocation_started";
export type SystemPrompt = string;
export type UserPrompt = string;
export type Kind5 = "invocation_finished";
export type Error1 = string | null;
export type Kind6 = "agent_execution_started";
export type Stage1 = string;
export type Attempt1 = number | null;
export type SystemPrompt1 = string;
export type UserPrompt1 = string;
export type Provider4 = string | null;
export type Model4 = string | null;
export type ReasoningEffort = string | null;
export type HarnessVersion = string | null;
export type Kind7 = "agent_execution_finished";
export type Error2 = string | null;
export type Kind8 = string;
export type Kind9 = "output";
export type Stream = "stdout" | "stderr";
export type Source1 = string;
export type Content = string;
export type Kind10 = "server_ready";
export type SocketProtocol = "jsonl";
export type Kind11 = "run_started";
export type OuterLoop = string;
export type Input = string;
export type MaxRounds = number | null;
export type ExpectedRoles = string[];
export type Kind12 = "run_startup_progress";
/**
 * Where a starting run is, in the order it gets there.
 *
 * Declaration order is the order: a starting run only moves to a later stage.
 */
export type StartupStage =
  "resolving" | "probing_workspace" | "building_image" | "building_tools" | "starting_container";
export type Done = number;
export type Total = number | null;
export type Label = string;
/**
 * What a startup stage is doing at the moment of one observation.
 */
export type StartupState = "running" | "waiting" | "finished";
/**
 * What a waiting startup stage waits on.
 */
export type StartupWaitCondition = "queued" | "running";
export type ElapsedSeconds = number | null;
export type Kind13 = "run_failed";
/**
 * Why a run failed to meet its goal, whether or not it preserved a partial result.
 */
export type RunFailureKind = "budget_exhausted" | "deadline" | "no_result";
export type Reason2 = string;
export type WorkstreamsStarted = number;
export type WorkstreamBudget = number;
export type CandidatesKept = number;
export type AdoptedCandidate = string | null;
export type Kind14 = "run_ending";
/**
 * Why the framework ended the run as it did. Closed: the result's reason starts with it.
 */
export type EndReason =
  "adopted" | "kept-input" | "no-winner" | "adoption-failed" | "stopped" | "deadline" | "finish-refused";
export type Summary2 = string;
export type Resumable = boolean;
export type RoundsFinished = number;
export type Kind15 = "run_interrupted";
export type Reason3 = string;
export type Signal = string | null;
export type Kind16 = "run_status_changed";
export type Kind17 = "experiments_changed";
export type Reason4 = "project_attached" | "active_hypothesis_changed" | "round_persisted";
export type Revision1 = number | null;
export type Kind18 = "iterations_changed";
export type Revision2 = string | null;
export type Kind19 = "configuration_failed";
export type Code1 = string;
export type Stage2 = string;
export type Message1 = string;
export type Usage = string | null;
export type ExitCode = number;
export type Kind20 = "phase";
export type Phase = string;
export type Attempt2 = number | null;
export type Kind21 = "agent_output_chunk";
export type Channel = "assistant" | "analysis" | "tool" | "diagnostic" | "prompt";
export type Content1 = string;
export type Progress = string | null;
export type AgentLabel = string | null;
export type ElapsedSeconds1 = number;
export type InputTokens = number;
export type ContextWindow = number | null;
export type Kind22 = "subprocess_output";
export type ProcessId = string;
export type ProcessKind = string;
export type Stream1 = "stdout" | "stderr";
export type Content2 = string;
export type Kind23 = "judge_result";
export type Verdict1 = "pass" | "fail";
export type Feedback = string;
export type Attempt3 = number;
export type Skipped = boolean;
export type SkipReason = string | null;
export type ImplementerOutcome =
  ("continue" | "supported" | "nominated" | "disproven" | "implementation_failed") | null;
export type Kind24 = "benchmark_result";
export type Metric1 = string;
export type Value1 = number;
export type Unit1 = string;
export type Kind25 = "round_finished";
export type Attempts1 = number;
export type JudgeVerdict = "pass" | "fail" | "skipped";
export type PerfMetric = number | null;
export type PerfUnit = string | null;
export type ProfileSkipped = boolean;
export type Kind26 = "tool_call";
export type Tool1 = string;
export type CallId = string | null;
export type Kind27 = "tool_result";
export type Tool2 = string;
export type CallId1 = string | null;
export type Content3 = string;
export type IsError = boolean;
export type Payload = (CommandResultPayload | JsonResultPayload) | null;
export type Kind28 = "command";
export type Stdout = string;
export type Stderr = string;
export type ExitCode1 = number | null;
export type Duration = number | null;
export type Kind29 = "json";
export type Value2 =
  | {
      [k: string]: unknown;
    }
  | unknown[];
export type Kind30 = "steer_proposed";
export type ProposalId = string;
export type Text5 = string;
/**
 * How a steer reaches the turns of its target.
 */
export type SteerMode1 = "next_turn" | "standing" | "interrupt";
export type Kind31 = "steer_delivery";
/**
 * Where one operator steer or withdrawal stands, as a client shows it.
 */
export type SteerDeliveryState =
  "queued" | "withdraw_queued" | "reached" | "reached_mid_turn" | "in_force" | "dropped" | "ended" | "refused";
export type ControlId8 = string | null;
export type Action8 = "steer" | "withdraw";
/**
 * How a steer reaches the turns of its target.
 */
export type SteerMode2 = "next_turn" | "standing" | "interrupt";
export type Withdraws1 = string | null;
export type Role = string | null;
export type AttemptId = string | null;
export type Reason5 = string | null;
export type Kind32 = "todo_update";
export type Content4 = string;
export type Status1 = string;
export type Todos = TodoItemData[];
export type Kind33 = "usage_update";
export type InputTokens1 = number;
export type ContextWindow1 = number | null;
export type Model5 = string | null;
export type Kind34 = "rate_limit_update";
export type Provider5 = string | null;
export type Window = string | null;
export type Limit1 = string | null;
export type UsedFraction = number | null;
export type ResetsAt = number | null;
export type WindowMinutes = number | null;
export type Exhausted = boolean;
export type Kind35 = "quota_paused";
export type Provider6 = string;
export type Condition = "quota_exhausted" | "rate_limited" | "unavailable";
export type Detail1 = string;
export type ResetsAt1 = number | null;
export type ResumesAt = number | null;
export type Policy = "pause" | "wait" | "fail" | "fallback";
export type FallbackProvider = string | null;
export type FallbackModel = string | null;
export type Kind36 = "quota_resumed";
export type Provider7 = string;
export type Condition1 = ("quota_exhausted" | "rate_limited" | "unavailable") | null;
export type Reason6 = "operator" | "wait_elapsed";
export type WaitedSeconds = number;
export type Kind37 = "quota_abandoned";
export type Provider8 = string;
export type Condition2 = "quota_exhausted" | "rate_limited" | "unavailable";
export type Detail2 = string;
export type Reason7 = string;
export type WaitedSeconds1 = number;
export type Kind38 = "quota_waiting";
export type Provider9 = string;
export type ResumesAt1 = number | null;
export type Kind39 = "quota_wait_cancelled";
export type Provider10 = string;
export type Reason8 = "resumed" | "paused";
export type Kind40 = "provider_switched";
export type FromProvider = string;
export type ToProvider = string;
export type ToModel = string;
export type Reason9 = "policy" | "operator";
export type Detail3 = string;
export type Kind41 = "gate_started";
/**
 * Closed set of framework-owned gates a candidate passes through.
 */
export type GateKind = "validation" | "build" | "accuracy" | "benchmark";
export type Recipe = string | null;
export type Command = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel = string | null;
export type Kind42 = "gate_finished";
export type Recipe1 = string | null;
export type Reused = boolean;
export type Metric2 = string | null;
export type Value3 = number | null;
export type Unit2 = string | null;
export type OutputTail = string | null;
export type RoundLabel3 = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource1 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel1 = string | null;
export type Kind43 = "workspace_snapshot";
export type Label1 = string;
export type Commit = string | null;
export type Baseline = string | null;
export type ExcludedPaths = string[];
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource2 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type Kind44 = "run_configured";
export type RunLogPath = string;
export type ProjectRoot = string;
export type Model6 = string | null;
export type Objective = string | null;
export type SearchPolicy = string | null;
export type BenchmarkContract = boolean;
export type ParetoObjectives = string | null;
export type MaxContinuationRounds = number | null;
export type MaxUnmeasuredTurns = number | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource3 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type Kind45 = "framework_warning";
export type Summary3 = string;
export type Detail4 = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource4 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel2 = string | null;
export type Kind46 = "winner_selected";
export type Selection = "candidate" | "baseline";
export type Revision3 = string;
export type SettlementId = string | null;
export type Kind47 = "measurement_compared";
export type Metric3 = string;
export type Unit3 = string | null;
export type Direction2 = "max" | "min";
export type CandidateCommit = string;
export type BaseCommit = string;
export type CandidateValue = number;
export type BaseValue = number;
export type Ratio = number;
export type Noise = number;
/**
 * Which side of the noise a candidate falls on. Mirrors ``vs_core.api.ComparisonVerdict``.
 */
export type MeasurementVerdict = "better" | "within_noise" | "worse" | "inconclusive";
export type CandidateSamples = number;
export type BaseSamples = number;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource5 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type Kind48 = "comparisons_read";
export type Name3 = string;
export type Label2 = string;
/**
 * What a run knows of one comparison baseline. Closed; mirrored by ``server.events``.
 */
export type ComparisonStatus = "measured" | "failed" | "not_configured" | "not_measured";
export type Metric4 = string | null;
export type Unit4 = string | null;
export type Direction3 = ("max" | "min") | null;
export type Value4 = number | null;
export type Spread = number | null;
export type Samples = number | null;
export type Hosts = string[];
export type Reason10 = string | null;
export type Comparisons = ComparisonReadingData[];
export type Kind49 = "workstream_finished";
export type SetupFailure = boolean;
export type Refunded = boolean;
export type RepeatedError = boolean;
/**
 * Why a workstream ended without a measured candidate. Closed.
 *
 * See ``vibesys.events.WorkstreamFailureKind``, which this mirrors
 * member-for-member.
 */
export type WorkstreamFailureKind =
  | "stopped"
  | "parent-unverified"
  | "refused"
  | "ended"
  | "out-of-budget"
  | "turn-failed"
  | "invalid-reply"
  | "render-failed"
  | "wait-refused"
  | "checkpoint-declined"
  | "no-changed-candidate"
  | "implementation-failed"
  | "review-rejected"
  | "no-trusted-evidence"
  | "evidence-refused"
  | "evidence-undecodable"
  | "profile-capture-unavailable"
  | "turn-limit"
  | "repeated-turn";
export type Role1 = string | null;
export type Turns = number | null;
export type Limit2 = number | null;
export type Text6 = string;
export type Kind50 = "turn_usage_recorded";
export type InputTokens2 = number | null;
export type CacheCreationInputTokens = number | null;
export type CacheReadInputTokens = number | null;
export type OutputTokens = number | null;
export type ReasoningOutputTokens = number | null;
export type TotalCostUsd = number | null;
export type DurationMs = number | null;
export type Kind51 = "span_started";
export type Kind52 = "span_updated";
export type Kind53 = "span_ended";
/**
 * True when the recording boundary cut variable-size payload fields to keep this event within MAX_SERIALIZED_RUN_EVENT_BYTES.
 */
export type Truncated = boolean;
export type Events = RunEvent[];
export type Round = number;
export type PerfMetric1 = number;
export type PerfUnit1 = string;
export type Passed = boolean;
export type ProfileSkipped1 = boolean;
export type Performance = PerformanceRound[];
export type ObjectiveMetric = string | null;
export type ObjectiveUnit = string | null;
export type ObjectiveDirection = ("max" | "min") | null;
export type ObjectiveBaselineValue = number | null;
export type ObjectiveBaselineRound = number | null;
export type ObjectiveBaselineCommit = string | null;
export type ObjectiveDescription = string | null;
export type HypothesisId = string;
export type Identified = boolean;
export type Title4 = string | null;
export type Claim = string | null;
export type Action9 = string | null;
export type FirstRound = number;
export type LastRound = number;
export type Round1 = number;
export type Passed1 = boolean;
export type Reviewed = boolean;
export type HypothesisOutcome = HypothesisOutcome1 | HypothesisResolution | null;
/**
 * Implementer-owned status for the active experimental hypothesis.
 *
 * ``SUPPORTED`` and ``NOMINATED`` are deliberately distinct from
 * ``PROVEN``: an implementer may submit evidence for independent review,
 * but only the judge can establish that the scoped hypothesis held.
 * ``NOMINATED`` additionally asks the framework to run its global gates for
 * the current candidate checkpoint. It does not imply that the overall
 * objective or terminal target has been achieved.
 */
export type HypothesisOutcome1 =
  "continue" | "supported" | "nominated" | "disproven" | "implementation_failed" | "inconclusive" | "blocked";
/**
 * Framework-owned resolution after all available evidence is known.
 */
export type HypothesisResolution =
  "proven" | "disproven" | "inconclusive" | "implementation_failed" | "blocked" | "rejected" | "unmeasured";
export type JudgeVerdict1 = ("pass" | "fail" | "deferred") | null;
export type PerfMetric2 = number | null;
export type PerfUnit2 = string | null;
export type PerfDeltaPct = number | null;
export type Commit1 = string | null;
export type OfficialEvaluation = boolean;
/**
 * How a measured candidate should be retained independently of its hypothesis.
 *
 * Hypothesis truth and checkpoint utility are different questions. A causal
 * forecast can be disproven while its implementation still establishes a
 * useful throughput/latency tradeoff. These values keep that distinction
 * explicit without promoting provisional evidence to an official result.
 */
export type CandidateDisposition = "unassessed" | "discard" | "prerequisite" | "pareto_frontier";
export type Rounds = HypothesisRound[];
export type ResolvedOutcome = string | null;
export type JudgeVerdict2 = ("pass" | "fail") | null;
export type PerfMetric3 = number | null;
export type PerfUnit3 = string | null;
export type PerfDeltaPct1 = number | null;
export type PerfMetricName = string | null;
export type PerfDirection = ("max" | "min") | null;
export type PerfBaselineValue = number | null;
export type PerfBaselineRound = number | null;
export type PerfBaselineCommit = string | null;
/**
 * Why a headline measurement carries no causal delta.
 *
 * Always re-derived from round evidence (``perf_provenance`` and the
 * baseline fields), never stored on the round record, so it cannot drift
 * from them. Absent entirely for records that predate provenance tracking:
 * a legacy absolute number keeps reading as a deliberate absolute rather
 * than being relabelled as unresolved.
 */
export type PerfDeltaReason = "no_baseline_yet" | "baseline_unresolved" | "not_framework_measured";
export type Best = boolean | null;
export type StrategyDisposition = ("available" | "parked" | "abandoned") | null;
export type StrategyReason = string | null;
export type Active = boolean;
export type Experiments = HypothesisEntry[];
export type RunId3 = string;
export type ProjectionId1 = string;
export type FromRevision = number | null;
export type ThroughRevision = number;
export type Reset = boolean;
export type RemovedHypothesisIds = string[];
export type ExperimentsReady = boolean | null;
export type Iterations = IterationSlice | IterationUnavailable | null;
export type Kind54 = "page";
export type SchemaVersion = 1;
export type RunId4 = string;
export type ProjectionRevision = string;
export type ParentId1 = string | null;
export type FromCursor = string | null;
export type Id1 = string;
export type ParentId2 = string | null;
/**
 * Policy-neutral kinds a client may use to label navigation rows.
 */
export type IterationKind = "round" | "workstream" | "attempt" | "generation" | "candidate" | "board" | "item";
export type Label3 = string;
export type Ordinal = number;
/**
 * Lifecycle shared by every policy's iteration projection.
 */
export type IterationStatus = "pending" | "active" | "completed" | "failed" | "cancelled";
export type ChildCount = number;
export type Id2 = string;
export type Value5 = number | null;
export type Unit5 = string | null;
export type Direction4 = ("max" | "min") | null;
export type HeadlineMetrics = IterationMetric[];
export type Items = IterationSummary[];
export type NextCursor = string | null;
export type Kind55 = "unavailable";
export type SchemaVersion1 = 1;
/**
 * Closed reasons why a policy cannot expose iteration navigation.
 */
export type IterationUnavailableReason =
  "external_state_unavailable" | "policy_not_mapped" | "projection_invalid" | "state_unavailable";
export type Detail5 = string | null;
export type IterationsReady = boolean | null;
export type Round2 = number;
export type Commit2 = string | null;
export type Base1 = string | null;
export type Files = DesignFileChange[] | null;
export type Path1 = string;
export type Change = "added" | "modified" | "deleted" | "renamed";
export type RenamedFrom = string | null;
export type Design = DesignRound[];
export type DesignReady = boolean | null;
export type Base2 = string;
export type Head1 = string;
export type Path2 = string;
export type RenamedFrom1 = string | null;
export type Patch = string | null;
export type Truncated1 = boolean;
export type ServerMessage =
  SubscribedMessage | EventMessage | EventBatchMessage | ActiveExecutionsChunkMessage | ProtocolErrorMessage;
export type Type16 = "subscribed";
export type RequestId17 = string;
export type ClientId17 = string;
export type RunId5 = string;
export type LatestSequence = number;
export type Type17 = "event";
/**
 * Event batch and cursor metadata sent to subscribers.
 */
export type EventBatchMessage = EventBatchMessage1 | EventBatchMessage2;
export type Type18 = "event_batch";
export type Events1 = RunEvent[];
export type ThroughSequence = number;
export type ActiveExecutionsCheckpointId = null;
export type StoreId1 = string;
export type HistoryAfterSequence = number;
export type Rebootstrap = boolean;
export type Type19 = "event_batch";
export type Events2 = RunEvent[];
export type ThroughSequence1 = number;
export type StoreId2 = string;
export type HistoryAfterSequence1 = number;
export type Rebootstrap1 = boolean;
export type Type20 = "active_executions_chunk";
export type CheckpointId = string;
export type ChunkIndex = number;
export type ChunkCount = number;
export type Payload1 = string;
export type ThroughSequence2 = number;
export type StoreId3 = string;
export type HistoryAfterSequence2 = number;
export type Type21 = "protocol_error";
export type RequestId18 = string | null;
export type ClientId18 = string;
export type Code2 = string;
export type Message2 = string;
export type LogOwner = StartupStep | GateStage | ToolBuild | RunNarration;
export type Kind56 = "startup_step";
/**
 * Where an opening environment is, in the order it gets there.
 */
export type EnvironmentStartupStage = "probing_workspace" | "building_image" | "building_tools" | "starting_container";
export type Kind57 = "gate_stage";
export type ExecutionId2 = string;
export type Attempt4 = number;
export type Kind58 = "tool_build";
export type Name4 = string;
export type Kind59 = "run_narration";
export type At = string;
/**
 * A line's severity: the standard ``logging`` level at or below the record's.
 */
export type NarrationLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
export type Logger = string;
export type Thread = string;
export type Message3 = string;
export type Traceback = string | null;
export type Candidate = string | null;
export type MetricValue = number | null;
export type VsBaseline = number | null;
export type Agent = string;
export type Role2 = string;
export type Model7 = string | null;
export type StartedBy = string[];
export type Stage3 = string;
export type MetricValue1 = number | null;
export type VsBaseline1 = number | null;
export type Name5 = string;
export type Finite = number;
export type Direction5 = "max" | "min";
export type Unit6 = string | null;
export type InputMetrics = MetricRow[];
export type Metrics = MetricRow[];
/**
 * Closed measurement kinds used for eligibility requirements.
 */
export type EvidenceKind = "correctness" | "benchmark" | "profiling";
export type Completed = number | null;
export type Required = number | null;
export type Unit7 = string | null;
export type Call = number;
export type HypothesisId1 = string | null;
export type ParentRevision = string | null;
export type BaseRound = number | null;
export type Round3 = number | null;
export type Idea = string | null;
export type Revision4 = string | null;
export type Outcome = string | null;
export type Summary4 = string | null;
export type Probe = boolean;

export interface ProtocolDocument {
  request: Request;
  response: Response;
  event: RunEvent;
  snapshot: RunSnapshot;
  server_message: ServerMessage;
  log_owner: LogOwner;
  narration_line: NarrationLine;
  span_attributes?: SpanAttributes;
}
/**
 * Request pausing after the active agent call.
 */
export interface PauseCommand {
  protocol_version?: ProtocolVersion;
  request_id?: RequestId;
  client_id?: ClientId;
  timestamp: Timestamp;
  type: Type;
  mode?: Mode;
}
/**
 * Request resuming a paused run.
 */
export interface ResumeCommand {
  protocol_version?: ProtocolVersion1;
  request_id?: RequestId1;
  client_id?: ClientId1;
  timestamp: Timestamp1;
  type: Type1;
  fallback?: Fallback;
  when?: When;
}
/**
 * Send steering text to the active run.
 */
export interface SteerCommand {
  protocol_version?: ProtocolVersion2;
  request_id?: RequestId2;
  client_id?: ClientId2;
  timestamp: Timestamp2;
  type: Type2;
  text: Text;
}
/**
 * Request stopping after the active agent call.
 */
export interface StopCommand {
  protocol_version?: ProtocolVersion3;
  request_id?: RequestId3;
  client_id?: ClientId3;
  timestamp: Timestamp3;
  type: Type3;
  mode?: Mode1;
}
/**
 * Request the current run snapshot.
 *
 * ``expected_store_id`` fences the read to the event store whose batch established a
 * client's sequence space. None preserves the unfenced request used by
 * clients that do not combine a snapshot with a live subscription. An empty
 * string is a real fence for the pre-attachment store, not an alias for None.
 */
export interface SnapshotQuery {
  protocol_version?: ProtocolVersion4;
  request_id?: RequestId4;
  client_id?: ClientId4;
  timestamp: Timestamp4;
  type: Type4;
  expected_store_id?: ExpectedStoreId;
}
/**
 * Send a message to an experiment-chat thread.
 */
export interface ChatQuery {
  protocol_version?: ProtocolVersion5;
  request_id?: RequestId5;
  client_id?: ClientId5;
  timestamp: Timestamp5;
  type: Type5;
  text: Text1;
  thread_id?: ChatThreadId | null;
}
/**
 * Create a new experiment-chat thread with its own agent selection.
 *
 * ``harness`` is a provider and model pair from the helper's ``harnesses.list`` catalogue,
 * the same one New run draws on. Omitted, the thread runs the run's own provider and model.
 * The response carries the resolved settings and thread identity.
 */
export interface ChatThreadCreateQuery {
  protocol_version?: ProtocolVersion6;
  request_id?: RequestId6;
  client_id?: ClientId6;
  timestamp: Timestamp6;
  type: Type6;
  harness?: HarnessChoice | null;
  title?: Title;
}
/**
 * One agent CLI provider and one model it runs.
 */
export interface HarnessChoice {
  provider: Provider;
  model: Model;
}
/**
 * Request persisted run history.
 */
export interface HistoryQuery {
  protocol_version?: ProtocolVersion7;
  request_id?: RequestId7;
  client_id?: ClientId7;
  timestamp: Timestamp7;
  type: Type7;
}
/**
 * Request the run's performance history.
 */
export interface PerformanceQuery {
  protocol_version?: ProtocolVersion8;
  request_id?: RequestId8;
  client_id?: ClientId8;
  timestamp: Timestamp8;
  type: Type8;
}
/**
 * Request the hypothesis-level experiment log for the attached run.
 */
export interface ExperimentQuery {
  protocol_version?: ProtocolVersion9;
  request_id?: RequestId9;
  client_id?: ClientId9;
  timestamp: Timestamp9;
  type: Type9;
  after?: ExperimentCursor | null;
}
/**
 * Client's last completely applied experiment projection.
 */
export interface ExperimentCursor {
  run_id: RunId;
  projection_id: ProjectionId;
  revision: Revision;
}
/**
 * Request one bounded page of roots or direct children.
 *
 * ``expected_store_id`` fences every page to the event store whose stream
 * established the navigation query. None preserves the unfenced request for
 * clients that do not combine iteration navigation with a live subscription.
 * An empty string is a real fence for the pre-attachment store.
 */
export interface IterationQuery {
  protocol_version?: ProtocolVersion10;
  request_id?: RequestId10;
  client_id?: ClientId10;
  timestamp: Timestamp10;
  type: Type10;
  expected_store_id?: ExpectedStoreId1;
  parent_id?: ParentId;
  cursor?: Cursor;
  limit?: Limit;
}
/**
 * Request the per-round design log for the attached run.
 *
 * The design log is the operator's view of what each round changed in the
 * system under optimization: the files the round touched and how each stage
 * of the round concluded.
 */
export interface DesignQuery {
  protocol_version?: ProtocolVersion11;
  request_id?: RequestId11;
  client_id?: ClientId11;
  timestamp: Timestamp11;
  type: Type11;
}
/**
 * Request one file's unified patch from a round's commit range.
 *
 * ``base`` and ``head`` are a round's own range exactly as ``query.design``
 * published it (``DesignRound.base`` and ``DesignRound.commit``), and
 * ``path`` must be one of that round's listed file changes. The server
 * validates all three, so a client cannot diff arbitrary revisions or read
 * paths the design log filtered out.
 */
export interface DesignPatchQuery {
  protocol_version?: ProtocolVersion12;
  request_id?: RequestId12;
  client_id?: ClientId12;
  timestamp: Timestamp12;
  type: Type12;
  base: Base;
  head: Head;
  path: Path;
}
/**
 * Request run events in a sequence interval.
 *
 * ``expected_store_id`` fences the interval to the event store whose stream
 * established the caller's history floor. None preserves the unfenced query
 * for callers that do not combine history with a live subscription. An empty
 * string is a real fence for the pre-attachment store.
 */
export interface EventsQuery {
  protocol_version?: ProtocolVersion13;
  request_id?: RequestId13;
  client_id?: ClientId13;
  timestamp: Timestamp13;
  type: Type13;
  expected_store_id?: ExpectedStoreId2;
  after_sequence?: AfterSequence;
  before_sequence?: BeforeSequence;
  timeout_ms?: TimeoutMs;
}
/**
 * Subscribe to run events, optionally replaying a recent tail.
 */
export interface SubscribeRequest {
  protocol_version?: ProtocolVersion14;
  request_id?: RequestId14;
  client_id?: ClientId14;
  timestamp: Timestamp14;
  type: Type14;
  after_sequence?: AfterSequence1;
  tail?: Tail;
  store_id?: StoreId;
}
/**
 * Send one operator control in the schema every control ingress shares.
 *
 * ``control`` is a ``vibesys.api.ControlRequest``, the request the headless inbox carries
 * too, so a client and ``vibesys control`` say the same things. The single-action commands
 * above remain; the server turns each into the same request. A server that predates this
 * command rejects it (``WP-CAPABILITY-PROBE``).
 */
export interface ControlCommand {
  protocol_version?: ProtocolVersion15;
  request_id?: RequestId15;
  client_id?: ClientId15;
  timestamp: Timestamp15;
  type: Type15;
  control: ControlRequest;
}
/**
 * Free-text guidance for the agent turns of its target, the whole run by default.
 *
 * With ``mode`` ``next_turn`` the message reaches exactly one turn of the target, once; with
 * ``standing`` it reaches every turn of the target until withdrawn; with ``interrupt`` it
 * reaches a running turn of the target now, or else the next one. A target the run cannot
 * resolve (a role the search does not declare, an attempt or item it does not hold, or an
 * attempt that has ended) is refused, and the message reaches no one.
 */
export interface SteerRequest {
  control_id: ControlId;
  action: Action;
  text: Text2;
  target?: SteerTarget;
  mode?: SteerMode;
}
/**
 * Whom a steer is for: the whole run, or one role, attempt or item, by id.
 */
export interface SteerTarget {
  kind?: SteerTargetKind;
  id?: SubjectId | null;
}
/**
 * Reach the next safe boundary and hold the run there.
 */
export interface PauseRequest {
  control_id: ControlId1;
  action: Action1;
}
/**
 * Continue a paused run.
 *
 * ``fallback`` switches a run paused on a provider capacity limit to its configured fallback
 * provider as it resumes. ``when="capacity_returns"`` keeps such a run paused until capacity
 * should be back, then resumes it; it is refused when the run is not paused on a capacity
 * limit, and cannot be combined with ``fallback``, which resumes now.
 */
export interface ResumeRequest {
  control_id: ControlId2;
  action: Action2;
  fallback?: Fallback1;
  when?: When1;
}
/**
 * End the run, draining or cancelling the work in flight.
 */
export interface StopRequest {
  control_id: ControlId3;
  action: Action3;
  mode?: StopMode;
}
/**
 * End standing guidance: the standing steer sent under ``withdraws``.
 *
 * Turns dispatched after the run takes the withdrawal no longer receive the guidance. A
 * withdrawal of a steer the run does not hold as standing guidance in force is refused.
 */
export interface WithdrawRequest {
  control_id: ControlId4;
  action: Action4;
  withdraws: Withdraws;
}
/**
 * Cancel one attempt: it stops, closes cancelled, and is never judged.
 *
 * The search hears it close as if it had withdrawn the attempt itself, and the run's agents
 * are told at their next turn. A parked attempt waiting to reopen has that reopen cancelled
 * and ends. An attempt that has ended is refused when submitted; a blocked one, a parked one
 * with no reopen waiting, or one the run does not hold, is refused when the run takes the
 * request.
 */
export interface CancelAttemptRequest {
  control_id: ControlId5;
  action: Action5;
  attempt: SubjectId;
}
/**
 * Cancel one work item: no attempt at it runs or starts again.
 *
 * Each running or waiting attempt at the item stops, as ``CancelAttemptRequest`` stops one;
 * a parked one waiting to reopen has its reopen cancelled and ends; any other parked or
 * blocked one stays parked and is never reopened; one already finishing finishes, and its
 * result stands. The run starts no attempt at the item afterwards; the
 * search hears each refused start and reopen. The run's agents are told what became of
 * each attempt. An item already cancelled is refused when submitted; one the run does not
 * hold is refused when the run takes the request.
 */
export interface CancelItemRequest {
  control_id: ControlId6;
  action: Action6;
  item: SubjectId;
}
/**
 * Change how many attempts the run admits at once.
 *
 * A lower limit stops no attempt that runs: the run admits nothing new until enough of them
 * end. A higher one admits what now fits at once.
 */
export interface SetMaxParallelRequest {
  control_id: ControlId7;
  action: Action7;
  max_parallel: MaxParallel;
}
/**
 * Response envelope for all protocol requests.
 */
export interface Response {
  protocol_version?: ProtocolVersion16;
  request_id: RequestId16;
  client_id?: ClientId16;
  timestamp: Timestamp16;
  ok?: Ok;
  error?: Error;
  diagnostic?: Diagnostic | null;
  ack?: CommandAck | null;
  chat?: ChatResult | null;
  chat_thread?: ChatThreadInfo | null;
  snapshot?: RunSnapshot | null;
  events?: Events;
  performance?: Performance;
  performance_context?: PerformanceContext | null;
  experiments?: Experiments;
  experiment_update?: ExperimentUpdate | null;
  experiments_ready?: ExperimentsReady;
  iterations?: Iterations;
  iterations_ready?: IterationsReady;
  design?: Design;
  design_ready?: DesignReady;
  design_patch?: DesignPatch | null;
}
/**
 * Structured, provider-neutral description of an operator diagnostic.
 *
 * Frozen for the same reason as ``RunEvent``: diagnostics ride along on
 * replayed events, which readers share rather than copy.
 */
export interface Diagnostic {
  id?: Id;
  code: Code;
  summary: Summary;
  detail?: Detail;
  hint?: Hint;
  scope: DiagnosticScope;
  severity?: DiagnosticSeverity;
  retryability?: DiagnosticRetryability;
  cause_id?: CauseId;
  debug_ref?: DebugRef;
  source?: Source;
  validation_paths?: ValidationPaths;
}
/**
 * Acknowledgment of a requested run command; ``action`` is the control's tag.
 */
export interface CommandAck {
  action: ControlAction;
  status: Status;
}
/**
 * Answer and thread identity returned by experiment chat.
 */
export interface ChatResult {
  question: Question;
  answer: Answer;
  effect?: Effect;
  thread_id?: ChatThreadId | null;
}
/**
 * Resolved identity and agent settings of one experiment-chat thread.
 */
export interface ChatThreadInfo {
  thread_id: ChatThreadId;
  title?: Title1;
  provider: Provider1;
  model: Model1;
}
/**
 * Current server projection of a run's public state.
 */
export interface RunSnapshot {
  protocol_version?: ProtocolVersion17;
  run_id: RunId1;
  project_scope?: ProjectScope;
  sequence: Sequence;
  status: RunStatus;
  agent_kind?: AgentKind;
  round_label?: RoundLabel;
  active_executions?: ActiveExecutions;
  chat_threads?: ChatThreads;
  last_chat_thread_id?: ChatThreadId | null;
  spans?: Spans;
  overview?: RunOverview | null;
}
/**
 * Authoritative activity checkpoint for one running agent execution.
 */
export interface ActiveAgentExecution {
  execution_id: ExecutionId;
  agent_kind: AgentKind1;
  round_label: RoundLabel1;
  stage: Stage;
  attempt?: Attempt;
  assignment: Assignment;
  started_at: StartedAt;
  activity: AgentExecutionActivityData;
  provider?: Provider2;
  model?: Model2;
}
/**
 * Complete current activity for an active agent execution.
 */
export interface AgentExecutionActivityData {
  kind: Kind;
  mode: Mode2;
  summary: Summary1;
  tool?: Tool;
}
/**
 * One flow task or awaited operation of the run, on the run clock.
 */
export interface Span {
  id: SpanId;
  parent: SpanId | null;
  type: SpanType;
  name: Name;
  status: SpanStatus;
  waiting_on?: WaitingOn;
  attempts?: Attempts;
  started_at: Seconds;
  ended_at?: Seconds | null;
  failure?: SpanFailure | null;
  stop?: SpanStop | null;
  invocation?: Invocation;
  label?: SpanLabel | null;
}
/**
 * One source a suspended flow waits on, since the run-clock time it began waiting on it.
 */
export interface WaitingOn1 {
  kind: SpanWaitKind;
  ref: Ref;
  since: Seconds;
}
/**
 * One try of ``ctx.retry`` in the flow: try ``n`` of ``of``.
 *
 * ``outcome`` is None while the try runs, and stays None when the flow ended inside it.
 */
export interface SpanAttempt {
  n: N;
  of: Of;
  started_at: Seconds;
  ended_at?: Seconds | null;
  outcome?: AttemptOutcome | null;
  reason?: Reason;
}
/**
 * Why a span ended failed or stopped.
 */
export interface SpanFailure {
  kind: FailureKind;
  message: Message;
}
/**
 * The operator's stop as this flow saw it: when it arrived, and whether it interrupted it.
 */
export interface SpanStop {
  requested_at: Seconds;
  landed?: Landed;
}
/**
 * What the search says a span is: one of its declared kinds, with that kind's fields.
 *
 * ``attrs_json`` holds the kind's attribute model as JSON (the search's own model, which a
 * state schema cannot name); ``verdict`` is one of the kind's verdicts, apart from ``status``.
 */
export interface SpanLabel {
  kind: Kind1;
  title?: Title2;
  links?: Links;
  attrs_json?: AttrsJson;
  verdict?: Verdict;
  reason?: Reason1;
}
/**
 * A labelled span's relation ``rel`` (one its kind declares) to the span ``to``.
 */
export interface SpanLink {
  rel: Rel;
  to: SpanId;
}
/**
 * The run's objective and what measures it: the Overview's header facts.
 *
 * One definition for the core event and the wire: the run's ``run_started`` event carries it,
 * and the server's snapshot carries it with the unit and direction its measurements add.
 */
export interface RunOverview {
  overview_version?: OverviewVersion;
  objective: ObjectiveOverview;
  benchmarks?: BenchmarkVersions;
}
/**
 * What the run optimizes: the task's objective and the metric it is judged on.
 *
 * ``unit`` and ``direction`` come from the run's own measurements and objectives once it has
 * them; ``correctness_check`` is the argv of the check a candidate must pass (empty when the
 * task gates on none).
 */
export interface ObjectiveOverview {
  text: Text3;
  metric?: Metric;
  unit?: Unit;
  direction?: Direction;
  objectives?: Objectives;
  correctness_check?: CorrectnessCheck;
}
/**
 * One axis the task compares candidates on, and which way is better.
 */
export interface TaskObjective {
  name: Name1;
  direction: Direction1;
}
/**
 * What measured the run.
 *
 * ``evaluator`` is the evaluator package when the task pins one; a source-tree evaluator has
 * no version, so it is None.
 */
export interface BenchmarkVersions {
  evaluator?: EvaluatorIdentity | null;
}
/**
 * The evaluator package a run measures with, as the task's manifest pins it.
 */
export interface EvaluatorIdentity {
  name: Name2;
  version: Version;
}
/**
 * One reproducible human, control, or invocation event.
 *
 * Frozen: a recorded event is a durable fact. Readers that need a variant
 * build one with ``model_copy(update=...)`` rather than mutating a shared
 * object, which lets ``EventStore`` replay history without copying it.
 */
export interface RunEvent {
  protocol_version?: ProtocolVersion18;
  sequence?: Sequence1;
  run_id?: RunId2;
  timestamp: Timestamp17;
  type: EventType;
  text?: Text4;
  diagnostic?: Diagnostic | null;
  status?: EventStatus | null;
  round_label?: RoundLabel2;
  agent_kind?: AgentKind2;
  invocation_id?: InvocationId;
  execution_id?: ExecutionId1;
  chat_thread_id?: ChatThreadId | null;
  /**
   * The agent-turn span of the run trace this event belongs to: the server stamps it on every event whose execution_id ran as a recorded agent-turn span (the turn's output, tool calls and results, activity and usage), so a client joins a transcript to the trace by id and never by invocation, name or time. Absent when the event is not part of a turn, or when it was recorded before its span was.
   */
  span_id?: SpanId | null;
  data?: Data;
  truncated?: Truncated;
}
/**
 * Completed answer and optional thread-turn identity.
 */
export interface ChatData {
  kind: Kind2;
  answer: Answer1;
  thread_title?: ThreadTitle;
  invocation_id?: InvocationId1;
}
/**
 * Identity and resolved agent settings for one experiment-chat thread.
 *
 * Replayed by clients to rebuild the thread list; the default thread is
 * implicit and never records one of these.
 */
export interface ChatThreadCreatedData {
  kind: Kind3;
  thread_id: ChatThreadId;
  title?: Title3;
  provider: Provider3;
  model: Model3;
  created_at: CreatedAt;
}
/**
 * Prompts submitted at the start of a model invocation.
 */
export interface InvocationStartedData {
  kind: Kind4;
  system_prompt: SystemPrompt;
  user_prompt: UserPrompt;
}
/**
 * Result or error recorded when a model invocation ends.
 */
export interface InvocationFinishedData {
  kind: Kind5;
  result?: Result;
  error?: Error1;
}
export interface Result {
  [k: string]: unknown;
}
/**
 * Semantic context for one prompt-to-result agent execution.
 */
export interface AgentExecutionStartedData {
  kind: Kind6;
  stage: Stage1;
  attempt?: Attempt1;
  system_prompt?: SystemPrompt1;
  user_prompt?: UserPrompt1;
  activity: AgentExecutionActivityData;
  provider?: Provider4;
  model?: Model4;
  reasoning_effort?: ReasoningEffort;
  harness_version?: HarnessVersion;
}
/**
 * Terminal result for one agent execution.
 */
export interface AgentExecutionFinishedData {
  kind: Kind7;
  result?: Result1;
  error?: Error2;
  reply?: AgentReply | null;
}
export interface Result1 {
  [k: string]: unknown;
}
/**
 * The typed reply a structured turn ended with, read through the role's own reply model.
 *
 * ``kind`` is that model's name and ``json_schema`` its JSON Schema, so a reader learns which
 * fields are text, choices or lists from the model and keeps no list of its own. ``value`` is
 * the validated reply as JSON.
 */
export interface AgentReply {
  kind: Kind8;
  json_schema: JsonSchema;
  value: Value;
}
export interface JsonSchema {
  [k: string]: unknown;
}
export interface Value {
  [k: string]: unknown;
}
/**
 * Captured line of server output and its stream.
 */
export interface OutputData {
  kind: Kind9;
  stream: Stream;
  source?: Source1;
  content: Content;
}
/**
 * Transport details emitted once the server is ready.
 */
export interface ServerReadyData {
  kind: Kind10;
  socket_protocol?: SocketProtocol;
}
/**
 * Initial input and loop settings for a run.
 */
export interface RunStartedData {
  kind: Kind11;
  outer_loop: OuterLoop;
  input: Input;
  max_rounds?: MaxRounds;
  expected_roles?: ExpectedRoles;
  overview?: RunOverview | null;
}
/**
 * A starting run's ``stage`` began, moved, waited, or finished.
 *
 * Journaled while the run acquires its resources, before it is ready. The
 * run's stage only moves forward. ``state`` is ``running`` when a stage begins
 * and at each step of its ``progress`` (an image build's steps done of total,
 * and the step now running), ``waiting`` while it is blocked on something
 * outside the run host (``condition``, and the ``elapsed_seconds`` waited so
 * far), and ``finished`` with the ``elapsed_seconds`` the stage took. A stage
 * that fails journals no ``finished``; the run's failure says why it ended.
 */
export interface RunStartupProgressData {
  kind: Kind12;
  stage: StartupStage;
  progress?: StartupProgress | null;
  state?: StartupState;
  condition?: StartupWaitCondition | null;
  elapsed_seconds?: ElapsedSeconds;
}
/**
 * How far a startup stage has come: ``done`` of ``total`` steps, now on ``label``.
 *
 * ``total`` is ``None`` when the stage does not know it. ``label`` names the
 * step now running (an image build's Dockerfile instruction), as data.
 */
export interface StartupProgress {
  done: Done;
  total?: Total;
  label?: Label;
}
/**
 * Why a run failed, what partial result it preserved, and the counts behind it.
 */
export interface RunFailedData {
  kind: Kind13;
  failure: RunFailure;
}
/**
 * What a failed run did and why it stopped, as data; frontends choose the wording.
 *
 * ``reason`` is the strategy's own account of the stop. ``workstreams_started`` counts
 * attempts core admitted, against the ``workstream_budget`` the run was allowed.
 * ``candidates_kept`` counts settled candidates eligible for adoption.
 * ``adopted_candidate`` names the verified candidate revision core adopted even though the
 * strategy still reported a failed outcome. Its None default preserves replay of failure events
 * recorded before the adoption fact was added to the event contract.
 */
export interface RunFailure {
  kind: RunFailureKind;
  reason: Reason2;
  workstreams_started: WorkstreamsStarted;
  workstream_budget: WorkstreamBudget;
  candidates_kept: CandidatesKept;
  adopted_candidate?: AdoptedCandidate;
}
/**
 * How a core run's durable record stands as the run ends, emitted once just before its end.
 *
 * ``reason`` is core's ``EndReason`` when the run recorded a result, ``None`` when it ended
 * without one (an error, or a stop that left work unsettled). ``resumable`` is whether a
 * resume continues it: its record is not terminal. ``rounds_finished`` counts its
 * workstreams (rounds) that reached their end. ``summary`` is core's account of the result.
 */
export interface RunEndingData {
  kind: Kind14;
  reason: EndReason | null;
  summary?: Summary2;
  resumable: Resumable;
  rounds_finished: RoundsFinished;
}
/**
 * Reason and optional signal for an interrupted run.
 */
export interface RunInterruptedData {
  kind: Kind15;
  reason: Reason3;
  signal?: Signal;
}
/**
 * One move of the run through its lifecycle.
 *
 * Carries the whole transition so a client folds the status instead of
 * inferring it: ``status`` is the new value and ``previous`` the one it
 * replaced. Which invocation boundary a pause landed on is on the event
 * envelope (``agent_kind``, ``round_label``, ``execution_id``) like every
 * other execution-scoped fact, not repeated here.
 */
export interface RunStatusChangedData {
  kind: Kind16;
  status: RunStatus;
  previous: RunStatus;
}
/**
 * Reason and revision for a changed experiment projection.
 */
export interface ExperimentsChangedData {
  kind: Kind17;
  reason: Reason4;
  revision?: Revision1;
}
/**
 * Semantic revision of the policy-owned iteration projection.
 */
export interface IterationsChangedData {
  kind: Kind18;
  revision?: Revision2;
}
/**
 * Diagnostic details for configuration-stage failure.
 */
export interface ConfigurationFailedData {
  kind: Kind19;
  code: Code1;
  stage: Stage2;
  message: Message1;
  usage?: Usage;
  exit_code: ExitCode;
}
/**
 * Name and optional attempt number for a loop phase.
 */
export interface PhaseData {
  kind: Kind20;
  phase: Phase;
  attempt?: Attempt2;
}
/**
 * Incremental output produced during agent execution.
 */
export interface AgentOutputChunkData {
  kind: Kind21;
  channel: Channel;
  content: Content1;
  status?: AgentStatusData | null;
}
/**
 * Structured progress readings for one agent invocation.
 *
 * Carried on presentation events so renderers can format their own status
 * prefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without
 * the server baking any layout or styling into the payload.
 */
export interface AgentStatusData {
  progress?: Progress;
  agent_label?: AgentLabel;
  elapsed_seconds?: ElapsedSeconds1;
  input_tokens?: InputTokens;
  context_window?: ContextWindow;
}
/**
 * Captured output from a managed subprocess.
 */
export interface SubprocessOutputData {
  kind: Kind22;
  process_id: ProcessId;
  process_kind: ProcessKind;
  stream: Stream1;
  content: Content2;
}
/**
 * Verdict and feedback returned by the judge.
 *
 * See ``vibesys.events.JudgeResultData`` for ``skipped``/``skip_reason``/
 * ``implementer_outcome``.
 */
export interface JudgeResultData {
  kind: Kind23;
  verdict: Verdict1;
  feedback: Feedback;
  attempt: Attempt3;
  skipped?: Skipped;
  skip_reason?: SkipReason;
  implementer_outcome?: ImplementerOutcome;
}
/**
 * Metric result emitted by a benchmark stage.
 */
export interface BenchmarkResultData {
  kind: Kind24;
  metric: Metric1;
  value: Value1;
  unit: Unit1;
}
/**
 * Wire summary of judge and performance outcomes for one recorded round.
 *
 * A projected dynamic implementation round carries a non-null envelope
 * ``round_label`` matching its accompanying ``workstream_finished`` event.
 * ``attempts`` is strategy-neutral; dynamic currently reports one and does
 * not use it as an implementer-turn count. The CLI glossary owns the public
 * round, workstream, and attempt definitions.
 */
export interface RoundFinishedData {
  kind: Kind25;
  attempts: Attempts1;
  judge_verdict: JudgeVerdict;
  perf_metric?: PerfMetric;
  perf_unit?: PerfUnit;
  profile_skipped: ProfileSkipped;
}
/**
 * Tool name, call identity, and arguments emitted by an agent.
 */
export interface ToolCallData {
  kind: Kind26;
  tool: Tool1;
  call_id?: CallId;
  args?: Args;
  status?: AgentStatusData | null;
}
export interface Args {
  [k: string]: unknown;
}
/**
 * Raw tool result and optional structured rendering payload.
 */
export interface ToolResultData {
  kind: Kind27;
  tool: Tool2;
  call_id?: CallId1;
  content: Content3;
  is_error?: IsError;
  payload?: Payload;
}
/**
 * Structured result of a command-style tool execution.
 */
export interface CommandResultPayload {
  kind: Kind28;
  stdout: Stdout;
  stderr: Stderr;
  exit_code?: ExitCode1;
  duration?: Duration;
}
/**
 * A tool result that is a JSON object or array, already parsed.
 */
export interface JsonResultPayload {
  kind: Kind29;
  value: Value2;
}
/**
 * A steer the chat agent suggests; the operator edits, sends or dismisses it.
 *
 * Recorded on the chat thread (the event header's ``chat_thread_id``) whose turn called
 * ``propose_steer``. It is a proposal only: nothing reaches the run until the operator sends
 * a ``command.control`` steer, which the proposal's fields prefill. ``proposal_id`` names it
 * for the client's card, and is unique within the run.
 */
export interface SteerProposedData {
  kind: Kind30;
  proposal_id: ProposalId;
  proposal: SteerProposal;
}
/**
 * Guidance a chat agent suggests, in the fields of a ``SteerRequest``.
 *
 * It carries no ``control_id`` and nothing in it reaches the run: only an operator's
 * ``command.control`` with a ``SteerRequest`` does. The tool that returns it is the
 * agent's only steering tool, so the agent cannot send guidance.
 */
export interface SteerProposal {
  text: Text5;
  target?: SteerTarget1;
  mode?: SteerMode1;
}
/**
 * Whom a steer is for: the whole run, or one role, attempt or item, by id.
 */
export interface SteerTarget1 {
  kind?: SteerTargetKind;
  id?: SubjectId | null;
}
/**
 * One step in an operator steer's life, by the control id the operator sent it under.
 *
 * Carried by the steer-lifecycle run-control events (queued, reached a turn, in force,
 * dropped, ended, withdrawal queued, refused); ``SteerDelivery`` is its one definition.
 */
export interface SteerDeliveryData {
  kind: Kind31;
  delivery: SteerDelivery;
}
/**
 * One step in a steer's life: the facts a client needs to show it, by control id.
 *
 * ``control_id`` is the id the operator sent the steer or withdrawal under, so a client
 * matches the step to its own send; None for a control sent without one. A step carries
 * the facts its state names and no other: ``mode`` when queued, ``withdraws`` for a queued
 * withdrawal, ``role`` when a turn took the steer, and ``reason`` when it ended unreached.
 */
export interface SteerDelivery {
  state: SteerDeliveryState;
  control_id?: ControlId8;
  action?: Action8;
  mode?: SteerMode2 | null;
  target?: SteerTarget2 | null;
  withdraws?: Withdraws1;
  role?: Role;
  attempt_id?: AttemptId;
  reason?: Reason5;
}
/**
 * Whom a steer is for: the whole run, or one role, attempt or item, by id.
 */
export interface SteerTarget2 {
  kind?: SteerTargetKind;
  id?: SubjectId | null;
}
/**
 * Current todo list reported by an agent.
 */
export interface TodoUpdateData {
  kind: Kind32;
  todos?: Todos;
}
/**
 * One item in the agent's reported todo list.
 */
export interface TodoItemData {
  content: Content4;
  status: Status1;
}
/**
 * Token usage reported by the active model.
 */
export interface UsageUpdateData {
  kind: Kind33;
  input_tokens: InputTokens1;
  context_window?: ContextWindow1;
  model?: Model5;
}
/**
 * One rate-limit window a provider reported, as semantic data.
 *
 * ``exhausted`` is the resolved fact (the provider's own statement, else
 * usage at or past 100%); the other fields are what the provider stated,
 * ``None`` when it did not state them. ``resets_at`` is epoch seconds.
 */
export interface RateLimitUpdateData {
  kind: Kind34;
  provider?: Provider5;
  window?: Window;
  limit?: Limit1;
  used_fraction?: UsedFraction;
  resets_at?: ResetsAt;
  window_minutes?: WindowMinutes;
  exhausted: Exhausted;
}
/**
 * A turn stopped on a provider capacity limit and the run paused for it.
 *
 * ``condition`` is ``quota_exhausted`` (a usage, spend or billing limit),
 * ``rate_limited`` (sustained rate limiting) or ``unavailable`` (a provider outage:
 * only the stopped turn waits, under its outage budget, and the run is not paused);
 * ``detail`` is the provider's
 * diagnostic and ``resets_at`` the epoch second the provider said capacity
 * returns, when it said. The run stays paused until it resumes; ``resumes_at`` is the
 * epoch second the run's quota policy resumes it by itself, ``None`` when only the
 * operator can. ``policy`` is the unattended quota policy; the fallback fields name the
 * provider and model an operator may resume with, ``None`` when none is configured.
 */
export interface QuotaPausedData {
  kind: Kind35;
  provider: Provider6;
  condition: Condition;
  detail: Detail1;
  resets_at?: ResetsAt1;
  resumes_at?: ResumesAt;
  policy?: Policy;
  fallback_provider?: FallbackProvider;
  fallback_model?: FallbackModel;
}
/**
 * A turn stopped on a capacity limit is sent again on the same provider.
 *
 * ``condition`` is the limit it stopped on, as in ``quota_paused`` (``None`` in events
 * recorded before the field existed). ``waited_seconds`` is how long that stop held the
 * turn: for ``unavailable`` the turn's own backoff, during which the rest of the run went
 * on; otherwise the run's pause.
 */
export interface QuotaResumedData {
  kind: Kind36;
  provider: Provider7;
  condition?: Condition1;
  reason?: Reason6;
  waited_seconds?: WaitedSeconds;
}
/**
 * The quota policy ended a turn with the quota error instead of pausing or waiting longer.
 *
 * ``waited_seconds`` is how long this last stop held the turn before it ended (zero when
 * it ended without waiting, and in events recorded before the field existed).
 */
export interface QuotaAbandonedData {
  kind: Kind37;
  provider: Provider8;
  condition: Condition2;
  detail: Detail2;
  reason: Reason7;
  waited_seconds?: WaitedSeconds1;
}
/**
 * At the operator's request, the run paused on a capacity limit resumes when capacity returns.
 *
 * ``resumes_at`` is the epoch second it resumes (the provider's reset time plus a margin),
 * ``None`` when the provider gave no reset time and the run tries again after the quota
 * policy's retry interval. A later request replaces this one.
 */
export interface QuotaWaitingData {
  kind: Kind38;
  provider: Provider9;
  resumes_at?: ResumesAt1;
}
/**
 * The operator ended a wait for capacity: ``resumed`` the run now, or ``paused`` it again.
 */
export interface QuotaWaitCancelledData {
  kind: Kind39;
  provider: Provider10;
  reason: Reason8;
}
/**
 * The run replaced a provider by its fallback.
 *
 * Sessions opened from now on run on ``to_provider`` and ``to_model`` as fresh
 * conversations; a session already open does not move. Every later event
 * carries the provider and model it actually ran on.
 */
export interface ProviderSwitchedData {
  kind: Kind40;
  from_provider: FromProvider;
  to_provider: ToProvider;
  to_model: ToModel;
  reason: Reason9;
  detail: Detail3;
}
/**
 * One framework gate began evaluating the current candidate.
 */
export interface GateStartedData {
  kind: Kind41;
  gate: GateKind;
  recipe?: Recipe;
  command?: Command;
  source?: FrameworkSource;
  source_label?: SourceLabel;
}
/**
 * Outcome of one framework gate; envelope status carries pass or fail.
 *
 * ``metric``/``value``/``unit`` are set only on a passing benchmark gate.
 * ``unit`` keeps the historical fallback of the metric name when the
 * contract declares no unit. ``output_tail`` carries bounded, credential-redacted
 * trailing command output on failure. ``round_label`` groups the per-recipe local-validation
 * gates of one attempt back together, so a client folds them into a
 * pass-fraction itself rather than the backend aggregating one.
 */
export interface GateFinishedData {
  kind: Kind42;
  gate: GateKind;
  recipe?: Recipe1;
  reused?: Reused;
  metric?: Metric2;
  value?: Value3;
  unit?: Unit2;
  output_tail?: OutputTail;
  round_label?: RoundLabel3;
  source?: FrameworkSource1;
  source_label?: SourceLabel1;
}
/**
 * A Git tracker outcome: a snapshot, baseline, or exclusion change.
 *
 * Exactly one aspect is populated per event: a snapshot attempt carries
 * ``label`` (``commit`` is None when there was nothing to commit), a
 * trusted-input baseline carries ``baseline``, and a snapshot-exclusion
 * change carries ``excluded_paths``.
 */
export interface WorkspaceSnapshotData {
  kind: Kind43;
  label?: Label1;
  commit?: Commit;
  baseline?: Baseline;
  excluded_paths?: ExcludedPaths;
  source?: FrameworkSource2;
}
/**
 * One per run: the resolved configuration a loop starts with.
 *
 * See ``vibesys.events.RunConfiguredData`` for ``max_continuation_rounds``/
 * ``max_unmeasured_turns``.
 */
export interface RunConfiguredData {
  kind: Kind44;
  run_log_path: RunLogPath;
  project_root: ProjectRoot;
  model?: Model6;
  objective?: Objective;
  search_policy?: SearchPolicy;
  benchmark_contract?: BenchmarkContract;
  pareto_objectives?: ParetoObjectives;
  max_continuation_rounds?: MaxContinuationRounds;
  max_unmeasured_turns?: MaxUnmeasuredTurns;
  source?: FrameworkSource3;
}
/**
 * A non-fatal framework fault an operator should see.
 *
 * The server projection also lifts this payload into the wire event's
 * ``diagnostic`` field so diagnostic-oriented clients need no new handling.
 */
export interface FrameworkWarningData {
  kind: Kind45;
  summary: Summary3;
  detail?: Detail4;
  source?: FrameworkSource4;
  source_label?: SourceLabel2;
}
/**
 * The run's result was chosen: a retained candidate, or the unchanged trusted input.
 */
export interface WinnerSelectedData {
  kind: Kind46;
  selection: Selection;
  revision: Revision3;
  settlement_id?: SettlementId;
}
/**
 * A candidate's measurement was compared with its base's. See ``vibesys.events``.
 */
export interface MeasurementComparedData {
  kind: Kind47;
  metric: Metric3;
  unit?: Unit3;
  direction: Direction2;
  candidate_commit: CandidateCommit;
  base_commit: BaseCommit;
  candidate_value: CandidateValue;
  base_value: BaseValue;
  ratio: Ratio;
  noise: Noise;
  verdict: MeasurementVerdict;
  candidate_samples: CandidateSamples;
  base_samples: BaseSamples;
  source?: FrameworkSource5;
}
/**
 * The comparison baselines a run read when it started or resumed; the newest event wins.
 *
 * A run never measures a comparison: ``vibesys compare`` does, once, and stores the reading.
 */
export interface ComparisonsReadData {
  kind: Kind48;
  comparisons: Comparisons;
}
/**
 * One comparison baseline as the run read it from the project's store.
 *
 * ``value`` is the mean of ``samples`` runs of the task's benchmark against the engine, and
 * ``spread`` their relative standard deviation (absent for one run). ``reason`` explains a
 * failed or unconfigured comparison. A hidden-from-agents comparison is still shown here:
 * the operator sees it; only the planner's prompt omits it.
 */
export interface ComparisonReadingData {
  name: Name3;
  label: Label2;
  status: ComparisonStatus;
  metric?: Metric4;
  unit?: Unit4;
  direction?: Direction3;
  value?: Value4;
  spread?: Spread;
  samples?: Samples;
  hosts?: Hosts;
  reason?: Reason10;
}
/**
 * Wire summary for one projected, recorded policy round and its typed failure, if any.
 *
 * See ``vibesys.events.WorkstreamFinishedData`` for the generic producer's
 * serial and dynamic behavior. Its current round projection always reports
 * ``setup_failure`` and ``refunded`` as ``False``; in the dynamic policy
 * specifically, profile and merge workstreams do not emit this event.
 */
export interface WorkstreamFinishedData {
  kind: Kind49;
  setup_failure: SetupFailure;
  refunded: Refunded;
  repeated_error: RepeatedError;
  last_error?: WorkstreamFailureData | null;
}
/**
 * One workstream's typed account of why it ended without a measured candidate.
 */
export interface WorkstreamFailureData {
  kind: WorkstreamFailureKind;
  role?: Role1;
  turns?: Turns;
  limit?: Limit2;
  text?: Text6;
}
/**
 * Settled token and cost accounting for one finished agent turn.
 *
 * See ``vibesys.events.TurnUsageData``.
 */
export interface TurnUsageData {
  kind: Kind50;
  input_tokens?: InputTokens2;
  cache_creation_input_tokens?: CacheCreationInputTokens;
  cache_read_input_tokens?: CacheReadInputTokens;
  output_tokens?: OutputTokens;
  reasoning_output_tokens?: ReasoningOutputTokens;
  total_cost_usd?: TotalCostUsd;
  duration_ms?: DurationMs;
  span_id?: SpanId | null;
}
/**
 * A span started.
 */
export interface SpanStartedData {
  span: Span;
  kind: Kind51;
}
/**
 * A span changed.
 */
export interface SpanUpdatedData {
  span: Span;
  kind: Kind52;
}
/**
 * A span ended.
 */
export interface SpanEndedData {
  span: Span;
  kind: Kind53;
}
/**
 * One measured performance result in a run.
 */
export interface PerformanceRound {
  round: Round;
  perf_metric: PerfMetric1;
  perf_unit: PerfUnit1;
  passed: Passed;
  profile_skipped?: ProfileSkipped1;
}
/**
 * What the performance plot measures and how to read it.
 *
 * Copied from recorded run state and the run manifest, never recomputed.
 * Every field is optional so the section can describe the objective before
 * the first measurement and omit facts a run never recorded; a run whose
 * prose is known before its metric still gets a description-only context.
 */
export interface PerformanceContext {
  objective_metric?: ObjectiveMetric;
  objective_unit?: ObjectiveUnit;
  objective_direction?: ObjectiveDirection;
  objective_baseline_value?: ObjectiveBaselineValue;
  objective_baseline_round?: ObjectiveBaselineRound;
  objective_baseline_commit?: ObjectiveBaselineCommit;
  objective_description?: ObjectiveDescription;
}
/**
 * One unit of investigation: a hypothesis and every round it spans.
 *
 * ``resolved_outcome`` is copied from the server's typed hypothesis state,
 * never recomputed by the server or client.
 */
export interface HypothesisEntry {
  hypothesis_id: HypothesisId;
  identified?: Identified;
  title?: Title4;
  claim?: Claim;
  action?: Action9;
  first_round: FirstRound;
  last_round: LastRound;
  rounds?: Rounds;
  resolved_outcome?: ResolvedOutcome;
  judge_verdict?: JudgeVerdict2;
  perf_metric?: PerfMetric3;
  perf_unit?: PerfUnit3;
  perf_delta_pct?: PerfDeltaPct1;
  perf_metric_name?: PerfMetricName;
  perf_direction?: PerfDirection;
  perf_baseline_value?: PerfBaselineValue;
  perf_baseline_round?: PerfBaselineRound;
  perf_baseline_commit?: PerfBaselineCommit;
  perf_delta_reason?: PerfDeltaReason | null;
  best?: Best;
  strategy_disposition?: StrategyDisposition;
  strategy_reason?: StrategyReason;
  active?: Active;
}
/**
 * One round belonging to a hypothesis, for the experiment-log drill-down.
 *
 * This is the single source for every per-round fact the server publishes.
 * Surfaces that need more about a round (the design log's file list, for
 * example) join to this row by ``round`` rather than restating its fields.
 *
 * ``hypothesis_outcome`` and ``candidate_disposition`` are closed sets, so
 * the generated client union is closed too. A round record written before a
 * member existed, or carrying a value the framework no longer defines, is
 * projected as ``None``: unreadable and unrecorded are the same thing to a
 * client, and a stale string must not take down the whole log.
 */
export interface HypothesisRound {
  round: Round1;
  passed: Passed1;
  reviewed: Reviewed;
  hypothesis_outcome?: HypothesisOutcome;
  judge_verdict?: JudgeVerdict1;
  perf_metric?: PerfMetric2;
  perf_unit?: PerfUnit2;
  perf_delta_pct?: PerfDeltaPct;
  commit?: Commit1;
  official_evaluation?: OfficialEvaluation;
  candidate_disposition?: CandidateDisposition | null;
}
/**
 * How to apply ``Response.experiments`` to a client's prior snapshot.
 *
 * A reset replaces the entire list. A delta replaces entries by stable
 * hypothesis ID and then removes the named IDs. ``from_revision`` is None for
 * a reset because no prior client state is trusted.
 */
export interface ExperimentUpdate {
  run_id: RunId3;
  projection_id: ProjectionId1;
  from_revision?: FromRevision;
  through_revision: ThroughRevision;
  reset: Reset;
  removed_hypothesis_ids?: RemovedHypothesisIds;
}
/**
 * One bounded page, with enough identity to reject stale merges.
 */
export interface IterationSlice {
  kind?: Kind54;
  schema_version?: SchemaVersion;
  run_id: RunId4;
  projection_revision: ProjectionRevision;
  parent_id: ParentId1;
  from_cursor?: FromCursor;
  items: Items;
  next_cursor?: NextCursor;
}
/**
 * One stable node in a policy's iteration hierarchy.
 */
export interface IterationSummary {
  id: Id1;
  parent_id?: ParentId2;
  kind: IterationKind;
  label: Label3;
  ordinal: Ordinal;
  status: IterationStatus;
  child_count?: ChildCount;
  headline_metrics?: HeadlineMetrics;
}
/**
 * One bounded headline measurement attached to an iteration row.
 */
export interface IterationMetric {
  id: Id2;
  value?: Value5;
  unit?: Unit5;
  direction?: Direction4;
}
/**
 * Typed absence, kept distinct from a supported empty tree.
 */
export interface IterationUnavailable {
  kind?: Kind55;
  schema_version?: SchemaVersion1;
  reason: IterationUnavailableReason;
  detail?: Detail5;
}
/**
 * What one round changed in the workspace.
 *
 * Deliberately narrow: every other per-round fact (outcome, review,
 * official evaluation, candidate disposition, measurement) already crosses
 * the protocol on ``HypothesisRound``, and a client joins the two by
 * ``round``. Publishing a second copy here let the two fetches disagree
 * about the same round.
 *
 * ``files`` is derived from the run workspace's git history. None means the
 * round's commit range could not be resolved (no checkpoint recorded, or the
 * workspace history no longer has it), which is distinct from an empty list,
 * a resolved range that touched nothing outside framework bookkeeping.
 */
export interface DesignRound {
  round: Round2;
  commit?: Commit2;
  base?: Base1;
  files?: Files;
}
/**
 * One workspace file a round's commit range touched.
 */
export interface DesignFileChange {
  path: Path1;
  change: Change;
  renamed_from?: RenamedFrom;
}
/**
 * One file's unified patch text from a round's commit range.
 *
 * ``patch`` is the raw ``git diff`` output for the one file (rename
 * detection on, so a renamed file arrives as a single patch spanning both
 * paths). None means the workspace repository could not produce the text
 * (repository missing or unreadable), which is distinct from an empty
 * string, a file the range lists but whose content did not change.
 *
 * ``truncated`` marks a patch cut at the server's size bound. The echoed
 * range and paths let a client show the exact ``git diff`` command that
 * reproduces the full output externally.
 */
export interface DesignPatch {
  base: Base2;
  head: Head1;
  path: Path2;
  renamed_from?: RenamedFrom1;
  patch?: Patch;
  truncated?: Truncated1;
}
/**
 * Initial acknowledgment for an event subscription.
 */
export interface SubscribedMessage {
  type: Type16;
  request_id: RequestId17;
  client_id?: ClientId17;
  run_id: RunId5;
  latest_sequence: LatestSequence;
}
/**
 * Single-event message for the legacy streaming protocol.
 */
export interface EventMessage {
  type: Type17;
  event: RunEvent;
}
/**
 * Event batch and cursor metadata sent to subscribers.
 */
export interface EventBatchMessage1 {
  type: Type18;
  events: Events1;
  through_sequence?: ThroughSequence;
  active_executions?: ActiveExecutions;
  active_executions_checkpoint_id?: ActiveExecutionsCheckpointId;
  store_id?: StoreId1;
  history_after_sequence?: HistoryAfterSequence;
  rebootstrap?: Rebootstrap;
}
/**
 * Event batch and cursor metadata sent to subscribers.
 */
export interface EventBatchMessage2 {
  type: Type19;
  events: Events2;
  through_sequence?: ThroughSequence1;
  active_executions: null;
  active_executions_checkpoint_id: string;
  store_id?: StoreId2;
  history_after_sequence?: HistoryAfterSequence1;
  rebootstrap?: Rebootstrap1;
}
/**
 * One bounded piece of an atomically replaced execution checkpoint.
 *
 * The payload pieces concatenate to the JSON encoding of ``ActiveExecutions``.
 * A client stages one transfer and publishes the decoded list only after all
 * pieces arrive in order. The metadata is copied from the batch whose
 * checkpoint this replaces, so the final publication remains in the same
 * sequence space even when the batch itself was split into event frames.
 */
export interface ActiveExecutionsChunkMessage {
  type: Type20;
  checkpoint_id: CheckpointId;
  chunk_index: ChunkIndex;
  chunk_count: ChunkCount;
  payload: Payload1;
  through_sequence: ThroughSequence2;
  store_id: StoreId3;
  history_after_sequence: HistoryAfterSequence2;
}
/**
 * Structured error envelope for protocol failures.
 */
export interface ProtocolErrorMessage {
  type: Type21;
  request_id?: RequestId18;
  client_id?: ClientId18;
  code: Code2;
  message: Message2;
  diagnostic?: Diagnostic | null;
}
/**
 * A step of opening the run's environment, such as building its image.
 */
export interface StartupStep {
  span_id?: SpanId | null;
  kind: Kind56;
  stage: EnvironmentStartupStage;
}
/**
 * One attempt of one gate of a candidate's evaluation.
 */
export interface GateStage {
  span_id?: SpanId | null;
  kind: Kind57;
  execution_id: ExecutionId2;
  gate: GateKind;
  attempt: Attempt4;
}
/**
 * Building or pulling a tool the run needs, such as a profiler.
 */
export interface ToolBuild {
  span_id?: SpanId | null;
  kind: Kind58;
  name: Name4;
}
/**
 * The run's own narration for humans: what it is doing, warnings, tracebacks.
 */
export interface RunNarration {
  span_id?: SpanId | null;
  kind: Kind59;
}
/**
 * One narration record, as one JSON line of a narration stream.
 */
export interface NarrationLine {
  at: At;
  level: NarrationLevel;
  logger: Logger;
  thread: Thread;
  message: Message3;
  traceback?: Traceback;
}
export interface SpanAttributes {
  dynamic?: DynamicSpanAttributes | null;
  evolve?: EvolveSpanAttributes | null;
  "multi-agent"?: MultiAgentSpanAttributes | null;
  plain?: PlainSpanAttributes | null;
  "profile-guided-multi-agent"?: ProfileGuidedMultiAgentSpanAttributes | null;
  "profile-guided-single-agent"?: ProfileGuidedSingleAgentSpanAttributes | null;
  "single-agent"?: SingleAgentSpanAttributes | null;
}
export interface DynamicSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
/**
 * The adoption of the run's winner: the candidate, what it measured and how it compares.
 *
 * ``candidate`` is the adopted revision; None when the input itself stands. The value is the
 * candidate's headline (the input's when it stands) and ``vs_baseline`` its ratio to the
 * input's; either is None while it is unknown.
 */
export interface AdoptionAttrs {
  candidate?: Candidate;
  metric_value?: MetricValue;
  vs_baseline?: VsBaseline;
}
/**
 * An agent turn: the agent asked, its role, the model it runs on, and who started it.
 *
 * ``agent`` is the agent's stable identity (the conversation its turns continue): every turn
 * of one agent carries the same, so a client counts an agent's turns by it. ``started_by``
 * names the agents whose work started this one (more than one when it serves several, say a
 * merge of their work); empty when the search itself started it. ``model`` is None for the
 * run's default. The provider and the reasoning effort are the host's settings, which the
 * search never sees.
 */
export interface AgentTurnAttrs {
  agent: Agent;
  role: Role2;
  model?: Model7;
  started_by?: StartedBy;
}
/**
 * A trusted measurement of a candidate: its stages, its value, and the ratio to the input's.
 *
 * ``failed`` is the first trusted check that rejected the candidate, once its readings are
 * read; None when every check it read passed. The verdict (``failed_check`` for a check, or a
 * rejection for another reason) is the search's own.
 */
export interface GateAttrs {
  stage: Stage3;
  metric_value?: MetricValue1;
  vs_baseline?: VsBaseline1;
  input_metrics?: InputMetrics;
  metrics?: Metrics;
  failed?: FailedCheck | null;
}
/**
 * One measured quantity on a declared axis.
 */
export interface MetricRow {
  name: Name5;
  value: Finite;
  direction: Direction5;
  unit?: Unit6;
}
/**
 * The trusted check that rejected a candidate, and how far it came when it reported that.
 *
 * ``completed`` of ``required`` ``unit`` (``93`` of ``100`` ``trials``) is what the check's
 * evaluator reported it got through; each is None when it reported no progress.
 */
export interface FailedCheck {
  check: EvidenceKind;
  completed?: Completed;
  required?: Required;
  unit?: Unit7;
}
/**
 * A planning call: which call of the run it is.
 */
export interface PlanningAttrs {
  call: Call;
}
/**
 * A workstream: one attempt at a candidate, the base it starts from, and what it kept.
 *
 * ``hypothesis_id`` is the candidate's identity: every attempt at one idea carries the same
 * one. ``round`` is the round that runs it, for a search that plays rounds (the serial
 * searches); None for one that plans calls instead. A round labels its workstream as it
 * begins, before it has chosen its hypothesis: until then ``hypothesis_id``,
 * ``parent_revision`` and ``idea`` are None.
 *
 * ``parent_revision`` is the base: the revision the attempt starts from. ``base_round`` is the
 * round of the idea that made it (a serial search); None when the base is the input.
 *
 * ``idea`` is what the candidate tries, in the words of the agent that chose it (a planner's
 * or designer's title, a mutator's summary, an issue's title); None until an agent said it.
 * ``revision`` is the revision the attempt kept, set with its verdict; None while it works and
 * when it kept nothing. ``outcome`` and ``summary`` are the implementer's own account of the
 * attempt (its structured outcome and what it did), set when the attempt settles; None while
 * it works and for a search whose agents give none. A ``probe`` only looks (a profile, a
 * performance evaluation): it writes no candidate and reaches no verdict, so it is not a
 * candidate.
 */
export interface WorkstreamAttrs {
  hypothesis_id?: HypothesisId1;
  parent_revision?: ParentRevision;
  base_round?: BaseRound;
  round?: Round3;
  idea?: Idea;
  revision?: Revision4;
  outcome?: Outcome;
  summary?: Summary4;
  probe?: Probe;
}
export interface EvolveSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
export interface MultiAgentSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
export interface PlainSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
export interface ProfileGuidedMultiAgentSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
export interface ProfileGuidedSingleAgentSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
export interface SingleAgentSpanAttributes {
  adoption?: AdoptionAttrs | null;
  agent_turn?: AgentTurnAttrs | null;
  gate?: GateAttrs | null;
  planning_call?: PlanningAttrs | null;
  workstream?: WorkstreamAttrs | null;
}
