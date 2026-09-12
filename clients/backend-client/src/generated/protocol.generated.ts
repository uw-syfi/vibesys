/* Generated from the Python protocol models. Do not edit. */

export type Request =
  | PauseCommand
  | ResumeCommand
  | SteerCommand
  | SnapshotQuery
  | ChatQuery
  | ChatThreadCreateQuery
  | ChatOptionsQuery
  | TuiDefaultsQuery
  | HistoryQuery
  | PerformanceQuery
  | ExperimentQuery
  | DesignQuery
  | EventsQuery
  | SubscribeRequest;
export type ProtocolVersion = 1;
export type RequestId = string;
export type Timestamp = string;
export type Type = "command.pause";
export type Mode = "after_current_agent_call";
export type ProtocolVersion1 = 1;
export type RequestId1 = string;
export type Timestamp1 = string;
export type Type1 = "command.resume";
export type ProtocolVersion2 = 1;
export type RequestId2 = string;
export type Timestamp2 = string;
export type Type2 = "command.steer";
export type Text = string;
export type ProtocolVersion3 = 1;
export type RequestId3 = string;
export type Timestamp3 = string;
export type Type3 = "query.snapshot";
export type ProtocolVersion4 = 1;
export type RequestId4 = string;
export type Timestamp4 = string;
export type Type4 = "query.chat";
export type Text1 = string;
export type ThreadId = string | null;
export type ProtocolVersion5 = 1;
export type RequestId5 = string;
export type Timestamp5 = string;
export type Type5 = "query.chat_thread_create";
export type Driver = ("agentshim" | "omnigent") | null;
export type Provider = string | null;
export type Model = string | null;
export type Title = string | null;
export type ProtocolVersion6 = 1;
export type RequestId6 = string;
export type Timestamp6 = string;
export type Type6 = "query.chat_options";
export type ProtocolVersion7 = 1;
export type RequestId7 = string;
export type Timestamp7 = string;
export type Type7 = "query.tui_defaults";
export type ProtocolVersion8 = 1;
export type RequestId8 = string;
export type Timestamp8 = string;
export type Type8 = "query.history";
export type ProtocolVersion9 = 1;
export type RequestId9 = string;
export type Timestamp9 = string;
export type Type9 = "query.performance";
export type ProtocolVersion10 = 1;
export type RequestId10 = string;
export type Timestamp10 = string;
export type Type10 = "query.experiments";
export type ProtocolVersion11 = 1;
export type RequestId11 = string;
export type Timestamp11 = string;
export type Type11 = "query.design";
export type ProtocolVersion12 = 1;
export type RequestId12 = string;
export type Timestamp12 = string;
export type Type12 = "query.events";
export type AfterSequence = number;
export type BeforeSequence = number | null;
export type TimeoutMs = number;
export type ProtocolVersion13 = 1;
export type RequestId13 = string;
export type Timestamp13 = string;
export type Type13 = "subscribe";
export type AfterSequence1 = number;
export type Tail = number | null;
export type ProtocolVersion14 = 1;
export type RequestId14 = string;
export type Timestamp14 = string;
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
export type Action = "pause" | "resume" | "steer";
export type Status = "pending" | "consumed";
export type Question = string;
export type Answer = string;
export type Effect = "none";
export type ThreadId1 = string | null;
export type ThreadId2 = string;
export type Title1 = string;
export type Driver1 = string;
export type Provider1 = string;
export type Model1 = string;
export type Provider2 = string;
export type Model2 = string;
export type Source1 = "run" | "role" | "suggested";
export type Default = boolean;
export type Models = ChatModelOption[];
export type Providers = ChatProviderOptions[];
export type RunsDir = string;
export type InputPath = string;
export type ExperimentName = string;
export type RepositoryOwner = string | null;
export type RepositoryName = string;
/**
 * Supported GitHub repository visibility values.
 */
export type RepositoryVisibility = "private" | "public" | "internal";
/**
 * Selectable terminal UI themes.
 */
export type TuiTheme =
  | "dark"
  | "light"
  | "solarized-dark"
  | "solarized-light"
  | "catppuccin-mocha"
  | "catppuccin-latte"
  | "high-contrast-dark"
  | "high-contrast-light";
export type ProtocolVersion15 = 1;
export type RunId = string;
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
 */
export type RunStatus = "starting" | "running" | "pausing" | "paused" | "completed" | "failed";
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
export type Mode1 = "thinking" | "responding" | "tool" | "waiting";
export type Summary1 = string;
export type Tool = string | null;
export type Driver2 = string | null;
export type Provider3 = string | null;
export type Model3 = string | null;
export type ActiveExecutions = ActiveAgentExecution[];
export type ChatThreads = ChatThreadInfo[];
export type ProtocolVersion16 = 1;
export type Sequence1 = number;
export type RunId1 = string;
export type Timestamp15 = string;
export type EventType =
  | "server_started"
  | "server_ready"
  | "configuration_failed"
  | "run_started"
  | "experiments_changed"
  | "run_interrupted"
  | "run_status_changed"
  | "chat"
  | "chat_thread_created"
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
  | "output"
  | "tool_call"
  | "tool_result"
  | "todo_update"
  | "usage_update"
  | "gate_started"
  | "gate_finished"
  | "workspace_snapshot"
  | "run_configured"
  | "framework_warning";
export type Text2 = string;
export type EventStatus =
  "active" | "answered" | "pending" | "consumed" | "completed" | "failed" | "cancelled" | "interrupted";
export type RoundLabel2 = string | null;
export type AgentKind2 = string | null;
export type InvocationId = string | null;
export type ExecutionId1 = string | null;
export type ChatThreadId = string | null;
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
      | RunInterruptedData
      | RunStatusChangedData
      | ExperimentsChangedData
      | ConfigurationFailedData
      | PhaseData
      | AgentOutputChunkData
      | SubprocessOutputData
      | JudgeResultData
      | BenchmarkResultData
      | RoundFinishedData
      | ToolCallData
      | ToolResultData
      | TodoUpdateData
      | UsageUpdateData
      | GateStartedData
      | GateFinishedData
      | WorkspaceSnapshotData
      | RunConfiguredData
      | FrameworkWarningData
    )
  | null;
export type Kind1 = "chat";
export type Answer1 = string;
export type ThreadTitle = string | null;
export type Kind2 = "chat_thread_created";
export type ThreadId3 = string;
export type Title2 = string;
export type Driver3 = string;
export type Provider4 = string;
export type Model4 = string;
export type CreatedAt = string;
export type Kind3 = "invocation_started";
export type SystemPrompt = string;
export type UserPrompt = string;
export type Kind4 = "invocation_finished";
export type Error1 = string | null;
export type Kind5 = "agent_execution_started";
export type Stage1 = string;
export type Attempt1 = number | null;
export type SystemPrompt1 = string;
export type UserPrompt1 = string;
export type Driver4 = string | null;
export type Provider5 = string | null;
export type Model5 = string | null;
export type Kind6 = "agent_execution_finished";
export type Error2 = string | null;
export type Kind7 = "output";
export type Stream = "stdout" | "stderr";
export type Source2 = string;
export type Content = string;
export type Kind8 = "server_ready";
export type SocketProtocol = "jsonl";
export type Kind9 = "run_started";
export type OuterLoop = string;
export type Input = string;
export type MaxRounds = number;
export type ExpectedRoles = string[];
export type Kind10 = "run_interrupted";
export type Reason = string;
export type Signal = string | null;
export type Kind11 = "run_status_changed";
export type Kind12 = "experiments_changed";
export type Reason1 = "project_attached" | "active_hypothesis_changed" | "round_persisted";
export type Kind13 = "configuration_failed";
export type Code1 = string;
export type Stage2 = string;
export type Message = string;
export type Usage = string | null;
export type ExitCode = number;
export type Kind14 = "phase";
export type Phase = string;
export type Attempt2 = number | null;
export type Kind15 = "agent_output_chunk";
export type Channel = "assistant" | "analysis" | "tool" | "diagnostic" | "prompt";
export type Content1 = string;
export type Progress = string | null;
export type AgentLabel = string | null;
export type ElapsedSeconds = number;
export type InputTokens = number;
export type ContextWindow = number | null;
export type Kind16 = "subprocess_output";
export type ProcessId = string;
export type ProcessKind = string;
export type Stream1 = "stdout" | "stderr";
export type Content2 = string;
export type Kind17 = "judge_result";
export type Verdict = "pass" | "fail";
export type Feedback = string;
export type Attempt3 = number;
export type Kind18 = "benchmark_result";
export type Metric = string;
export type Value = number;
export type Unit = string;
export type Kind19 = "round_finished";
export type Attempts = number;
export type JudgeVerdict = "pass" | "fail" | "skipped";
export type PerfMetric = number | null;
export type PerfUnit = string | null;
export type ProfileSkipped = boolean;
export type Kind20 = "tool_call";
export type Tool1 = string;
export type CallId = string | null;
export type Kind21 = "tool_result";
export type Tool2 = string;
export type CallId1 = string | null;
export type Content3 = string;
export type IsError = boolean;
export type Payload = (CommandResultPayload | JsonResultPayload) | null;
export type Kind22 = "command";
export type Stdout = string;
export type Stderr = string;
export type ExitCode1 = number | null;
export type Duration = number | null;
export type Kind23 = "json";
export type Value1 =
  | {
      [k: string]: unknown;
    }
  | unknown[];
export type Kind24 = "todo_update";
export type Content4 = string;
export type Status1 = string;
export type Todos = TodoItemData[];
export type Kind25 = "usage_update";
export type InputTokens1 = number;
export type ContextWindow1 = number | null;
export type Model6 = string | null;
export type Kind26 = "gate_started";
/**
 * Closed set of framework-owned gates a candidate passes through.
 */
export type GateKind = "validation" | "accuracy" | "benchmark";
export type Recipe = string | null;
export type Command = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel = string | null;
export type Kind27 = "gate_finished";
export type Recipe1 = string | null;
export type Reused = boolean;
export type Metric1 = string | null;
export type Value2 = number | null;
export type Unit1 = string | null;
export type OutputTail = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource1 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel1 = string | null;
export type Kind28 = "workspace_snapshot";
export type Label = string;
export type Commit = string | null;
export type Baseline = string | null;
export type ExcludedPaths = string[];
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource2 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type Kind29 = "run_configured";
export type RunLogPath = string;
export type ProjectRoot = string;
export type Model7 = string | null;
export type Objective = string | null;
export type SearchPolicy = string | null;
export type BenchmarkContract = boolean;
export type ParetoObjectives = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource3 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type Kind30 = "framework_warning";
export type Summary2 = string;
export type Detail1 = string | null;
/**
 * Closed set of framework subsystems that emit framework events.
 */
export type FrameworkSource4 = "gates" | "git_tracking" | "loop" | "gpu" | "skypilot" | "other";
export type SourceLabel2 = string | null;
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
export type Title3 = string | null;
export type Claim = string | null;
export type Action1 = string | null;
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
export type Kept = boolean | null;
export type StrategyDisposition = ("available" | "parked" | "abandoned") | null;
export type StrategyReason = string | null;
export type Active = boolean;
export type Experiments = HypothesisEntry[];
export type ExperimentsReady = boolean | null;
export type Round2 = number;
export type Commit2 = string | null;
export type Files = DesignFileChange[] | null;
export type Path = string;
export type Change = "added" | "modified" | "deleted" | "renamed";
export type RenamedFrom = string | null;
export type Design = DesignRound[];
export type DesignReady = boolean | null;
export type ServerMessage = SubscribedMessage | EventMessage | EventBatchMessage | ProtocolErrorMessage;
export type Type14 = "subscribed";
export type RequestId15 = string;
export type RunId2 = string;
export type LatestSequence = number;
export type Type15 = "event";
export type Type16 = "event_batch";
export type Events1 = RunEvent[];
export type ThroughSequence = number;
export type ActiveExecutions1 = ActiveAgentExecution[];
export type HistoryAfterSequence = number;
export type Type17 = "protocol_error";
export type RequestId16 = string | null;
export type Code2 = string;
export type Message1 = string;

export interface ProtocolDocument {
  request: Request;
  response: Response;
  event: RunEvent;
  snapshot: RunSnapshot;
  server_message: ServerMessage;
  [k: string]: unknown;
}
export interface PauseCommand {
  protocol_version?: ProtocolVersion;
  request_id?: RequestId;
  timestamp?: Timestamp;
  type?: Type;
  mode?: Mode;
}
export interface ResumeCommand {
  protocol_version?: ProtocolVersion1;
  request_id?: RequestId1;
  timestamp?: Timestamp1;
  type?: Type1;
}
export interface SteerCommand {
  protocol_version?: ProtocolVersion2;
  request_id?: RequestId2;
  timestamp?: Timestamp2;
  type?: Type2;
  text: Text;
}
export interface SnapshotQuery {
  protocol_version?: ProtocolVersion3;
  request_id?: RequestId3;
  timestamp?: Timestamp3;
  type?: Type3;
}
export interface ChatQuery {
  protocol_version?: ProtocolVersion4;
  request_id?: RequestId4;
  timestamp?: Timestamp4;
  type?: Type4;
  text: Text1;
  thread_id?: ThreadId;
}
/**
 * Create a new experiment-chat thread with its own agent selection.
 *
 * Omitted fields resolve to the run's configured driver, provider, and
 * model. The response carries the resolved settings and thread identity.
 * ``driver`` exists for completeness and stays validated when supplied, but
 * which driver backs a run is a deployment detail: clients omit it so every
 * thread inherits the run's.
 */
export interface ChatThreadCreateQuery {
  protocol_version?: ProtocolVersion5;
  request_id?: RequestId5;
  timestamp?: Timestamp5;
  type?: Type5;
  driver?: Driver;
  provider?: Provider;
  model?: Model;
  title?: Title;
}
/**
 * Request the agent selections this run's experiment chat offers.
 */
export interface ChatOptionsQuery {
  protocol_version?: ProtocolVersion6;
  request_id?: RequestId6;
  timestamp?: Timestamp6;
  type?: Type6;
}
/**
 * Request the launch-directory configuration defaults a TUI applies.
 *
 * A terminal client resolves its theme from the run's configuration. Asking
 * over the control channel keeps TOML parsing in the server and saves the
 * launcher an extra Python process on the boot path.
 */
export interface TuiDefaultsQuery {
  protocol_version?: ProtocolVersion7;
  request_id?: RequestId7;
  timestamp?: Timestamp7;
  type?: Type7;
}
export interface HistoryQuery {
  protocol_version?: ProtocolVersion8;
  request_id?: RequestId8;
  timestamp?: Timestamp8;
  type?: Type8;
}
export interface PerformanceQuery {
  protocol_version?: ProtocolVersion9;
  request_id?: RequestId9;
  timestamp?: Timestamp9;
  type?: Type9;
}
/**
 * Request the hypothesis-level experiment log for the attached run.
 */
export interface ExperimentQuery {
  protocol_version?: ProtocolVersion10;
  request_id?: RequestId10;
  timestamp?: Timestamp10;
  type?: Type10;
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
  timestamp?: Timestamp11;
  type?: Type11;
}
export interface EventsQuery {
  protocol_version?: ProtocolVersion12;
  request_id?: RequestId12;
  timestamp?: Timestamp12;
  type?: Type12;
  after_sequence?: AfterSequence;
  before_sequence?: BeforeSequence;
  timeout_ms?: TimeoutMs;
}
export interface SubscribeRequest {
  protocol_version?: ProtocolVersion13;
  request_id?: RequestId13;
  timestamp?: Timestamp13;
  type?: Type13;
  after_sequence?: AfterSequence1;
  tail?: Tail;
}
export interface Response {
  protocol_version?: ProtocolVersion14;
  request_id: RequestId14;
  timestamp?: Timestamp14;
  ok?: Ok;
  error?: Error;
  diagnostic?: Diagnostic | null;
  ack?: CommandAck | null;
  chat?: ChatResult | null;
  chat_thread?: ChatThreadInfo | null;
  chat_options?: ChatOptions | null;
  tui_defaults?: InteractiveSetupDefaults | null;
  snapshot?: RunSnapshot | null;
  events?: Events;
  performance?: Performance;
  performance_context?: PerformanceContext | null;
  experiments?: Experiments;
  experiments_ready?: ExperimentsReady;
  design?: Design;
  design_ready?: DesignReady;
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
}
export interface CommandAck {
  action: Action;
  status: Status;
}
export interface ChatResult {
  question: Question;
  answer: Answer;
  effect?: Effect;
  thread_id?: ThreadId1;
}
/**
 * Resolved identity and agent settings of one experiment-chat thread.
 */
export interface ChatThreadInfo {
  thread_id: ThreadId2;
  title?: Title1;
  driver: Driver1;
  provider: Provider1;
  model: Model1;
}
/**
 * All offered chat selections grouped by provider.
 */
export interface ChatOptions {
  providers?: Providers;
}
/**
 * One supported chat provider and its suggested models.
 */
export interface ChatProviderOptions {
  provider: Provider2;
  models?: Models;
}
/**
 * One offered chat model and the source of its suggestion.
 */
export interface ChatModelOption {
  model: Model2;
  source: Source1;
  default?: Default;
}
/**
 * JSON contract passed to the interactive launch form.
 */
export interface InteractiveSetupDefaults {
  runs_dir: RunsDir;
  input_path: InputPath;
  experiment_name: ExperimentName;
  repository_owner: RepositoryOwner;
  repository_name: RepositoryName;
  visibility: RepositoryVisibility;
  theme: TuiTheme;
}
export interface RunSnapshot {
  protocol_version?: ProtocolVersion15;
  run_id: RunId;
  sequence: Sequence;
  status: RunStatus;
  agent_kind?: AgentKind;
  round_label?: RoundLabel;
  active_executions?: ActiveExecutions;
  chat_threads?: ChatThreads;
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
  driver?: Driver2;
  provider?: Provider3;
  model?: Model3;
}
/**
 * Complete current activity for an active agent execution.
 */
export interface AgentExecutionActivityData {
  kind?: Kind;
  mode: Mode1;
  summary: Summary1;
  tool?: Tool;
  [k: string]: unknown;
}
/**
 * One reproducible human, control, or invocation event.
 *
 * Frozen: a recorded event is a durable fact. Readers that need a variant
 * build one with ``model_copy(update=...)`` rather than mutating a shared
 * object, which lets ``EventStore`` replay history without copying it.
 */
export interface RunEvent {
  protocol_version?: ProtocolVersion16;
  sequence?: Sequence1;
  run_id?: RunId1;
  timestamp: Timestamp15;
  type: EventType;
  text?: Text2;
  diagnostic?: Diagnostic | null;
  status?: EventStatus | null;
  round_label?: RoundLabel2;
  agent_kind?: AgentKind2;
  invocation_id?: InvocationId;
  execution_id?: ExecutionId1;
  chat_thread_id?: ChatThreadId;
  data?: Data;
}
export interface ChatData {
  kind?: Kind1;
  answer: Answer1;
  thread_title?: ThreadTitle;
  [k: string]: unknown;
}
/**
 * Identity and resolved agent settings for one experiment-chat thread.
 *
 * Replayed by clients to rebuild the thread list; the default thread is
 * implicit and never records one of these.
 */
export interface ChatThreadCreatedData {
  kind?: Kind2;
  thread_id: ThreadId3;
  title?: Title2;
  driver: Driver3;
  provider: Provider4;
  model: Model4;
  created_at: CreatedAt;
  [k: string]: unknown;
}
export interface InvocationStartedData {
  kind?: Kind3;
  system_prompt: SystemPrompt;
  user_prompt: UserPrompt;
  [k: string]: unknown;
}
export interface InvocationFinishedData {
  kind?: Kind4;
  result?: Result;
  error?: Error1;
  [k: string]: unknown;
}
export interface Result {
  [k: string]: unknown;
}
/**
 * Semantic context for one prompt-to-result agent execution.
 */
export interface AgentExecutionStartedData {
  kind?: Kind5;
  stage: Stage1;
  attempt?: Attempt1;
  system_prompt?: SystemPrompt1;
  user_prompt?: UserPrompt1;
  activity: AgentExecutionActivityData;
  driver?: Driver4;
  provider?: Provider5;
  model?: Model5;
  [k: string]: unknown;
}
/**
 * Terminal result for one agent execution.
 */
export interface AgentExecutionFinishedData {
  kind?: Kind6;
  result?: Result1;
  error?: Error2;
  [k: string]: unknown;
}
export interface Result1 {
  [k: string]: unknown;
}
export interface OutputData {
  kind?: Kind7;
  stream: Stream;
  source?: Source2;
  content: Content;
  [k: string]: unknown;
}
export interface ServerReadyData {
  kind?: Kind8;
  socket_protocol?: SocketProtocol;
  [k: string]: unknown;
}
export interface RunStartedData {
  kind?: Kind9;
  outer_loop: OuterLoop;
  input: Input;
  max_rounds: MaxRounds;
  expected_roles?: ExpectedRoles;
  [k: string]: unknown;
}
export interface RunInterruptedData {
  kind?: Kind10;
  reason: Reason;
  signal?: Signal;
  [k: string]: unknown;
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
  kind?: Kind11;
  status: RunStatus;
  previous: RunStatus;
  [k: string]: unknown;
}
export interface ExperimentsChangedData {
  kind?: Kind12;
  reason: Reason1;
  [k: string]: unknown;
}
export interface ConfigurationFailedData {
  kind?: Kind13;
  code: Code1;
  stage: Stage2;
  message: Message;
  usage?: Usage;
  exit_code: ExitCode;
  [k: string]: unknown;
}
export interface PhaseData {
  kind?: Kind14;
  phase: Phase;
  attempt?: Attempt2;
  [k: string]: unknown;
}
export interface AgentOutputChunkData {
  kind?: Kind15;
  channel: Channel;
  content: Content1;
  status?: AgentStatusData | null;
  [k: string]: unknown;
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
  elapsed_seconds?: ElapsedSeconds;
  input_tokens?: InputTokens;
  context_window?: ContextWindow;
  [k: string]: unknown;
}
export interface SubprocessOutputData {
  kind?: Kind16;
  process_id: ProcessId;
  process_kind: ProcessKind;
  stream: Stream1;
  content: Content2;
  [k: string]: unknown;
}
export interface JudgeResultData {
  kind?: Kind17;
  verdict: Verdict;
  feedback: Feedback;
  attempt: Attempt3;
  [k: string]: unknown;
}
export interface BenchmarkResultData {
  kind?: Kind18;
  metric: Metric;
  value: Value;
  unit: Unit;
  [k: string]: unknown;
}
export interface RoundFinishedData {
  kind?: Kind19;
  attempts: Attempts;
  judge_verdict: JudgeVerdict;
  perf_metric?: PerfMetric;
  perf_unit?: PerfUnit;
  profile_skipped?: ProfileSkipped;
  [k: string]: unknown;
}
export interface ToolCallData {
  kind?: Kind20;
  tool: Tool1;
  call_id?: CallId;
  args?: Args;
  status?: AgentStatusData | null;
  [k: string]: unknown;
}
export interface Args {
  [k: string]: unknown;
}
export interface ToolResultData {
  kind?: Kind21;
  tool: Tool2;
  call_id?: CallId1;
  content: Content3;
  is_error?: IsError;
  payload?: Payload;
  [k: string]: unknown;
}
/**
 * Structured result of a command-style tool execution.
 */
export interface CommandResultPayload {
  kind?: Kind22;
  stdout: Stdout;
  stderr: Stderr;
  exit_code?: ExitCode1;
  duration?: Duration;
  [k: string]: unknown;
}
/**
 * A tool result that is a JSON object or array, already parsed.
 */
export interface JsonResultPayload {
  kind?: Kind23;
  value: Value1;
  [k: string]: unknown;
}
export interface TodoUpdateData {
  kind?: Kind24;
  todos?: Todos;
  [k: string]: unknown;
}
export interface TodoItemData {
  content: Content4;
  status: Status1;
  [k: string]: unknown;
}
export interface UsageUpdateData {
  kind?: Kind25;
  input_tokens: InputTokens1;
  context_window?: ContextWindow1;
  model?: Model6;
  [k: string]: unknown;
}
/**
 * One framework gate began evaluating the current candidate.
 */
export interface GateStartedData {
  kind?: Kind26;
  gate: GateKind;
  recipe?: Recipe;
  command?: Command;
  source?: FrameworkSource;
  source_label?: SourceLabel;
  [k: string]: unknown;
}
/**
 * Outcome of one framework gate; envelope status carries pass or fail.
 *
 * ``metric``/``value``/``unit`` are set only on a passing benchmark gate.
 * ``unit`` keeps the historical fallback of the metric name when the
 * contract declares no unit. ``output_tail`` carries the trailing command
 * output on failure.
 */
export interface GateFinishedData {
  kind?: Kind27;
  gate: GateKind;
  recipe?: Recipe1;
  reused?: Reused;
  metric?: Metric1;
  value?: Value2;
  unit?: Unit1;
  output_tail?: OutputTail;
  source?: FrameworkSource1;
  source_label?: SourceLabel1;
  [k: string]: unknown;
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
  kind?: Kind28;
  label?: Label;
  commit?: Commit;
  baseline?: Baseline;
  excluded_paths?: ExcludedPaths;
  source?: FrameworkSource2;
  [k: string]: unknown;
}
/**
 * One per run: the resolved configuration a loop starts with.
 */
export interface RunConfiguredData {
  kind?: Kind29;
  run_log_path: RunLogPath;
  project_root: ProjectRoot;
  model?: Model7;
  objective?: Objective;
  search_policy?: SearchPolicy;
  benchmark_contract?: BenchmarkContract;
  pareto_objectives?: ParetoObjectives;
  source?: FrameworkSource3;
  [k: string]: unknown;
}
/**
 * A non-fatal framework fault an operator should see.
 *
 * The server projection also lifts this payload into the wire event's
 * ``diagnostic`` field so diagnostic-oriented clients need no new handling.
 */
export interface FrameworkWarningData {
  kind?: Kind30;
  summary: Summary2;
  detail?: Detail1;
  source?: FrameworkSource4;
  source_label?: SourceLabel2;
  [k: string]: unknown;
}
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
  title?: Title3;
  claim?: Claim;
  action?: Action1;
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
  kept?: Kept;
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
  files?: Files;
}
/**
 * One workspace file a round's commit range touched.
 */
export interface DesignFileChange {
  path: Path;
  change: Change;
  renamed_from?: RenamedFrom;
}
export interface SubscribedMessage {
  type?: Type14;
  request_id: RequestId15;
  run_id: RunId2;
  latest_sequence: LatestSequence;
}
export interface EventMessage {
  type?: Type15;
  event: RunEvent;
}
export interface EventBatchMessage {
  type?: Type16;
  events: Events1;
  through_sequence?: ThroughSequence;
  active_executions?: ActiveExecutions1;
  history_after_sequence?: HistoryAfterSequence;
}
export interface ProtocolErrorMessage {
  type?: Type17;
  request_id?: RequestId16;
  code: Code2;
  message: Message1;
  diagnostic?: Diagnostic | null;
}
