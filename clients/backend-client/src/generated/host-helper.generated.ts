/* Generated from the Python host helper models (server.host_api). Do not edit. */

export type Request =
  | HelloRequest
  | InstancesListRequest
  | InstancesWatchRequest
  | InstancesUnwatchRequest
  | TasksListRequest
  | RunsListRequest
  | RunStartRequest
  | RunStopRequest
  | RunConnectRequest
  | PingRequest
  | PathsCompleteRequest;
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
export type Type2 = "instances.watch";
export type RequestId3 = string;
export type DeadlineMs3 = number;
export type Type3 = "instances.unwatch";
export type WatchId = string;
export type RequestId4 = string;
export type DeadlineMs4 = number;
export type Type4 = "tasks.list";
export type Project = string;
export type RequestId5 = string;
export type DeadlineMs5 = number;
export type Type5 = "runs.list";
export type Project1 = string;
export type RequestId6 = string;
export type DeadlineMs6 = number;
export type Type6 = "run.start";
export type Project2 = string;
export type Task = string | null;
export type Args = string[];
export type Resume = ResumeRun | ResumeLatest | null;
export type RunId = string;
export type Latest = true;
export type RequestId7 = string;
export type DeadlineMs7 = number;
export type Type7 = "run.stop";
export type InstanceId = string;
export type Force = boolean;
export type RequestId8 = string;
export type DeadlineMs8 = number;
export type Type8 = "run.connect";
export type InstanceId1 = string;
/**
 * Which run-protocol connection a run channel carries (``WP-ROLES``).
 */
export type RunRole = "control" | "subscribe" | "chat";
export type RequestId9 = string;
export type DeadlineMs9 = number;
export type Type9 = "ping";
export type RequestId10 = string;
export type DeadlineMs10 = number;
export type Type10 = "paths.complete";
export type Prefix = string;
export type Message =
  | HelloReply
  | InstancesListReply
  | InstancesWatchReply
  | InstancesUnwatchReply
  | TasksListReply
  | RunsListReply
  | RunStartReply
  | RunStopReply
  | RunConnectReply
  | PingReply
  | PathsCompleteReply
  | InstancesChanged
  | HelperError;
export type RequestId11 = string;
export type Type11 = "hello";
export type ProtocolVersion = number;
/**
 * Optional capabilities negotiated by ``hello``; a closed set per protocol version.
 *
 * A client calls a gated method only after ``hello`` returned its feature, so
 * a strict peer never sees a method it does not know.
 */
export type HelperFeature = "instances_watch" | "paths_complete";
export type Features1 = HelperFeature[];
export type VibesysVersion = string;
export type Root = string;
export type Commit = string | null;
export type Dirty = boolean | null;
export type RequestId12 = string;
export type Type12 = "instances.list";
export type Version = 3;
export type Version1 = 3;
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
 */
export type StartupStage = "resolving";
export type Code = string;
export type Stage = string;
export type Message1 = string;
export type SocketPath = string;
export type LogPath = string;
export type ProjectRoot = string;
export type RunId1 = string;
export type Pid = number;
export type StartedAt = number;
export type EndedAt = number | null;
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
export type RequestId13 = string;
export type Type13 = "instances.watch";
export type WatchId1 = string;
export type RequestId14 = string;
export type Type14 = "instances.unwatch";
export type WatchId2 = string;
export type RequestId15 = string;
export type Type15 = "tasks.list";
export type Version3 = 1;
export type ProjectRoot1 = string;
export type Name = string;
export type Tasks = TaskSummary[];
export type RequestId16 = string;
export type Type16 = "runs.list";
export type ProjectRoot2 = string;
export type RunId2 = string;
export type Task1 = string | null;
export type DisplayName = string;
export type CreatedAt = string;
export type InstanceId2 = string | null;
export type Runs = RunSummary[];
export type RequestId17 = string;
export type Type17 = "run.start";
export type Launch = LiveInstanceRecord | DetachedLaunchFailure;
export type Version4 = 1;
export type Outcome = "failed";
export type Code1 = string;
export type Stage1 = string;
export type Message2 = string;
export type ExitCode = number;
export type LogPath1 = string | null;
export type RequestId18 = string;
export type Type18 = "run.stop";
export type Version5 = 1;
export type Id2 = string;
/**
 * What ``vibesys instances stop`` observed.
 */
export type StopOutcome = "stopped" | "stopping" | "not_running" | "still_running" | "unsupported";
/**
 * How ``vibesys instances stop`` reached the server.
 */
export type StopRoute = "control_socket" | "signal";
export type RequestId19 = string;
export type Type19 = "run.connect";
export type RunId3 = string;
export type ProtocolVersion2 = number;
export type RequestId20 = string;
export type Type20 = "ping";
export type RequestId21 = string;
export type Type21 = "paths.complete";
export type Directory = string;
export type Name1 = string;
export type Path = string;
export type VibesysCheckout = boolean;
export type VibesysProject = boolean;
export type GitRepository = boolean;
export type Entries = PathEntry[];
export type Truncated = boolean;
export type Type22 = "instances.changed";
export type WatchId3 = string;
export type Type23 = "error";
export type RequestId22 = string | null;
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
  | "internal";
export type Message3 = string;
export type Path1 = string | null;
export type SupportedVersions = [unknown, unknown] | null;
export type VibesysVersion2 = string | null;

/**
 * Root schema document for the host helper: what a client sends and what it receives.
 */
export interface HostHelperDocument {
  request: Request;
  message: Message;
  [k: string]: unknown;
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
 * Push the listing now and again whenever it changes, until ``instances.unwatch``.
 */
export interface InstancesWatchRequest {
  request_id: RequestId2;
  deadline_ms: DeadlineMs2;
  type: Type2;
}
/**
 * Stop the watch that ``watch_id`` (its request's id) started.
 */
export interface InstancesUnwatchRequest {
  request_id: RequestId3;
  deadline_ms: DeadlineMs3;
  type: Type3;
  watch_id: WatchId;
}
/**
 * The tasks a project defines under ``.vibesys/tasks/``.
 */
export interface TasksListRequest {
  request_id: RequestId4;
  deadline_ms: DeadlineMs4;
  type: Type4;
  project: Project;
}
/**
 * A project's runs, with the phase of any server on this host that drives one.
 */
export interface RunsListRequest {
  request_id: RequestId5;
  deadline_ms: DeadlineMs5;
  type: Type5;
  project: Project1;
}
/**
 * Start a detached run (``vibesys --detach``) in ``project``.
 */
export interface RunStartRequest {
  request_id: RequestId6;
  deadline_ms: DeadlineMs6;
  type: Type6;
  project: Project2;
  task?: Task;
  args?: Args;
  resume?: Resume;
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
 * Stop a live detached server (``vibesys instances stop``).
 */
export interface RunStopRequest {
  request_id: RequestId7;
  deadline_ms: DeadlineMs7;
  type: Type7;
  instance_id: InstanceId;
  force?: Force;
}
/**
 * Make this channel a byte relay to a live run's control socket.
 */
export interface RunConnectRequest {
  request_id: RequestId8;
  deadline_ms: DeadlineMs8;
  type: Type8;
  instance_id: InstanceId1;
  role: RunRole;
}
/**
 * Liveness of the channel.
 */
export interface PingRequest {
  request_id: RequestId9;
  deadline_ms: DeadlineMs9;
  type: Type9;
}
/**
 * The directories that complete a typed path on the host.
 */
export interface PathsCompleteRequest {
  request_id: RequestId10;
  deadline_ms: DeadlineMs10;
  type: Type10;
  prefix: Prefix;
}
/**
 * The negotiated version and features, and what will start runs on this host.
 */
export interface HelloReply {
  request_id: RequestId11;
  type: Type11;
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
  request_id: RequestId12;
  type: Type12;
  listing: InstanceList;
}
/**
 * ``vibesys instances list --json``: the detached servers on this node (version 3).
 */
export interface InstanceList {
  version?: Version;
  instances: Instances;
  ended?: Ended;
  unreadable?: Unreadable;
  unverified?: Unverified;
}
/**
 * What a detached server publishes about itself (version 3).
 *
 * ``run_id`` is allocated by the launcher before the server starts, so the
 * record names its run from the first time it is published. ``phase`` is the
 * run's lifecycle as the server folds it from its journal. When the server
 * exits it leaves its last record, with an ended phase and ``ended_at``, as a
 * tombstone the registry keeps for a day.
 */
export interface LiveInstanceRecord {
  version?: Version1;
  id: Id;
  status: InstanceStatus;
  phase: RunPhase;
  socket_path: SocketPath;
  log_path: LogPath;
  project_root: ProjectRoot;
  run_id: RunId1;
  pid: Pid;
  started_at: StartedAt;
  ended_at?: EndedAt;
  hostname: Hostname;
  protocol_version?: ProtocolVersion1;
  vibesys_version: VibesysVersion1;
  vibesys_root?: VibesysRoot;
}
/**
 * A run's lifecycle status with the detail its status needs.
 *
 * ``stage`` says where a ``starting`` run is; ``reason`` says why a
 * ``failed`` one ended. Neither is present for any other status.
 */
export interface RunPhase {
  status: RunStatus;
  stage?: StartupStage | null;
  reason?: FailureReason | null;
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
 * A registry entry this reader cannot interpret; ``version`` is ``None`` if it has none.
 */
export interface UnreadableRecord {
  id: Id1;
  version: Version2;
}
/**
 * The watch started; ``instances.changed`` for ``watch_id`` follows at once and on change.
 */
export interface InstancesWatchReply {
  request_id: RequestId13;
  type: Type13;
  watch_id: WatchId1;
}
/**
 * The watch ended; no ``instances.changed`` for ``watch_id`` follows.
 */
export interface InstancesUnwatchReply {
  request_id: RequestId14;
  type: Type14;
  watch_id: WatchId2;
}
/**
 * The project's tasks.
 */
export interface TasksListReply {
  request_id: RequestId15;
  type: Type15;
  tasks: TaskList;
}
/**
 * ``vibesys tasks --json``: the tasks of one project.
 */
export interface TaskList {
  version?: Version3;
  project_root: ProjectRoot1;
  tasks: Tasks;
}
/**
 * One task of a project.
 */
export interface TaskSummary {
  name: Name;
}
/**
 * The project's runs.
 */
export interface RunsListReply {
  request_id: RequestId16;
  type: Type16;
  runs: RunList;
}
/**
 * A project's runs, oldest first.
 */
export interface RunList {
  project_root: ProjectRoot2;
  runs: Runs;
}
/**
 * One run of a project, from its durable manifest and this host's registry.
 */
export interface RunSummary {
  run_id: RunId2;
  task: Task1;
  display_name: DisplayName;
  created_at: CreatedAt;
  phase: RunPhase | null;
  instance_id: InstanceId2;
}
/**
 * The started server's record, or why nothing started.
 */
export interface RunStartReply {
  request_id: RequestId17;
  type: Type17;
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
  version?: Version4;
  outcome?: Outcome;
  code: Code1;
  stage: Stage1;
  message: Message2;
  exit_code: ExitCode;
  log_path?: LogPath1;
  live_instance?: LiveInstanceRecord | null;
}
/**
 * What the stop observed.
 */
export interface RunStopReply {
  request_id: RequestId18;
  type: Type18;
  result: InstanceStopResult;
}
/**
 * ``vibesys instances stop --json``.
 */
export interface InstanceStopResult {
  version?: Version5;
  id: Id2;
  outcome: StopOutcome;
  route?: StopRoute | null;
}
/**
 * The channel is now a byte relay carrying the run protocol to this run.
 */
export interface RunConnectReply {
  request_id: RequestId19;
  type: Type19;
  run_id: RunId3;
  protocol_version: ProtocolVersion2;
}
/**
 * The channel is alive.
 */
export interface PingReply {
  request_id: RequestId20;
  type: Type20;
}
/**
 * Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``.
 */
export interface PathsCompleteReply {
  request_id: RequestId21;
  type: Type21;
  directory: Directory;
  entries: Entries;
  truncated: Truncated;
}
/**
 * One directory that completes the typed path, with what it looks like.
 */
export interface PathEntry {
  name: Name1;
  path: Path;
  vibesys_checkout: VibesysCheckout;
  vibesys_project: VibesysProject;
  git_repository: GitRepository;
}
/**
 * A push on a watch: the listing as it is now, sent first and then on each change.
 */
export interface InstancesChanged {
  type: Type22;
  watch_id: WatchId3;
  listing: InstanceList;
}
/**
 * A failed request, or a line that was not one (``request_id`` is ``None`` then).
 */
export interface HelperError {
  type: Type23;
  request_id: RequestId22;
  code: HelperErrorCode;
  message: Message3;
  path?: Path1;
  supported_versions?: SupportedVersions;
  vibesys_version?: VibesysVersion2;
}
