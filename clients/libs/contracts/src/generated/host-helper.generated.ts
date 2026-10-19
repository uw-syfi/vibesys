/* Generated from the Python host helper models (server.host_api). Do not edit. */

export type Request =
  | HelloRequest
  | InstancesListRequest
  | InstancesInspectRequest
  | InstancesWatchRequest
  | InstancesUnwatchRequest
  | TasksListRequest
  | RunsListRequest
  | RunStartRequest
  | RunStopRequest
  | RunConnectRequest
  | PingRequest
  | PathsCompleteRequest
  | LogsListRequest
  | LogsReadRequest
  | LogsCancelRequest
  | HarnessesListRequest
  | RunsReadEventsRequest
  | RunsEventsForSpanRequest
  | RunsReadTailRequest
  | RunsSummaryRequest
  | MachineDescribeRequest
  | CheckoutInspectRequest
  | RunPreflightRequest
  | ExamplesListRequest
  | ProjectCreateFromExampleRequest
  | ProjectCopyOutRequest
  | ComputeListRequest
  | ComputeTestClusterRequest
  | ComputeAddClusterRequest
  | ComputeRemoveClusterRequest
  | AgentLoginsListRequest
  | AgentLoginsStartRequest
  | ProjectTaskGoalRequest
  | ExamplesDestinationRequest
  | UsageWindowRequest;
export type RequestId = string;
export type DeadlineMs = number;
export type Type = "hello";
/**
 * @minItems 2
 * @maxItems 2
 */
export type ClientVersions = [unknown, unknown];
export type Features = string[];
export type AppVersion = string;
export type RequestId1 = string;
export type DeadlineMs1 = number;
export type Type1 = "instances.list";
export type RequestId2 = string;
export type DeadlineMs2 = number;
export type Type2 = "instances.inspect";
export type InstanceId = string;
export type IncludeLog = boolean;
export type RequestId3 = string;
export type DeadlineMs3 = number;
export type Type3 = "instances.watch";
export type RequestId4 = string;
export type DeadlineMs4 = number;
export type Type4 = "instances.unwatch";
export type WatchId = string;
export type RequestId5 = string;
export type DeadlineMs5 = number;
export type Type5 = "tasks.list";
export type Project = string;
export type RequestId6 = string;
export type DeadlineMs6 = number;
export type Type6 = "runs.list";
export type Project1 = string;
export type DigestRuns = string[] | null;
export type WithDashboard = boolean | null;
export type RequestId7 = string;
export type DeadlineMs7 = number;
export type Type7 = "run.start";
export type Project2 = string;
export type Task = string | null;
export type Args = string[];
export type Resume = ResumeRun | ResumeLatest | null;
export type RunId = string;
export type Latest = true;
export type Provider = string;
export type Model = string;
export type Rounds = number | null;
export type TimeLimitS = number | null;
/**
 * Where a run's results go: kept on the machine, or also published to GitHub.
 */
export type RunResults = "keep" | "publish_github";
export type Role = string;
export type Model1 = string | null;
/**
 * A role's reasoning effort; ``default`` keeps the configured one.
 */
export type ReasoningEffort = "default" | "low" | "medium" | "high";
export type Roles = RoleOption[];
export type Agents = {
  [k: string]: HarnessChoice;
} | null;
export type MeasureOn = (SameMachine | SlurmTarget | ModalTarget) | null;
export type Kind = "same_machine";
export type Kind1 = "slurm";
export type Cluster = string;
export type Kind2 = "modal";
export type RequestId8 = string;
export type DeadlineMs8 = number;
export type Type8 = "run.stop";
export type InstanceId1 = string;
export type Force = boolean;
export type RequestId9 = string;
export type DeadlineMs9 = number;
export type Type9 = "run.connect";
export type InstanceId2 = string;
/**
 * Which run-protocol connection a run channel carries (``WP-ROLES``).
 */
export type RunRole = "control" | "subscribe" | "chat";
export type RequestId10 = string;
export type DeadlineMs10 = number;
export type Type10 = "ping";
export type RequestId11 = string;
export type DeadlineMs11 = number;
export type Type11 = "paths.complete";
export type Prefix = string;
export type RequestId12 = string;
export type DeadlineMs12 = number;
export type Type12 = "logs.list";
export type InstanceId3 = string;
export type SpanId = string | null;
export type RequestId13 = string;
export type DeadlineMs13 = number;
export type Type13 = "logs.read";
export type InstanceId4 = string;
export type StreamId = string;
export type Start = LogOffset | LogTail;
export type Offset = number;
export type TailBytes = number;
export type MaxBytes = number;
export type WaitMs = number;
export type RequestId14 = string;
export type DeadlineMs14 = number;
export type Type14 = "logs.cancel";
export type ReadId = string;
export type RequestId15 = string;
export type DeadlineMs15 = number;
export type Type15 = "harnesses.list";
export type WithGenerations = boolean | null;
export type RequestId16 = string;
export type DeadlineMs16 = number;
export type Type16 = "runs.read_events";
export type Project3 = string;
export type RunId1 = string;
export type FromSequence = number;
export type Max = number;
export type WithTotals = boolean | null;
export type RequestId17 = string;
export type DeadlineMs17 = number;
export type Type17 = "runs.events_for_span";
export type Project4 = string;
export type RunId2 = string;
export type RequestedSpanId = string;
export type FromSequence1 = number;
export type Max1 = number;
export type RequestId18 = string;
export type DeadlineMs18 = number;
export type Type18 = "runs.read_tail";
export type Project5 = string;
export type RunId3 = string;
export type BeforeSequence = number | null;
export type Tail = number;
export type RequestId19 = string;
export type DeadlineMs19 = number;
export type Type19 = "runs.summary";
export type Project6 = string;
export type RunId4 = string;
export type Offset1 = number;
export type RequestId20 = string;
export type DeadlineMs20 = number;
export type Type20 = "machine.describe";
export type RequestId21 = string;
export type DeadlineMs21 = number;
export type Type21 = "checkout.inspect";
export type Path = string;
export type RequestId22 = string;
export type DeadlineMs22 = number;
export type Type22 = "run.preflight";
export type Project7 = string;
export type Task1 = string | null;
export type Args1 = string[];
export type Agents1 = {
  [k: string]: HarnessChoice;
} | null;
export type MeasureOn1 = (SameMachine | SlurmTarget | ModalTarget) | null;
export type WithRoles = boolean | null;
export type WithProjectClean = boolean | null;
export type WithBackend = boolean | null;
export type WithCauses = boolean | null;
export type RequestId23 = string;
export type DeadlineMs23 = number;
export type Type23 = "examples.list";
export type WithCatalogue = boolean | null;
export type RequestId24 = string;
export type DeadlineMs24 = number;
export type Type24 = "project.create_from_example";
export type Example = string;
export type Destination = string;
export type RequestId25 = string;
export type DeadlineMs25 = number;
export type Type25 = "project.copy_out";
export type Source = string;
export type Destination1 = string;
export type RequestId26 = string;
export type DeadlineMs26 = number;
export type Type26 = "compute.list";
export type RequestId27 = string;
export type DeadlineMs27 = number;
export type Type27 = "compute.test_cluster";
export type Name = string;
export type Reach = SshReach | LoginNodeReach;
export type Kind3 = "ssh";
export type Host = string;
export type Kind4 = "login_node";
export type Workspace = string;
export type Partition = string | null;
export type Nodes = number | null;
export type TimeLimit = string | null;
/**
 * @maxItems 64
 */
export type SbatchOptions = string[];
/**
 * How a machine's GPUs run work; a closed set, ``cpu`` when it has none.
 */
export type Accelerator = "cpu" | "cuda" | "rocm" | "metal";
export type GpuModels = string[];
export type GpusPerNode = number;
export type RequestId28 = string;
export type DeadlineMs28 = number;
export type Type28 = "compute.add_cluster";
export type RequestId29 = string;
export type DeadlineMs29 = number;
export type Type29 = "compute.remove_cluster";
export type Name1 = string;
export type RequestId30 = string;
export type DeadlineMs30 = number;
export type Type30 = "agent_logins.list";
export type RequestId31 = string;
export type DeadlineMs31 = number;
export type Type31 = "agent_logins.start";
export type Provider1 = string;
export type RequestId32 = string;
export type DeadlineMs32 = number;
export type Type32 = "project.task_goal";
export type Project8 = string;
export type Task2 = string;
export type RequestId33 = string;
export type DeadlineMs33 = number;
export type Type33 = "examples.destination";
export type Example1 = string;
export type Destination2 = string;
export type RequestId34 = string;
export type DeadlineMs34 = number;
export type Type34 = "usage.window";
export type Project9 = string;
/**
 * @maxItems 100
 */
export type RunIds = string[];
export type Since = string;
export type Until = string;
export type Message =
  | HelloReply
  | InstancesListReply
  | InstancesInspectReply
  | InstancesWatchReply
  | InstancesUnwatchReply
  | TasksListReply
  | RunsListReply
  | RunStartReply
  | RunStopReply
  | RunConnectReply
  | PingReply
  | PathsCompleteReply
  | LogsListReply
  | LogsReadReply
  | LogsCancelReply
  | HarnessesListReply
  | RunsReadEventsReply
  | RunsEventsForSpanReply
  | RunsReadTailReply
  | RunsSummaryReply
  | MachineDescribeReply
  | CheckoutInspectReply
  | RunPreflightReply
  | ExamplesListReply
  | ProjectCreateFromExampleReply
  | ProjectCopyOutReply
  | ComputeListReply
  | ComputeTestClusterReply
  | ComputeAddClusterReply
  | ComputeRemoveClusterReply
  | AgentLoginsListReply
  | AgentLoginsStartReply
  | ProjectTaskGoalReply
  | ExamplesDestinationReply
  | UsageWindowReply
  | InstancesChanged
  | HelperError;
export type RequestId35 = string;
export type Type35 = "hello";
export type ProtocolVersion = number;
/**
 * Optional capabilities negotiated by ``hello``; a closed set per protocol version.
 *
 * A client calls a gated method only after ``hello`` returned its feature, so
 * a strict peer never sees a method it does not know.
 */
export type HelperFeature =
  | "instances_watch"
  | "paths_complete"
  | "instances_inspect"
  | "logs"
  | "logs_cancel"
  | "harnesses"
  | "logs_by_span"
  | "run_history"
  | "run_summary"
  | "run_history_pages"
  | "run_span_events"
  | "run_tail"
  | "machine_describe"
  | "checkout_inspect"
  | "preflight"
  | "run_options"
  | "examples"
  | "run_roles"
  | "project_clean"
  | "run_backend"
  | "run_causes"
  | "example_catalogue"
  | "run_digest"
  | "run_dashboard"
  | "harness_generations"
  | "run_role_agents"
  | "measurement_target"
  | "machine_compute"
  | "agent_logins"
  | "project_goal"
  | "example_destination"
  | "usage_window"
  | "run_trace";
export type Features1 = HelperFeature[];
export type VibesysVersion = string;
export type Root = string;
export type Commit = string | null;
export type Dirty = boolean | null;
export type RequestId36 = string;
export type Type36 = "instances.list";
export type Version = 5;
export type Version1 = 5;
export type Id = string;
/**
 * How far a live server has come; run status itself is on its socket.
 */
export type InstanceStatus = "starting" | "serving";
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
/**
 * Where a starting run is, in the order it gets there.
 *
 * Declaration order is the order: a starting run only moves to a later stage.
 */
export type StartupStage = "resolving" | "building_image" | "starting_container";
export type Done = number;
export type Total = number | null;
export type Label = string;
export type Code = string;
export type Stage = string;
export type Message1 = string;
export type SocketPath = string;
export type LogPath = string;
export type ProjectRoot = string;
export type RunId5 = string;
export type Pid = number;
export type StartedAt = number;
export type EndedAt = number | null;
/**
 * Who paused a paused run.
 */
export type PauseCause = "user" | "quota";
/**
 * Why a run ended, on its tombstone: vs-core's ``EndReason`` members and two of the shell's.
 *
 * The first seven are ``vs_core``'s ``EndReason`` values, copied because this module
 * may not import the core (``tests/server/test_run_end_record.py`` keeps them equal).
 */
export type RunEndOutcome =
  | "adopted"
  | "kept-input"
  | "no-winner"
  | "adoption-failed"
  | "stopped"
  | "deadline"
  | "finish-refused"
  | "failed"
  | "force_stopped";
export type Reason = string;
export type Resumable = boolean;
export type ResumeFromRound = number | null;
export type Hostname = string;
export type ProtocolVersion1 = 1;
export type VibesysVersion1 = string;
export type VibesysRoot = string | null;
export type Instances = LiveInstanceRecord[];
export type Ended = LiveInstanceRecord[];
export type Id1 = string;
export type Version2 = number | null;
export type Unreadable = UnreadableRecord[];
export type Unverified = string[];
export type RequestId37 = string;
export type Type37 = "instances.inspect";
export type Version3 = 1;
export type Id2 = string;
/**
 * What one coherent registry observation proved about an instance.
 */
export type InspectionState = "live" | "ended" | "unverified";
export type Version4 = 1;
export type Id3 = string;
export type ProjectRoot1 = string;
export type RunId6 = string;
export type Task3 = string | null;
export type Pid1 = number;
export type StartedAt1 = number;
export type Hostname1 = string;
export type ProtocolVersion2 = 1;
export type VibesysVersion2 = string;
export type VibesysRoot1 = string | null;
export type ControlSocketPath = string;
export type ServerLogPath = string;
export type ControlSocketPath1 = string;
export type ServerLogPath1 = string;
export type Log = (AvailableLogTail | MissingLogTail) | null;
export type State = "available";
export type Text = string;
export type Truncated = boolean;
export type State1 = "missing";
export type RequestId38 = string;
export type Type38 = "instances.watch";
export type WatchId1 = string;
export type RequestId39 = string;
export type Type39 = "instances.unwatch";
export type WatchId2 = string;
export type RequestId40 = string;
export type Type40 = "tasks.list";
export type Version5 = 1;
export type ProjectRoot2 = string;
export type Name2 = string;
export type Tasks = TaskSummary[];
export type RequestId41 = string;
export type Type41 = "runs.list";
export type ProjectRoot3 = string;
export type RunId7 = string;
export type Task4 = string | null;
export type DisplayName = string;
export type CreatedAt = string;
export type InstanceId5 = string | null;
export type Digest = (RunDigest | RunDigestUnreadable) | null;
export type Kind5 = "digest";
export type Ideas = number | null;
export type AdoptedRatio = number | null;
export type BestSoFarRatio = number | null;
export type DurationS = number | null;
export type Summary = string;
export type Resumable1 = boolean;
export type RoundsFinished = number;
/**
 * The journal's last word on a run: the terminal event it ends with, if it has one.
 */
export type RunClosing = "finished" | "failed" | "interrupted";
export type Ratio = number | null;
/**
 * What a run's idea came to, as the run page's chart marks it.
 */
export type IdeaVerdict = "kept" | "not_better" | "failed" | "running";
export type At = string | null;
/**
 * @maxItems 64
 */
export type Points = IdeaPoint[];
export type PointsOmitted = number;
export type FailedStreak = number;
export type LastEventAt = string | null;
export type Kind6 = "unreadable";
export type Message2 = string;
/**
 * Why a run needs its user, judged from its journal, its server and the time now.
 */
export type RunAttention = "none" | "failed" | "repeated_failure" | "stalled";
export type Runs = RunSummary[];
export type RequestId42 = string;
export type Type42 = "run.start";
export type Launch = LiveInstanceRecord | DetachedLaunchFailure;
export type Version6 = 1;
export type Outcome = "failed";
export type Code1 = string;
export type Stage1 = string;
export type Message3 = string;
export type ExitCode = number;
export type LogPath1 = string | null;
export type RequestId43 = string;
export type Type43 = "run.stop";
export type Version7 = 1;
export type Id4 = string;
/**
 * What ``vibesys instances stop`` observed.
 */
export type StopOutcome = "stopped" | "stopping" | "not_running" | "still_running" | "unsupported";
/**
 * How ``vibesys instances stop`` reached the server.
 */
export type StopRoute = "control_socket" | "signal";
export type RequestId44 = string;
export type Type44 = "run.connect";
export type RunId8 = string;
export type ProtocolVersion3 = number;
export type RequestId45 = string;
export type Type45 = "ping";
export type RequestId46 = string;
export type Type46 = "paths.complete";
export type Directory = string;
export type Name3 = string;
export type Path1 = string;
export type VibesysCheckout = boolean;
export type VibesysProject = boolean;
export type GitRepository = boolean;
export type Entries = PathEntry[];
export type Truncated1 = boolean;
export type RequestId47 = string;
export type Type47 = "logs.list";
export type Id5 = string;
export type JsonValue = unknown;
export type Command = string;
export type RetainedStart = number;
export type Size = number;
/**
 * How a stream was closed.
 */
export type LogStreamOutcome = "ended" | "failed" | "abandoned";
export type Streams = LogStream[];
export type RequestId48 = string;
export type Type48 = "logs.read";
export type Offset2 = number;
export type Data = string;
export type NextOffset = number;
export type Eof = boolean;
export type Closed = boolean;
export type TruncatedBefore = boolean;
export type Offset3 = number;
export type Length = number;
/**
 * Which of a command's output pipes wrote some bytes.
 */
export type LogPipe = "stdout" | "stderr";
export type Pipes = LogPipeSpan[];
export type RequestId49 = string;
export type Type49 = "logs.cancel";
export type ReadId1 = string;
export type RequestId50 = string;
export type Type50 = "harnesses.list";
export type Provider2 = string;
export type Model2 = string;
export type Default = boolean;
export type ProviderName = string;
export type ModelName = string | null;
export type Family = string | null;
export type Tier = number | null;
export type TierName = string | null;
/**
 * Who made a model: the closed set of makers whose models a provider's CLI runs.
 */
export type Maker = "anthropic" | "openai" | "google";
export type Id6 = string;
export type Name4 = string;
export type Order = number;
export type Harnesses = HarnessOption[];
export type RequestId51 = string;
export type Type51 = "runs.read_events";
export type Events = {
  [k: string]: JsonValue;
}[];
export type NextSequence = number;
export type Complete = boolean;
export type TotalEvents = number | null;
export type TotalBytes = number | null;
export type RequestId52 = string;
export type Type52 = "runs.events_for_span";
export type Events1 = {
  [k: string]: JsonValue;
}[];
export type NextSequence1 = number;
export type Complete1 = boolean;
export type Total1 = number;
export type RequestId53 = string;
export type Type53 = "runs.read_tail";
export type Events2 = {
  [k: string]: JsonValue;
}[];
export type Complete2 = boolean;
export type LastSequence = number;
export type RequestId54 = string;
export type Type54 = "runs.summary";
export type ThroughSequence = number;
export type EventCount = number;
export type JournalBytes = number;
/**
 * Whether a journal records the run trace, the spans a run's ideas and measurements are read from.
 */
export type TraceFormat = "recorded" | "predates_trace";
export type RoundsFinished1 = number;
export type SpanCount = number;
export type ReplyCount = number;
export type UsageCount = number;
export type RecordCount = number;
export type Input = number | null;
export type CacheRead = number | null;
export type CacheWrite = number | null;
export type Output = number | null;
export type Reasoning = number | null;
export type Overview = {
  [k: string]: JsonValue;
} | null;
export type Spans = {
  [k: string]: JsonValue;
}[];
export type SpanId1 = string;
export type Kind7 = string;
export type SchemaId = string;
export type Replies = TurnReply[];
export type SpanId2 = string;
export type Usage = SpanTokens[];
export type RunId9 = string | null;
export type ExecutionId = string | null;
export type SpanId3 = string | null;
export type RoundLabel = string | null;
export type Role1 = string | null;
export type Harness = string | null;
export type Model3 = string | null;
export type ReasoningEffort1 = string | null;
export type HarnessVersion = string | null;
export type StartedAt2 = string | null;
export type EndedAt1 = string | null;
export type CostUsd = number | null;
export type DurationMs = number | null;
export type Records = UsageRecord[];
export type NextOffset1 = number | null;
export type RequestId55 = string;
export type Type55 = "machine.describe";
export type Root1 = string;
export type Version8 = string;
export type Commit1 = string | null;
export type Dirty1 = boolean | null;
export type CpuCores = number;
export type Vendor = string;
export type Model4 = string;
export type Gpus = Gpu[];
export type Docker = DockerReachable | DockerUnreachable;
export type Reachable = true;
export type Version9 = string;
export type Reachable1 = false;
export type Reason1 = string;
export type HarnessClis = string[];
export type Live = number;
export type EndedToday = number;
export type RequestId56 = string;
export type Type56 = "checkout.inspect";
export type Inspection = CheckoutFound | CheckoutNotFound | CheckoutTooOld;
export type State2 = "found";
export type Root2 = string;
export type Version10 = string;
export type Commit2 = string | null;
export type Dirty2 = boolean | null;
export type Compatible = boolean;
export type State3 = "not_found";
export type State4 = "too_old";
export type Version11 = string;
export type Min = string;
export type RequestId57 = string;
export type Type57 = "run.preflight";
/**
 * What a preflight check is about; closed.
 */
export type CheckId =
  | "checkout_found"
  | "project_own_repository"
  | "project_clean"
  | "settings_valid"
  | "docker_reachable"
  | "agent_image"
  | "evaluator_available"
  | "measurement_target";
/**
 * How a check came out; Start is enabled exactly when none is ``fail``.
 */
export type CheckState = "ok" | "warn" | "fail";
export type Detail = string;
export type Fix = (ChooseCheckout | CopyOut | EditSetting | StartDocker | DeclareBuild | ConfigureCompute) | null;
export type Kind8 = "choose_checkout";
export type Kind9 = "copy_out";
export type SuggestedPath = string;
export type Kind10 = "edit_setting";
export type Key = string;
export type Kind11 = "start_docker";
export type Kind12 = "declare_build";
export type Manifest = string;
export type Outputs = string[];
export type Kind13 = "configure_compute";
export type Target = SlurmTarget | ModalTarget;
export type Cause =
  | (
      | BackendUnavailable
      | ProjectDirty
      | TaskNotFound
      | TaskUnusable
      | TaskNeedsProvisioning
      | TaskImageUnsupported
      | SysboxMissing
      | ConfigInvalid
      | RolesNotDeclared
      | ModelNotVetted
      | ArgumentsRejected
      | ArgumentsUnrecognized
      | OptionNotAccepted
      | GitHubUnavailable
      | MeasurementTargetNotConfigured
      | MeasurementTargetMisfit
    )
  | null;
export type Kind14 = "backend_unavailable";
export type Task5 = string;
/**
 * A compute backend a task targets (``vs_sandbox.ComputeBackend``'s values); closed.
 */
export type Backend = "cpu" | "cuda" | "rocm" | "metal" | "trainium";
export type Declared = Backend[];
export type Kind15 = "project_dirty";
export type Project10 = string;
export type Paths = string[];
export type Kind16 = "task_not_found";
export type Task6 = string;
export type Available = string[];
export type Kind17 = "task_unusable";
export type Task7 = string | null;
export type Problem = string;
export type Kind18 = "task_needs_provisioning";
export type Task8 = string;
export type Kind19 = "task_image_unsupported";
export type Task9 = string | null;
export type Problem1 = string;
export type Kind20 = "sysbox_missing";
export type Task10 = string;
export type Problem2 = string;
export type Kind21 = "config_invalid";
export type Problem3 = string;
export type Kind22 = "roles_not_declared";
export type Roles1 = string[];
export type Kind23 = "model_not_vetted";
export type Provider3 = string;
export type Model5 = string | null;
export type Vetted = string[];
export type Role2 = string | null;
export type Fallback = boolean;
export type Kind24 = "arguments_rejected";
export type Problem4 = string;
export type Kind25 = "arguments_unrecognized";
export type Arguments = string[];
export type Kind26 = "option_not_accepted";
/**
 * A typed New run option (``RunOptions``); closed.
 */
export type RunOption = "rounds" | "time_limit" | "results" | "role_model" | "role_reasoning_effort" | "role_agent";
export type Kind27 = "github_unavailable";
export type Problem5 = string;
export type Kind28 = "measurement_target_not_configured";
export type Target1 = SlurmTarget | ModalTarget;
export type Kind29 = "measurement_target_misfit";
export type Target2 = SameMachine | SlurmTarget | ModalTarget;
export type Checks = Check[];
export type Roles2 = string[] | null;
export type RequestId58 = string;
export type Type58 = "examples.list";
export type Version12 = 1;
export type Checkout1 = string;
export type Name5 = string;
export type Category = string;
export type Tasks1 = string[];
export type ExternalRepo = boolean;
export type TaskDetails = ExampleTask[] | null;
export type Task11 = string;
export type Metric = string;
export type Direction = "max" | "min";
export type Objectives = ExampleObjective[];
export type Goal = string;
export type GoalTruncated = boolean;
/**
 * What an example task optimizes; the first question New run asks.
 */
export type ExampleDomain = "llm-serving" | "data-structures" | "databases" | "microservices";
export type Accelerator1 = string;
export type Count = number;
/**
 * Who makes the hardware a task targets.
 */
export type AcceleratorVendor = "nvidia" | "amd" | "aws" | "apple" | "cpu";
export type Name6 = string;
/**
 * Who made a served model; ``OTHER`` for a maker with no entry yet.
 */
export type ModelMaker = "meta" | "qwen" | "deepseek" | "zhipu" | "moonshot" | "ai2" | "openai" | "other";
export type Name7 = string;
export type Description = string;
export type Examples = ExampleSummary[];
export type RequestId59 = string;
export type Type59 = "project.create_from_example";
export type Project11 = string;
export type Tasks2 = string[];
export type RequestId60 = string;
export type Type60 = "project.copy_out";
export type RequestId61 = string;
export type Type61 = "compute.list";
export type ConfigPath = string;
/**
 * Which file of the machine a listed cluster lives in.
 */
export type ClusterSource = "vibesys" | "slurm_toml";
export type Clusters = ListedCluster[];
export type TokenPresent = boolean;
/**
 * Where the machine holds its Modal token.
 */
export type ModalTokenSource = "environment" | "profile";
export type RequestId62 = string;
export type Type62 = "compute.test_cluster";
export type Result = ClusterTestPassed | ClusterTestFailed;
export type Outcome1 = "passed";
export type Partitions = string[];
export type Outcome2 = "failed";
/**
 * Which step of a connection test failed.
 */
export type ClusterTestStage = "reach" | "scheduler" | "workspace";
export type Detail1 = string;
export type RequestId63 = string;
export type Type63 = "compute.add_cluster";
export type RequestId64 = string;
export type Type64 = "compute.remove_cluster";
export type RequestId65 = string;
export type Type65 = "agent_logins.list";
export type Provider4 = string;
export type ProviderName1 = string;
export type Installed = boolean;
/**
 * Whether a harness is signed in, as far as its own tooling can say.
 */
export type LoginState = "signed_in" | "signed_out" | "unknown";
export type How = (AccountLogin | ApiKeyLogin) | null;
export type Kind30 = "account";
export type Kind31 = "api_key";
export type Variable = string | null;
export type File = string | null;
export type ApiKeyVariables = string[];
export type Detail2 = string;
export type CanStartLogin = boolean;
export type Logins = AgentLogin[];
export type RequestId66 = string;
export type Type66 = "agent_logins.start";
export type Result1 = LoginStarted | LoginUnavailable;
export type Outcome3 = "started";
export type Instruction = string | null;
export type Outcome4 = "unavailable";
/**
 * Why a login could not be started.
 */
export type LoginUnavailableReason = "not_installed" | "no_login_command" | "needs_terminal" | "start_failed";
export type Command1 = string | null;
export type Detail3 = string;
export type RequestId67 = string;
export type Type67 = "project.task_goal";
export type Task12 = string;
export type Objectives1 = ExampleObjective[];
export type Goal1 = string;
export type GoalTruncated1 = boolean;
export type RequestId68 = string;
export type Type68 = "examples.destination";
/**
 * What is at the place an example would be copied to.
 */
export type ExampleDestination = "absent" | "empty" | "example_project" | "occupied";
export type Project12 = string | null;
export type RequestId69 = string;
export type Type69 = "usage.window";
export type Since1 = string;
export type Until1 = string;
export type SpentUsd = number;
export type SpendPerHourUsd = number;
export type Turns = number;
export type UnpricedTurns = number;
export type UntimedTurns = number;
export type NewBests = number;
export type CostPerNewBestUsd = number | null;
export type RunId10 = string;
export type Task13 = string | null;
export type CostUsd1 = number;
/**
 * @maxItems 100
 */
export type ByRun = RunSpend[];
export type Role3 = string | null;
export type Harness1 = string | null;
export type Model6 = string | null;
export type CostUsd2 = number;
export type Turns1 = number;
/**
 * @maxItems 64
 */
export type ByAgent = AgentSpend[];
export type AgentsOmitted = number;
/**
 * @maxItems 100
 */
export type MissingRuns = string[];
export type Type70 = "instances.changed";
export type WatchId3 = string;
export type Type71 = "error";
export type RequestId70 = string | null;
/**
 * Why a request failed; a closed set.
 */
export type HelperErrorCode =
  | "handshake_required"
  | "incompatible"
  | "invalid_request"
  | "duplicate_request_id"
  | "feature_not_negotiated"
  | "deadline_exceeded"
  | "channel_busy"
  | "unknown_watch"
  | "project_missing"
  | "project_uninitialized"
  | "task_invalid"
  | "path_missing"
  | "path_not_directory"
  | "permission_denied"
  | "run_gone"
  | "connect_denied"
  | "connect_failed"
  | "unknown_instance"
  | "unknown_log_stream"
  | "log_store_corrupt"
  | "unknown_example"
  | "examples_unavailable"
  | "destination_exists"
  | "destination_nested"
  | "not_nested"
  | "project_create_failed"
  | "unknown_run"
  | "unvetted_harness"
  | "unknown_cluster"
  | "cluster_exists"
  | "cluster_read_only"
  | "cluster_test_failed"
  | "cluster_invalid"
  | "compute_config_unreadable"
  | "modal_not_configured"
  | "unknown_provider"
  | "internal";
export type Message4 = string;
export type Path2 = string | null;
export type SupportedVersions = [unknown, unknown] | null;
export type VibesysVersion3 = string | null;
/**
 * How a run channel's relay ended; each value has its own exit status.
 *
 * The relay core (``server.relay``) decides the outcome and maps it to the
 * helper's exit status; a client reads it from the ``ChannelEnd`` line.
 */
export type ChannelEndOutcome =
  | "client_closed"
  | "server_closed"
  | "run_gone"
  | "connect_denied"
  | "connect_failed"
  | "server_stalled"
  | "client_stalled"
  | "server_failed"
  | "client_failed"
  | "server_unresponsive";
export type ExitStatus = number;
export type Detail4 = string;
/**
 * One strict stderr line preserving an undisclosed instance cleanup failure.
 *
 * A successful ``run.start`` reply could not reach the client, so only this
 * diagnostic can tell an operator which live instance may need recovery. The
 * discriminated variants make result presence and nonterminal outcomes part of
 * the generated schema rather than Python-only validation.
 */
export type RunStartCompensationFailure =
  WorkerStartRefusedCompensationFailure | StopFailedCompensationFailure | StopUnconfirmedCompensationFailure;
export type Type72 = "run.start_compensation_failed";
export type ExitStatus1 = 12;
export type RequestId71 = string;
export type Detail5 = string;
export type Kind32 = "worker_start_refused";
export type InstanceId6 = string;
export type Type73 = "run.start_compensation_failed";
export type ExitStatus2 = 12;
export type RequestId72 = string;
export type Detail6 = string;
export type Kind33 = "stop_failed";
export type InstanceId7 = string;
export type Type74 = "run.start_compensation_failed";
export type ExitStatus3 = 12;
export type RequestId73 = string;
export type Detail7 = string;
export type Kind34 = "stop_unconfirmed";
export type Version13 = 1;
export type Id7 = string;
export type Outcome5 = "stopping" | "still_running" | "unsupported";

/**
 * Root schema document for the host helper: what a client sends and what it receives.
 */
export interface HostHelperDocument {
  request: Request;
  message: Message;
  channel_end: ChannelEnd;
  compensation_failure: RunStartCompensationFailure;
}
/**
 * The first message on every channel: which versions and features the client speaks.
 *
 * Its shape never changes across protocol versions, so any helper can parse
 * any client's ``hello`` and answer ``incompatible`` with a reason.
 */
export interface HelloRequest {
  request_id: RequestId;
  deadline_ms: DeadlineMs;
  type: Type;
  client_versions: ClientVersions;
  features?: Features;
  app_version: AppVersion;
}
/**
 * The live and recently ended detached servers on this host.
 */
export interface InstancesListRequest {
  request_id: RequestId1;
  deadline_ms: DeadlineMs1;
  type: Type1;
}
/**
 * Inspect one detached server without starting another host process.
 *
 * ``include_log`` is deprecated: it asks for a tail of ``server.log``, the detached
 * server's raw output, which is for a person at a terminal. No client UI may depend on
 * it; a run's logs come from its log store through ``logs.list`` and ``logs.read``. The
 * helper keeps answering it so older clients keep working, and removes it at the next
 * helper protocol version (2).
 */
export interface InstancesInspectRequest {
  request_id: RequestId2;
  deadline_ms: DeadlineMs2;
  type: Type2;
  instance_id: InstanceId;
  include_log?: IncludeLog;
}
/**
 * Push the listing now and again whenever it changes, until ``instances.unwatch``.
 */
export interface InstancesWatchRequest {
  request_id: RequestId3;
  deadline_ms: DeadlineMs3;
  type: Type3;
}
/**
 * Stop the watch that ``watch_id`` (its request's id) started.
 */
export interface InstancesUnwatchRequest {
  request_id: RequestId4;
  deadline_ms: DeadlineMs4;
  type: Type4;
  watch_id: WatchId;
}
/**
 * The tasks a project defines under ``.vibesys/tasks/``.
 */
export interface TasksListRequest {
  request_id: RequestId5;
  deadline_ms: DeadlineMs5;
  type: Type5;
  project: Project;
}
/**
 * A project's runs, with the phase of any server on this host that drives one.
 */
export interface RunsListRequest {
  request_id: RequestId6;
  deadline_ms: DeadlineMs6;
  type: Type6;
  project: Project1;
  digest_runs?: DigestRuns;
  with_dashboard?: WithDashboard;
}
/**
 * Start a detached run (``vibesys --detach``) in ``project``.
 *
 * ``args`` never carry ``UNVETTED_MODEL_FLAG`` (or an abbreviation of it): a run
 * started through the helper uses only vetted harnesses. With ``harness``, they
 * carry neither ``CLI_PROVIDER_FLAG`` nor ``MODEL_FLAG``, so the pair has one source.
 */
export interface RunStartRequest {
  request_id: RequestId7;
  deadline_ms: DeadlineMs7;
  type: Type7;
  project: Project2;
  task?: Task;
  args?: Args;
  resume?: Resume;
  harness?: HarnessChoice | null;
  options?: RunOptions | null;
  agents?: Agents;
  measure_on?: MeasureOn;
}
/**
 * Resume this run.
 */
export interface ResumeRun {
  run_id: RunId;
}
/**
 * Resume the project's current or latest run; the launch resolves it before detaching.
 */
export interface ResumeLatest {
  latest: Latest;
}
/**
 * One agent CLI provider and one model it runs.
 */
export interface HarnessChoice {
  provider: Provider;
  model: Model;
}
/**
 * What the New run screen sets on a run; ``None`` (or no role) keeps the configuration.
 */
export interface RunOptions {
  rounds?: Rounds;
  time_limit_s?: TimeLimitS;
  results?: RunResults | null;
  roles?: Roles;
}
/**
 * One agent role's model and reasoning effort; ``model`` ``None`` keeps the run's model.
 */
export interface RoleOption {
  role: Role;
  model?: Model1;
  reasoning_effort?: ReasoningEffort;
}
/**
 * Measure on the machine the agent works on.
 */
export interface SameMachine {
  kind: Kind;
}
/**
 * Measure on the Slurm cluster ``compute.list`` names ``cluster``.
 */
export interface SlurmTarget {
  kind: Kind1;
  cluster: Cluster;
}
/**
 * Measure on Modal, with the token present on the machine.
 */
export interface ModalTarget {
  kind: Kind2;
}
/**
 * Stop a live detached server (``vibesys instances stop``).
 */
export interface RunStopRequest {
  request_id: RequestId8;
  deadline_ms: DeadlineMs8;
  type: Type8;
  instance_id: InstanceId1;
  force?: Force;
}
/**
 * Make this channel a byte relay to a live run's control socket.
 */
export interface RunConnectRequest {
  request_id: RequestId9;
  deadline_ms: DeadlineMs9;
  type: Type9;
  instance_id: InstanceId2;
  role: RunRole;
}
/**
 * Liveness of the channel.
 */
export interface PingRequest {
  request_id: RequestId10;
  deadline_ms: DeadlineMs10;
  type: Type10;
}
/**
 * The directories that complete a typed path on the host.
 */
export interface PathsCompleteRequest {
  request_id: RequestId11;
  deadline_ms: DeadlineMs11;
  type: Type11;
  prefix: Prefix;
}
/**
 * The log streams recorded for one instance, live or ended.
 */
export interface LogsListRequest {
  request_id: RequestId12;
  deadline_ms: DeadlineMs12;
  type: Type12;
  instance_id: InstanceId3;
  span_id?: SpanId;
}
/**
 * One bounded chunk of a stream; at the end of an open stream, wait up to ``wait_ms`` for more.
 *
 * A wait ends as soon as bytes arrive or the stream closes, and always before
 * the request's deadline, so a follower keeps exactly one read in flight.
 */
export interface LogsReadRequest {
  request_id: RequestId13;
  deadline_ms: DeadlineMs13;
  type: Type13;
  instance_id: InstanceId4;
  stream_id: StreamId;
  start: Start;
  max_bytes: MaxBytes;
  wait_ms?: WaitMs;
}
/**
 * Read from this byte offset of the stream.
 */
export interface LogOffset {
  offset: Offset;
}
/**
 * Read the last ``tail_bytes`` bytes the stream holds, from the first line start in them.
 */
export interface LogTail {
  tail_bytes: TailBytes;
}
/**
 * Withdraw the ``logs.read`` whose request id is ``read_id``: a wait it is in ends now.
 *
 * The read still answers, with what it holds (an empty chunk at the end of the
 * stream), and its worker returns, so the channel can admit another request.
 * Naming a read that already answered, or no read, is not an error: withdrawal
 * races the reply by nature. Admitted even when the channel's workers are full.
 */
export interface LogsCancelRequest {
  request_id: RequestId14;
  deadline_ms: DeadlineMs14;
  type: Type14;
  read_id: ReadId;
}
/**
 * The vetted provider and model pairs ``run.start``'s ``harness`` accepts.
 */
export interface HarnessesListRequest {
  request_id: RequestId15;
  deadline_ms: DeadlineMs15;
  type: Type15;
  with_generations?: WithGenerations;
}
/**
 * A page of one run's recorded events, from its journal on disk, live or ended.
 */
export interface RunsReadEventsRequest {
  request_id: RequestId16;
  deadline_ms: DeadlineMs16;
  type: Type16;
  project: Project3;
  run_id: RunId1;
  from_sequence: FromSequence;
  max: Max;
  with_totals?: WithTotals;
}
/**
 * A page of the recorded events of one run-trace span, from its journal on disk.
 *
 * The events of ``span_id`` and of every span beneath it, oldest first: an event belongs
 * to the span it names in ``span_id`` and, when it names none, to the span whose
 * ``invocation`` is its ``execution_id``. For an agent turn that is the turn's output, tool
 * calls and results, activity and usage; for a flow, those of every turn under it. Span
 * events themselves are not included: the trace already carries them.
 */
export interface RunsEventsForSpanRequest {
  request_id: RequestId17;
  deadline_ms: DeadlineMs17;
  type: Type17;
  project: Project4;
  run_id: RunId2;
  span_id: RequestedSpanId;
  from_sequence?: FromSequence1;
  max: Max1;
}
/**
 * The newest recorded events of one run before a sequence, from its journal on disk.
 */
export interface RunsReadTailRequest {
  request_id: RequestId18;
  deadline_ms: DeadlineMs18;
  type: Type18;
  project: Project5;
  run_id: RunId3;
  before_sequence?: BeforeSequence;
  tail: Tail;
}
/**
 * One page of a run's summary: what its run level shows, as of the journal's end now.
 *
 * The summary is the journal's fold the client would compute itself (see
 * ``RunsSummaryReply``), kept beside the journal so a read costs what the summary holds, not
 * what the journal does. Its items (every span, then every reply) are read in pages: start at
 * ``offset`` 0 and continue at each page's ``next_offset`` until it is ``None``.
 */
export interface RunsSummaryRequest {
  request_id: RequestId19;
  deadline_ms: DeadlineMs19;
  type: Type19;
  project: Project6;
  run_id: RunId4;
  offset?: Offset1;
}
/**
 * What this host offers runs: its checkout, hardware, Docker, harness CLIs and runs.
 */
export interface MachineDescribeRequest {
  request_id: RequestId20;
  deadline_ms: DeadlineMs20;
  type: Type20;
}
/**
 * Whether ``path`` holds a VibeSys checkout, and whether this app can use it.
 */
export interface CheckoutInspectRequest {
  request_id: RequestId21;
  deadline_ms: DeadlineMs21;
  type: Type21;
  path: Path;
}
/**
 * Whether the run ``run.start`` would launch with these inputs can start, check by check.
 */
export interface RunPreflightRequest {
  request_id: RequestId22;
  deadline_ms: DeadlineMs22;
  type: Type22;
  project: Project7;
  task?: Task1;
  args?: Args1;
  harness?: HarnessChoice | null;
  options?: RunOptions | null;
  agents?: Agents1;
  measure_on?: MeasureOn1;
  with_roles?: WithRoles;
  with_project_clean?: WithProjectClean;
  with_backend?: WithBackend;
  with_causes?: WithCauses;
}
/**
 * The examples of the checkout this helper runs from, with their tasks.
 */
export interface ExamplesListRequest {
  request_id: RequestId23;
  deadline_ms: DeadlineMs23;
  type: Type23;
  with_catalogue?: WithCatalogue;
}
/**
 * Copy ``example`` into its own new Git repository at ``destination`` (absent or empty).
 */
export interface ProjectCreateFromExampleRequest {
  request_id: RequestId24;
  deadline_ms: DeadlineMs24;
  type: Type24;
  example: Example;
  destination: Destination;
}
/**
 * Copy ``source``, a folder inside another Git repository, into its own new one at ``destination``.
 */
export interface ProjectCopyOutRequest {
  request_id: RequestId25;
  deadline_ms: DeadlineMs25;
  type: Type25;
  source: Source;
  destination: Destination1;
}
/**
 * The machine's compute settings.
 */
export interface ComputeListRequest {
  request_id: RequestId26;
  deadline_ms: DeadlineMs26;
  type: Type26;
}
/**
 * Test whether ``cluster`` can be reached from the machine, and read its hardware.
 */
export interface ComputeTestClusterRequest {
  request_id: RequestId27;
  deadline_ms: DeadlineMs27;
  type: Type27;
  cluster: SlurmClusterSettings;
}
/**
 * One Slurm cluster as its machine reaches and uses it.
 */
export interface SlurmClusterSettings {
  name: Name;
  reach: Reach;
  workspace: Workspace;
  partition?: Partition;
  nodes?: Nodes;
  time_limit?: TimeLimit;
  sbatch_options?: SbatchOptions;
  hardware?: ClusterHardware | null;
}
/**
 * The cluster is reached from the machine over SSH, by a host alias in its ``~/.ssh/config``.
 */
export interface SshReach {
  kind: Kind3;
  host: Host;
}
/**
 * The machine is a login node of the cluster: its own scheduler commands reach it.
 */
export interface LoginNodeReach {
  kind: Kind4;
}
/**
 * What a cluster's nodes offer, as ``sinfo`` lists it or as its operator states it.
 */
export interface ClusterHardware {
  accelerator?: Accelerator | null;
  gpu_models?: GpuModels;
  gpus_per_node?: GpusPerNode;
}
/**
 * Save ``cluster``, once it passes the test again; a name already set up is refused.
 */
export interface ComputeAddClusterRequest {
  request_id: RequestId28;
  deadline_ms: DeadlineMs28;
  type: Type28;
  cluster: SlurmClusterSettings;
}
/**
 * Remove the cluster ``name`` from the settings file this helper edits.
 */
export interface ComputeRemoveClusterRequest {
  request_id: RequestId29;
  deadline_ms: DeadlineMs29;
  type: Type29;
  name: Name1;
}
/**
 * Each vetted harness's login on this machine.
 */
export interface AgentLoginsListRequest {
  request_id: RequestId30;
  deadline_ms: DeadlineMs30;
  type: Type30;
}
/**
 * Start the harness's own login on this machine.
 */
export interface AgentLoginsStartRequest {
  request_id: RequestId31;
  deadline_ms: DeadlineMs31;
  type: Type31;
  provider: Provider1;
}
/**
 * The goal text and objectives of one task of a project.
 */
export interface ProjectTaskGoalRequest {
  request_id: RequestId32;
  deadline_ms: DeadlineMs32;
  type: Type32;
  project: Project8;
  task: Task2;
}
/**
 * What ``project.create_from_example`` would find at ``destination`` for ``example``.
 */
export interface ExamplesDestinationRequest {
  request_id: RequestId33;
  deadline_ms: DeadlineMs33;
  type: Type33;
  example: Example1;
  destination: Destination2;
}
/**
 * What the named runs of a project spent in ``[since, until)``.
 *
 * ``until`` is "now" for the reply: the helper reads no clock for it, so a client names the time it
 * is asking at (or the end of a past window) and the answer is a function of the journals and
 * these two instants. A turn is placed at the time it ended (``UsageRecord.ended_at``).
 */
export interface UsageWindowRequest {
  request_id: RequestId34;
  deadline_ms: DeadlineMs34;
  type: Type34;
  project: Project9;
  run_ids: RunIds;
  since: Since;
  until: Until;
}
/**
 * The negotiated version and features, and what will start runs on this host.
 */
export interface HelloReply {
  request_id: RequestId35;
  type: Type35;
  protocol_version: ProtocolVersion;
  features: Features1;
  vibesys_version: VibesysVersion;
  checkout: Checkout | null;
}
/**
 * The VibeSys source checkout the helper runs from.
 */
export interface Checkout {
  root: Root;
  commit: Commit;
  dirty: Dirty;
}
/**
 * The registry's listing.
 */
export interface InstancesListReply {
  request_id: RequestId36;
  type: Type36;
  listing: InstanceList;
}
/**
 * ``vibesys instances list --json``: the detached servers on this node (version 5).
 */
export interface InstanceList {
  version?: Version;
  instances: Instances;
  ended?: Ended;
  unreadable?: Unreadable;
  unverified?: Unverified;
}
/**
 * What a detached server publishes about itself (version 5).
 *
 * ``run_id`` is allocated by the launcher before the server starts, so the
 * record names its run from the first time it is published. ``phase`` is the
 * run's lifecycle as the server folds it from its journal, and ``paused`` who
 * paused it while it is paused. When the server exits it leaves its last
 * record, with an ended phase, ``ended_at`` and ``end``, as a tombstone the
 * registry keeps for a day.
 */
export interface LiveInstanceRecord {
  version?: Version1;
  id: Id;
  status: InstanceStatus;
  phase: RunPhase;
  socket_path: SocketPath;
  log_path: LogPath;
  project_root: ProjectRoot;
  run_id: RunId5;
  pid: Pid;
  started_at: StartedAt;
  ended_at?: EndedAt;
  paused?: PauseCause | null;
  end?: EndRecord | null;
  hostname: Hostname;
  protocol_version?: ProtocolVersion1;
  vibesys_version: VibesysVersion1;
  vibesys_root?: VibesysRoot;
}
/**
 * A run's lifecycle status with the detail its status needs.
 *
 * ``stage`` (and, when the stage reports it, ``progress``) says where a
 * ``starting`` run is; ``reason`` says why a ``failed`` one ended. None of
 * them is present for any other status.
 */
export interface RunPhase {
  status: RunStatus;
  stage?: StartupStage | null;
  progress?: StartupProgress | null;
  reason?: FailureReason | null;
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
 * Why a run failed: the diagnostic it ended with.
 */
export interface FailureReason {
  code: Code;
  stage: Stage;
  message: Message1;
}
/**
 * How a run ended, as its tombstone reports it: the sidebar's Recent row and the ended banner.
 *
 * ``resumable`` says whether a resume continues the run: its durable record exists and
 * is not terminal. ``resume_from_round`` is the round that resume starts at, when the
 * server knew it (a crashed server did not say).
 */
export interface EndRecord {
  outcome: RunEndOutcome;
  reason: Reason;
  resumable: Resumable;
  resume_from_round?: ResumeFromRound;
}
/**
 * A registry entry this reader cannot interpret; ``version`` is ``None`` if it has none.
 */
export interface UnreadableRecord {
  id: Id1;
  version: Version2;
}
/**
 * One coherent inspection of a detached server and its retained artifacts.
 */
export interface InstancesInspectReply {
  request_id: RequestId37;
  type: Type37;
  inspection: InstanceInspection;
}
/**
 * ``vibesys instances inspect --json``: identity plus an optional diagnostic tail.
 */
export interface InstanceInspection {
  version?: Version3;
  id: Id2;
  state: InspectionState;
  record: LiveInstanceRecord | null;
  metadata: InstanceInspectionMetadata | null;
  control_socket_path: ControlSocketPath1;
  server_log_path: ServerLogPath1;
  log?: Log;
}
/**
 * Immutable detached-process facts retained after its live record disappears.
 */
export interface InstanceInspectionMetadata {
  version?: Version4;
  id: Id3;
  project_root: ProjectRoot1;
  run_id: RunId6;
  task?: Task3;
  pid: Pid1;
  started_at: StartedAt1;
  hostname: Hostname1;
  protocol_version?: ProtocolVersion2;
  vibesys_version: VibesysVersion2;
  vibesys_root?: VibesysRoot1;
  control_socket_path: ControlSocketPath;
  server_log_path: ServerLogPath;
}
/**
 * A bounded suffix of one instance's regular ``server.log`` file.
 */
export interface AvailableLogTail {
  state: State;
  text: Text;
  truncated: Truncated;
}
/**
 * The instance has no retained ``server.log`` file.
 */
export interface MissingLogTail {
  state: State1;
}
/**
 * The watch started; ``instances.changed`` for ``watch_id`` follows at once and on change.
 */
export interface InstancesWatchReply {
  request_id: RequestId38;
  type: Type38;
  watch_id: WatchId1;
}
/**
 * The watch ended; no ``instances.changed`` for ``watch_id`` follows.
 */
export interface InstancesUnwatchReply {
  request_id: RequestId39;
  type: Type39;
  watch_id: WatchId2;
}
/**
 * The project's tasks.
 */
export interface TasksListReply {
  request_id: RequestId40;
  type: Type40;
  tasks: TaskList;
}
/**
 * ``vibesys tasks --json``: the tasks of one project.
 */
export interface TaskList {
  version?: Version5;
  project_root: ProjectRoot2;
  tasks: Tasks;
}
/**
 * One task of a project.
 */
export interface TaskSummary {
  name: Name2;
}
/**
 * The project's runs.
 */
export interface RunsListReply {
  request_id: RequestId41;
  type: Type41;
  runs: RunList;
}
/**
 * A project's runs, oldest first.
 */
export interface RunList {
  project_root: ProjectRoot3;
  runs: Runs;
}
/**
 * One run of a project, from its durable manifest and this host's registry.
 */
export interface RunSummary {
  run_id: RunId7;
  task: Task4;
  display_name: DisplayName;
  created_at: CreatedAt;
  phase: RunPhase | null;
  instance_id: InstanceId5;
  digest?: Digest;
  attention?: RunAttention | null;
}
/**
 * What a run's journal says at a glance, for a list row; never a replay of the run.
 *
 * ``ideas`` counts the ideas the run page counts (one per hypothesis, probes excluded) and
 * ``adopted_ratio`` is the result the run's adoption span names, as the run page headlines it:
 * an improvement over the input (for a ``min`` objective, the input's value over the result's);
 * ``None`` when the run adopted nothing. The best raw reading of a gate is not it: that may be
 * an idea that later failed. ``best_so_far_ratio`` is what the page headlines before the run has
 * opened an adoption (while it works): the best idea that passed and beat the input, worded as an
 * improvement the same way; ``None`` before one does and once an adoption exists. ``ideas`` is
 * ``None`` for a journal that predates the run trace (``TraceFormat.PREDATES_TRACE``): it
 * records rounds, not ideas, so no count is true. ``duration_s`` runs from the journal's first
 * event to the last step it recorded; ``None`` for a run with no events. ``ending`` is the run's
 * ending record and ``closing`` its terminal event: both ``None`` for a run still going or one
 * whose server died without a word; they are how an ending is known once its tombstone expired.
 */
export interface RunDigest {
  kind: Kind5;
  ideas: Ideas;
  adopted_ratio: AdoptedRatio;
  best_so_far_ratio: BestSoFarRatio;
  duration_s: DurationS;
  ending: RunEndingFact | null;
  closing: RunClosing | null;
  dashboard?: RunDashboard | null;
}
/**
 * How a run's durable record stood as it ended, as its server reports it to the record.
 *
 * ``outcome`` is core's end reason when the run recorded a result, ``None`` when it ended
 * without one; ``summary`` is core's account of that result.
 */
export interface RunEndingFact {
  outcome: RunEndOutcome | null;
  summary?: Summary;
  resumable: Resumable1;
  rounds_finished: RoundsFinished;
}
/**
 * What a run's tile draws, folded from the journal in the digest's pass.
 *
 * ``points`` are the run page's chart (``performanceFold``): the ideas in the order their results
 * landed (the idea's measurement time, else when its last attempt ended or began; ties in the
 * order the ideas began), each with its verdict. A run with more than ``DASHBOARD_MAX_POINTS``
 * ideas keeps the newest points of the verdicts that matter to the line: the oldest ideas that
 * are not ``kept`` go first, then the oldest ``kept`` ones, and ``points_omitted`` counts the
 * ideas left out (the ideas count is ``len(points) + points_omitted``). ``failed_streak`` counts
 * the ideas at the end of the order that failed, with none passing since (a running idea is
 * skipped), over every idea and not only the ones kept. ``last_event_at`` is the journal's last
 * event, the only time fact a stalled run is judged from (``RunAttention``).
 */
export interface RunDashboard {
  points: Points;
  points_omitted: PointsOmitted;
  failed_streak: FailedStreak;
  last_event_at: LastEventAt;
}
/**
 * One idea of a run on the performance chart: its result against the input and its verdict.
 *
 * ``ratio`` is worded like ``RunDigest.adopted_ratio`` (an improvement over the input, for a
 * ``min`` objective the input's value over the result's); ``None`` when the idea has no
 * measurement against the input (it failed before one, or still works).
 *
 * ``at`` is when the idea's result landed, by the journal's event clock: the time of the gate event
 * that last wrote the reading the idea is plotted by, else of the idea's last event; ``None`` when
 * the journal's event has no readable time. It is the one definition of when a ``kept`` idea became
 * the best (``usage.window`` counts new bests by it); the span clock orders the chart but is not a
 * time of day.
 */
export interface IdeaPoint {
  ratio: Ratio;
  verdict: IdeaVerdict;
  at?: At;
}
/**
 * A run whose journal exists and could not be read; ``message`` says why, in one line.
 */
export interface RunDigestUnreadable {
  kind: Kind6;
  message: Message2;
}
/**
 * The started server's record, or why nothing started.
 */
export interface RunStartReply {
  request_id: RequestId42;
  type: Type42;
  launch: Launch;
}
/**
 * What ``vibesys --detach`` prints on stdout, as one line, when it starts nothing.
 *
 * On success the line is a ``LiveInstanceRecord`` instead; a reader tells them
 * apart by ``outcome``, which a record never has. ``code`` is a
 * ``DetachedLaunchErrorCode`` or the code of the run's configuration
 * diagnostic (``invalid_arguments``, ``resume_not_found``, ...), and
 * ``exit_code`` is the process's exit status.
 */
export interface DetachedLaunchFailure {
  version?: Version6;
  outcome?: Outcome;
  code: Code1;
  stage: Stage1;
  message: Message3;
  exit_code: ExitCode;
  log_path?: LogPath1;
  live_instance?: LiveInstanceRecord | null;
}
/**
 * What the stop observed.
 */
export interface RunStopReply {
  request_id: RequestId43;
  type: Type43;
  result: InstanceStopResult;
}
/**
 * ``vibesys instances stop --json``.
 */
export interface InstanceStopResult {
  version?: Version7;
  id: Id4;
  outcome: StopOutcome;
  route?: StopRoute | null;
}
/**
 * The channel is now a byte relay carrying the run protocol to this run.
 */
export interface RunConnectReply {
  request_id: RequestId44;
  type: Type44;
  run_id: RunId8;
  protocol_version: ProtocolVersion3;
}
/**
 * The channel is alive.
 */
export interface PingReply {
  request_id: RequestId45;
  type: Type45;
}
/**
 * Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``.
 */
export interface PathsCompleteReply {
  request_id: RequestId46;
  type: Type46;
  directory: Directory;
  entries: Entries;
  truncated: Truncated1;
}
/**
 * One directory that completes the typed path, with what it looks like.
 */
export interface PathEntry {
  name: Name3;
  path: Path1;
  vibesys_checkout: VibesysCheckout;
  vibesys_project: VibesysProject;
  git_repository: GitRepository;
}
/**
 * The instance's streams, oldest first.
 */
export interface LogsListReply {
  request_id: RequestId47;
  type: Type47;
  streams: Streams;
}
/**
 * One recorded log stream of an instance, as its store indexes it.
 */
export interface LogStream {
  id: Id5;
  owner: Owner;
  command: Command;
  retained_start: RetainedStart;
  size: Size;
  outcome: LogStreamOutcome | null;
}
export interface Owner {
  [k: string]: JsonValue;
}
/**
 * One chunk of the stream.
 */
export interface LogsReadReply {
  request_id: RequestId48;
  type: Type48;
  chunk: LogChunk;
}
/**
 * Bytes ``[offset, next_offset)`` of a stream, decoded as UTF-8 text.
 *
 * The helper cuts a chunk that more bytes follow after the last line end in
 * its window, or, with no line end there, before a character the window
 * splits; so ``data`` decodes without loss except for bytes that are not
 * UTF-8, which become U+FFFD. ``next_offset - offset`` counts bytes.
 */
export interface LogChunk {
  offset: Offset2;
  data: Data;
  next_offset: NextOffset;
  eof: Eof;
  closed: Closed;
  truncated_before: TruncatedBefore;
  pipes: Pipes;
}
/**
 * Bytes ``[offset, offset + length)`` of a stream were written by ``pipe``.
 */
export interface LogPipeSpan {
  offset: Offset3;
  length: Length;
  pipe: LogPipe;
}
/**
 * The withdrawal was taken: a read ``read_id`` named that was still waiting stops waiting.
 */
export interface LogsCancelReply {
  request_id: RequestId49;
  type: Type49;
  read_id: ReadId1;
}
/**
 * The vetted pairs, grouped by provider with each provider's default first.
 */
export interface HarnessesListReply {
  request_id: RequestId50;
  type: Type50;
  harnesses: Harnesses;
}
/**
 * A vetted provider and model pair a run may start with.
 */
export interface HarnessOption {
  provider: Provider2;
  model: Model2;
  default: Default;
  display?: HarnessDisplay | null;
}
/**
 * How a provider and model pair reads, as agentshim names it.
 */
export interface HarnessDisplay {
  provider_name: ProviderName;
  model_name?: ModelName;
  family?: Family;
  tier?: Tier;
  tier_name?: TierName;
  maker?: Maker | null;
  generation?: ModelGeneration | null;
}
/**
 * One generation of a maker's models, and where it ranks among the maker's.
 */
export interface ModelGeneration {
  id: Id6;
  name: Name4;
  order: Order;
}
/**
 * One page of a run's recorded events, oldest first.
 */
export interface RunsReadEventsReply {
  request_id: RequestId51;
  type: Type51;
  events: Events;
  next_sequence: NextSequence;
  complete: Complete;
  total_events?: TotalEvents;
  total_bytes?: TotalBytes;
}
/**
 * One page of a span's recorded events, oldest first.
 */
export interface RunsEventsForSpanReply {
  request_id: RequestId52;
  type: Type52;
  events: Events1;
  next_sequence: NextSequence1;
  complete: Complete1;
  total: Total1;
}
/**
 * The newest events before the request's ``before_sequence``, oldest first.
 */
export interface RunsReadTailReply {
  request_id: RequestId53;
  type: Type53;
  events: Events2;
  complete: Complete2;
  last_sequence: LastSequence;
}
/**
 * One page of a run's summary: the client's fold of its journal, to ``through_sequence``.
 *
 * Every field equals what a client reading the whole journal up to ``through_sequence`` folds
 * itself, which is the contract the helper's tests hold it to:
 *
 * - ``spans``: the run trace (``foldTrace``): each span as its latest span event left it, in
 *   the order the spans started, so each comes after its parent;
 * - ``overview``: the ``overview`` of the last ``run_started`` event that declares one
 *   (``foldOverview``), with no unit or direction a live server adds from run state;
 * - ``replies``: the last typed reply of each agent-turn span, by ``agent_execution_finished``
 *   events with a ``span_id``, in the order the spans first replied (``implementerOutcomes``);
 * - ``tokens`` and ``usage``: the run's spend, folded from ``turn_usage_recorded`` events by
 *   summing (``ledgerWith``): ``tokens`` over every turn, those the trace names and those it
 *   does not, ``usage`` over each turn span that is named, in the order the spans first spent;
 * - ``records``: one ``UsageRecord`` per ``turn_usage_recorded`` event, in journal order, whose
 *   tokens sum to ``tokens`` (``server.run_usage``);
 * - the idea rows, the measurement points and the chart are folds of ``spans`` and ``replies``
 *   the client already owns (``CandidateFold``, ``performanceFold``); nothing duplicates them.
 *
 * An event at or before the last folded sequence is skipped whole, as the client skips it. The
 * run level is final at ``through_sequence``. Events after it are the live tail: read them with
 * ``runs.read_events`` from ``through_sequence + 1``, or subscribe after ``through_sequence``. A
 * run still writing can move between the pages of one read: every page names its
 * ``through_sequence``, and a read whose pages disagree starts again.
 */
export interface RunsSummaryReply {
  request_id: RequestId54;
  type: Type54;
  through_sequence: ThroughSequence;
  event_count: EventCount;
  journal_bytes: JournalBytes;
  trace: TraceFormat;
  rounds_finished: RoundsFinished1;
  span_count: SpanCount;
  reply_count: ReplyCount;
  usage_count: UsageCount;
  record_count: RecordCount;
  tokens: TokenTally;
  overview: Overview;
  spans: Spans;
  replies: Replies;
  schemas: Schemas;
  usage: Usage;
  records: Records;
  next_offset: NextOffset1;
}
/**
 * Tokens spent, counted as the run protocol's ``turn_usage_recorded`` reports them.
 *
 * A count the provider did not report is ``None``, not zero: a sum is ``None`` until some turn
 * reports the count, and then adds only the turns that did, so a reader can tell "none" from
 * "zero" and a sum over turns with a missing count is a lower bound.
 */
export interface TokenTally {
  input: Input;
  cache_read: CacheRead;
  cache_write: CacheWrite;
  output: Output;
  reasoning: Reasoning;
}
/**
 * The typed reply an agent turn ended with, its schema named by ``schema_id``.
 *
 * ``kind``, the schema and ``value`` are the three fields of the run protocol's ``AgentReply``;
 * the schema is carried once per page in ``schemas`` because it is the repeated, large part.
 */
export interface TurnReply {
  span_id: SpanId1;
  kind: Kind7;
  schema_id: SchemaId;
  value: Value;
}
export interface Value {
  [k: string]: JsonValue;
}
export interface Schemas {
  [k: string]: {
    [k: string]: JsonValue;
  };
}
/**
 * The tokens the turns of one agent-turn span spent, summed.
 */
export interface SpanTokens {
  span_id: SpanId2;
  tally: TokenTally;
}
/**
 * What one agent turn spent, and who and when it was: the one definition of a usage record.
 *
 * A projection of the journal (``server.run_usage``): a ``turn_usage_recorded`` event joined to
 * the ``agent_execution_started`` of its ``execution_id``. A value the journal does not hold is
 * ``None``, never a guess: a turn whose execution start is missing has no role, harness, model,
 * effort, version or start. The run is a field so records of many runs fold together without
 * changing the fold. ``role`` and ``harness`` are open sets (the roles of a run's flow, the
 * providers agentshim runs) owned elsewhere, so this contract names no list of them.
 */
export interface UsageRecord {
  run_id: RunId9;
  execution_id: ExecutionId;
  span_id: SpanId3;
  round_label: RoundLabel;
  role: Role1;
  harness: Harness;
  model: Model3;
  reasoning_effort: ReasoningEffort1;
  harness_version: HarnessVersion;
  started_at: StartedAt2;
  ended_at: EndedAt1;
  tokens: TokenTally;
  cost_usd: CostUsd;
  duration_ms: DurationMs;
}
/**
 * The machine's description.
 */
export interface MachineDescribeReply {
  request_id: RequestId55;
  type: Type55;
  machine: MachineDescription;
}
/**
 * What one machine offers runs.
 */
export interface MachineDescription {
  checkout: MachineCheckout | null;
  hardware: Hardware;
  docker: Docker;
  harness_clis: HarnessClis;
  harness_cli_names?: HarnessCliNames;
  runs: RunCounts;
}
/**
 * The VibeSys checkout this helper runs from, which starts runs on this machine.
 */
export interface MachineCheckout {
  root: Root1;
  version: Version8;
  commit: Commit1;
  dirty: Dirty1;
}
/**
 * The machine's processors.
 */
export interface Hardware {
  cpu_cores: CpuCores;
  gpus: Gpus;
  accelerator: Accelerator;
}
/**
 * One GPU as its vendor's tool names it.
 */
export interface Gpu {
  vendor: Vendor;
  model: Model4;
}
/**
 * Docker's daemon answered, with its server version.
 */
export interface DockerReachable {
  reachable?: Reachable;
  version: Version9;
}
/**
 * Docker could not be reached, and why, in one line.
 */
export interface DockerUnreachable {
  reachable?: Reachable1;
  reason: Reason1;
}
export interface HarnessCliNames {
  [k: string]: string;
}
/**
 * Runs on this machine: live now, and ended since the host's local midnight.
 */
export interface RunCounts {
  live: Live;
  ended_today: EndedToday;
}
/**
 * What the path holds.
 */
export interface CheckoutInspectReply {
  request_id: RequestId56;
  type: Type56;
  inspection: Inspection;
}
/**
 * A VibeSys checkout at least ``MIN_CHECKOUT_VERSION``.
 *
 * ``compatible`` says whether it ships the host helper this app starts from a checkout.
 */
export interface CheckoutFound {
  state: State2;
  root: Root2;
  version: Version10;
  commit: Commit2;
  dirty: Dirty2;
  compatible: Compatible;
}
/**
 * The path holds no VibeSys checkout (or does not exist).
 */
export interface CheckoutNotFound {
  state: State3;
}
/**
 * A VibeSys checkout older than this app supports: ``version`` is below ``min``.
 */
export interface CheckoutTooOld {
  state: State4;
  version: Version11;
  min: Min;
}
/**
 * Every check, in ``CheckId`` order; Start is enabled exactly when none is ``fail``.
 */
export interface RunPreflightReply {
  request_id: RequestId57;
  type: Type57;
  checks: Checks;
  roles?: Roles2;
  backend?: Backend | null;
}
/**
 * One preflight check: its state, one line of detail, and the action that fixes it.
 */
export interface Check {
  id: CheckId;
  state: CheckState;
  detail: Detail;
  fix?: Fix;
  cause?: Cause;
}
/**
 * Pick another VibeSys checkout for this machine.
 */
export interface ChooseCheckout {
  kind: Kind8;
}
/**
 * Copy the project into its own repository (``project.copy_out``), at the suggested path.
 */
export interface CopyOut {
  kind: Kind9;
  suggested_path: SuggestedPath;
}
/**
 * Edit the named setting.
 */
export interface EditSetting {
  kind: Kind10;
  key: Key;
}
/**
 * Start Docker on the machine.
 */
export interface StartDocker {
  kind: Kind11;
}
/**
 * Declare a ``[build]`` producing ``outputs`` in the task manifest at ``manifest``.
 */
export interface DeclareBuild {
  kind: Kind12;
  manifest: Manifest;
  outputs: Outputs;
}
/**
 * Set up the cluster or Modal on the machine, in that machine's compute settings.
 */
export interface ConfigureCompute {
  kind: Kind13;
  target: Target;
}
/**
 * The machine cannot run the compute backend the task targets.
 *
 * A fact, not a sentence: the client words it for its own controls, the CLI for its flags.
 */
export interface BackendUnavailable {
  kind: Kind14;
  task: Task5;
  backend: Backend;
  declared: Declared;
}
/**
 * The project's repository has changes a run cannot start beside.
 *
 * A fact, not a sentence: the client words it. ``paths`` are relative to the project, sorted.
 */
export interface ProjectDirty {
  kind: Kind15;
  project: Project10;
  paths: Paths;
}
/**
 * The project has no task of that name.
 */
export interface TaskNotFound {
  kind: Kind16;
  task: Task6;
  available: Available;
}
/**
 * The task's files cannot be loaded; ``problem`` is the loader's own account (raw text).
 */
export interface TaskUnusable {
  kind: Kind17;
  task: Task7;
  problem: Problem;
}
/**
 * The task needs inputs copied into a project of its own, which this run does not do.
 */
export interface TaskNeedsProvisioning {
  kind: Kind18;
  task: Task8;
}
/**
 * The task's Dockerfile cannot be used the way this run would run; ``problem`` is raw text.
 */
export interface TaskImageUnsupported {
  kind: Kind19;
  task: Task9;
  problem: Problem1;
}
/**
 * The task runs Docker inside its sandbox, which needs a runtime this machine's Docker lacks.
 */
export interface SysboxMissing {
  kind: Kind20;
  task: Task10;
  problem: Problem2;
}
/**
 * The run's configuration (``agent.toml`` and what overrides it) is not valid.
 */
export interface ConfigInvalid {
  kind: Kind21;
  problem: Problem3;
}
/**
 * The configuration names agent roles the run's orchestration does not declare.
 */
export interface RolesNotDeclared {
  kind: Kind22;
  roles: Roles1;
}
/**
 * A model, or none, outside the vetted models of its provider.
 */
export interface ModelNotVetted {
  kind: Kind23;
  provider: Provider3;
  model: Model5;
  vetted: Vetted;
  role: Role2;
  fallback: Fallback;
}
/**
 * The run's argument parser refused the extra arguments; ``problem`` is its message (raw).
 */
export interface ArgumentsRejected {
  kind: Kind24;
  problem: Problem4;
}
/**
 * Extra arguments the run's search does not take, in the order given (raw).
 */
export interface ArgumentsUnrecognized {
  kind: Kind25;
  arguments: Arguments;
}
/**
 * The run's search does not take a typed option the screen set (for instance rounds).
 */
export interface OptionNotAccepted {
  kind: Kind26;
  option: RunOption;
}
/**
 * The run publishes to GitHub, and this machine has no signed-in account; ``problem`` is raw.
 */
export interface GitHubUnavailable {
  kind: Kind27;
  problem: Problem5;
}
/**
 * The cluster is not set up on the machine, or Modal has no token there.
 */
export interface MeasurementTargetNotConfigured {
  kind: Kind28;
  target: Target1;
}
/**
 * The target's hardware cannot run the compute backend the task targets.
 */
export interface MeasurementTargetMisfit {
  kind: Kind29;
  target: Target2;
  backend: Backend;
  offered: Accelerator;
}
/**
 * The checkout's example catalog.
 */
export interface ExamplesListReply {
  request_id: RequestId58;
  type: Type58;
  listing: ExampleList;
}
/**
 * ``vibesys example list --json``: every catalogued example, by category then name.
 */
export interface ExampleList {
  version?: Version12;
  checkout: Checkout1;
  examples: Examples;
}
/**
 * One example in ``vibesys example list --json``.
 */
export interface ExampleSummary {
  name: Name5;
  category: Category;
  tasks: Tasks1;
  external_repo: ExternalRepo;
  task_details?: TaskDetails;
}
/**
 * What one task of an example is about, read from its ``vibesys.input.toml`` and goal file.
 */
export interface ExampleTask {
  task: Task11;
  objectives: Objectives;
  goal: Goal;
  goal_truncated: GoalTruncated;
  domain: ExampleDomain;
  hardware: ExampleHardware;
  model: ExampleModel | null;
  workload: ExampleWorkload | null;
}
/**
 * One axis the task's ``objectives.toml`` declares.
 */
export interface ExampleObjective {
  metric: Metric;
  direction: Direction;
}
/**
 * The hardware a task targets.
 */
export interface ExampleHardware {
  accelerator: Accelerator1;
  count: Count;
  vendor: AcceleratorVendor;
  backend: Backend | null;
}
/**
 * The model an LLM-serving task serves.
 */
export interface ExampleModel {
  name: Name6;
  maker: ModelMaker;
}
/**
 * The workload a data-structure task drives.
 */
export interface ExampleWorkload {
  name: Name7;
  description: Description;
}
/**
 * The project made from the example.
 */
export interface ProjectCreateFromExampleReply {
  request_id: RequestId59;
  type: Type59;
  created: CreatedProject;
}
/**
 * A project made in its own new Git repository, and its tasks.
 */
export interface CreatedProject {
  project: Project11;
  tasks: Tasks2;
}
/**
 * The project copied out into its own repository.
 */
export interface ProjectCopyOutReply {
  request_id: RequestId60;
  type: Type60;
  created: CreatedProject;
}
/**
 * The machine's compute settings.
 */
export interface ComputeListReply {
  request_id: RequestId61;
  type: Type61;
  settings: ComputeSettings;
}
/**
 * What the machine offers as places to measure: its clusters and its Modal token.
 */
export interface ComputeSettings {
  config_path: ConfigPath;
  clusters: Clusters;
  modal: ModalStatus;
}
/**
 * A cluster set up on the machine, and the file it comes from.
 */
export interface ListedCluster {
  cluster: SlurmClusterSettings;
  source: ClusterSource;
}
/**
 * Whether Modal is set up on the machine; the token itself is never sent.
 */
export interface ModalStatus {
  token_present: TokenPresent;
  source?: ModalTokenSource | null;
}
/**
 * What testing the cluster found.
 */
export interface ComputeTestClusterReply {
  request_id: RequestId62;
  type: Type62;
  result: Result;
}
/**
 * The cluster answered: what its scheduler lists, and the hardware read from it.
 */
export interface ClusterTestPassed {
  outcome: Outcome1;
  hardware: ClusterHardware;
  partitions: Partitions;
}
/**
 * The cluster did not pass; ``detail`` is one line saying how.
 */
export interface ClusterTestFailed {
  outcome: Outcome2;
  stage: ClusterTestStage;
  detail: Detail1;
}
/**
 * The settings after the cluster was saved.
 */
export interface ComputeAddClusterReply {
  request_id: RequestId63;
  type: Type63;
  settings: ComputeSettings;
}
/**
 * The settings after the cluster was removed.
 */
export interface ComputeRemoveClusterReply {
  request_id: RequestId64;
  type: Type64;
  settings: ComputeSettings;
}
/**
 * The logins, in the order ``harnesses.list`` offers the providers.
 */
export interface AgentLoginsListReply {
  request_id: RequestId65;
  type: Type65;
  logins: Logins;
}
/**
 * One harness's login on the machine.
 */
export interface AgentLogin {
  provider: Provider4;
  provider_name: ProviderName1;
  installed: Installed;
  state: LoginState;
  how?: How;
  api_key_variables?: ApiKeyVariables;
  detail?: Detail2;
  can_start_login: CanStartLogin;
}
/**
 * Signed in with a person's account (a subscription login).
 */
export interface AccountLogin {
  kind: Kind30;
}
/**
 * Signed in with an API key; where it is held is named, the key never.
 */
export interface ApiKeyLogin {
  kind: Kind31;
  variable?: Variable;
  file?: File;
}
/**
 * What starting the login did.
 */
export interface AgentLoginsStartReply {
  request_id: RequestId66;
  type: Type66;
  result: Result1;
}
/**
 * The harness's own login was started on the machine.
 */
export interface LoginStarted {
  outcome: Outcome3;
  instruction?: Instruction;
}
/**
 * The login was not started, and why.
 */
export interface LoginUnavailable {
  outcome: Outcome4;
  reason: LoginUnavailableReason;
  command?: Command1;
  detail?: Detail3;
}
/**
 * The task's goal and objectives.
 */
export interface ProjectTaskGoalReply {
  request_id: RequestId67;
  type: Type67;
  goal: TaskGoal;
}
/**
 * What a task is for: its goal text and the axes it declares, read from its files.
 */
export interface TaskGoal {
  task: Task12;
  objectives: Objectives1;
  goal: Goal1;
  goal_truncated: GoalTruncated1;
}
/**
 * What the destination holds.
 */
export interface ExamplesDestinationReply {
  request_id: RequestId68;
  type: Type68;
  state: ExampleDestination;
  project: Project12;
}
/**
 * What the Usage page shows for one window.
 *
 * Costs are the sums of the turns' reported ``cost_usd`` (a turn that reported none counts in
 * ``unpriced_turns`` and adds nothing, so a total is a lower bound while it is not zero). A turn
 * with no recorded end time cannot be placed in a window: it counts in ``untimed_turns`` and
 * nowhere else. ``new_bests`` counts the ideas kept (``IdeaVerdict.KEPT``) whose result landed in
 * the window, and ``cost_per_new_best_usd`` is ``spent_usd`` over it, ``None`` when there is none.
 * A run's kept ideas beyond ``DASHBOARD_MAX_POINTS`` are not counted.
 */
export interface UsageWindowReply {
  request_id: RequestId69;
  type: Type69;
  since: Since1;
  until: Until1;
  spent_usd: SpentUsd;
  spend_per_hour_usd: SpendPerHourUsd;
  tokens: TokenTally;
  turns: Turns;
  unpriced_turns: UnpricedTurns;
  untimed_turns: UntimedTurns;
  new_bests: NewBests;
  cost_per_new_best_usd: CostPerNewBestUsd;
  by_run: ByRun;
  by_agent: ByAgent;
  agents_omitted: AgentsOmitted;
  missing_runs: MissingRuns;
}
/**
 * What one run spent in the window; only runs that had a turn end in it are listed.
 */
export interface RunSpend {
  run_id: RunId10;
  task: Task13;
  cost_usd: CostUsd1;
}
/**
 * What one kind of agent spent in the window: a role on a harness and model.
 *
 * A part the journal did not record for a turn is ``None``, so the turns of an execution whose
 * start is missing form their own entry.
 */
export interface AgentSpend {
  role: Role3;
  harness: Harness1;
  model: Model6;
  cost_usd: CostUsd2;
  turns: Turns1;
}
/**
 * A push on a watch: the listing as it is now, sent first and then on each change.
 */
export interface InstancesChanged {
  type: Type70;
  watch_id: WatchId3;
  listing: InstanceList;
}
/**
 * A failed request, or a line that was not one (``request_id`` is ``None`` then).
 */
export interface HelperError {
  type: Type71;
  request_id: RequestId70;
  code: HelperErrorCode;
  message: Message4;
  path?: Path2;
  supported_versions?: SupportedVersions;
  vibesys_version?: VibesysVersion3;
}
/**
 * The one stderr line a relayed run channel writes when it ends with a nonzero status.
 */
export interface ChannelEnd {
  outcome: ChannelEndOutcome;
  exit_status: ExitStatus;
  detail: Detail4;
}
/**
 * The helper could not start the worker that owns the compensating stop.
 */
export interface WorkerStartRefusedCompensationFailure {
  type: Type72;
  exit_status: ExitStatus1;
  request_id: RequestId71;
  detail: Detail5;
  kind: Kind32;
  instance_id: InstanceId6;
}
/**
 * The registry stop raised before returning typed evidence.
 */
export interface StopFailedCompensationFailure {
  type: Type73;
  exit_status: ExitStatus2;
  request_id: RequestId72;
  detail: Detail6;
  kind: Kind33;
  instance_id: InstanceId7;
}
/**
 * The registry returned a typed result that does not prove terminal cleanup.
 *
 * The stop result is the one source of the instance identity in this variant,
 * so a serialized report cannot carry two identities that disagree.
 */
export interface StopUnconfirmedCompensationFailure {
  type: Type74;
  exit_status: ExitStatus3;
  request_id: RequestId73;
  detail: Detail7;
  kind: Kind34;
  stop_result: UnconfirmedInstanceStopResult;
}
/**
 * A complete stop result whose outcome does not prove terminal cleanup.
 */
export interface UnconfirmedInstanceStopResult {
  version?: Version13;
  id: Id7;
  outcome: Outcome5;
  route?: StopRoute | null;
}
