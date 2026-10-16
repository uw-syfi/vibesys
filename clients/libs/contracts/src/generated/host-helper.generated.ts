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
  | MachineDescribeRequest
  | CheckoutInspectRequest;
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
export type RequestId16 = string;
export type DeadlineMs16 = number;
export type Type16 = "runs.read_events";
export type Project3 = string;
export type RunId1 = string;
export type FromSequence = number;
export type Max = number;
export type RequestId17 = string;
export type DeadlineMs17 = number;
export type Type17 = "machine.describe";
export type RequestId18 = string;
export type DeadlineMs18 = number;
export type Type18 = "checkout.inspect";
export type Path = string;
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
  | MachineDescribeReply
  | CheckoutInspectReply
  | InstancesChanged
  | HelperError;
export type RequestId19 = string;
export type Type19 = "hello";
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
  | "machine_describe"
  | "checkout_inspect";
export type Features1 = HelperFeature[];
export type VibesysVersion = string;
export type Root = string;
export type Commit = string | null;
export type Dirty = boolean | null;
export type RequestId20 = string;
export type Type20 = "instances.list";
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
export type RunId2 = string;
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
export type RequestId21 = string;
export type Type21 = "instances.inspect";
export type Version3 = 1;
export type Id2 = string;
/**
 * What one coherent registry observation proved about an instance.
 */
export type InspectionState = "live" | "ended" | "unverified";
export type Version4 = 1;
export type Id3 = string;
export type ProjectRoot1 = string;
export type RunId3 = string;
export type Task1 = string | null;
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
export type RequestId22 = string;
export type Type22 = "instances.watch";
export type WatchId1 = string;
export type RequestId23 = string;
export type Type23 = "instances.unwatch";
export type WatchId2 = string;
export type RequestId24 = string;
export type Type24 = "tasks.list";
export type Version5 = 1;
export type ProjectRoot2 = string;
export type Name = string;
export type Tasks = TaskSummary[];
export type RequestId25 = string;
export type Type25 = "runs.list";
export type ProjectRoot3 = string;
export type RunId4 = string;
export type Task2 = string | null;
export type DisplayName = string;
export type CreatedAt = string;
export type InstanceId5 = string | null;
export type Runs = RunSummary[];
export type RequestId26 = string;
export type Type26 = "run.start";
export type Launch = LiveInstanceRecord | DetachedLaunchFailure;
export type Version6 = 1;
export type Outcome = "failed";
export type Code1 = string;
export type Stage1 = string;
export type Message2 = string;
export type ExitCode = number;
export type LogPath1 = string | null;
export type RequestId27 = string;
export type Type27 = "run.stop";
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
export type RequestId28 = string;
export type Type28 = "run.connect";
export type RunId5 = string;
export type ProtocolVersion3 = number;
export type RequestId29 = string;
export type Type29 = "ping";
export type RequestId30 = string;
export type Type30 = "paths.complete";
export type Directory = string;
export type Name1 = string;
export type Path1 = string;
export type VibesysCheckout = boolean;
export type VibesysProject = boolean;
export type GitRepository = boolean;
export type Entries = PathEntry[];
export type Truncated1 = boolean;
export type RequestId31 = string;
export type Type31 = "logs.list";
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
export type RequestId32 = string;
export type Type32 = "logs.read";
export type Offset1 = number;
export type Data = string;
export type NextOffset = number;
export type Eof = boolean;
export type Closed = boolean;
export type TruncatedBefore = boolean;
export type Offset2 = number;
export type Length = number;
/**
 * Which of a command's output pipes wrote some bytes.
 */
export type LogPipe = "stdout" | "stderr";
export type Pipes = LogPipeSpan[];
export type RequestId33 = string;
export type Type33 = "logs.cancel";
export type ReadId1 = string;
export type RequestId34 = string;
export type Type34 = "harnesses.list";
export type Provider1 = string;
export type Model1 = string;
export type Default = boolean;
export type Harnesses = HarnessOption[];
export type RequestId35 = string;
export type Type35 = "runs.read_events";
export type Events = {
  [k: string]: JsonValue;
}[];
export type NextSequence = number;
export type Complete = boolean;
export type RequestId36 = string;
export type Type36 = "machine.describe";
export type Root1 = string;
export type Version8 = string;
export type Commit1 = string | null;
export type Dirty1 = boolean | null;
export type CpuCores = number;
export type Vendor = string;
export type Model2 = string;
export type Gpus = Gpu[];
/**
 * How a machine's GPUs run work; a closed set, ``cpu`` when it has none.
 */
export type Accelerator = "cpu" | "cuda" | "rocm" | "metal";
export type Docker = DockerReachable | DockerUnreachable;
export type Reachable = true;
export type Version9 = string;
export type Reachable1 = false;
export type Reason1 = string;
export type HarnessClis = string[];
export type Live = number;
export type EndedToday = number;
export type RequestId37 = string;
export type Type37 = "checkout.inspect";
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
export type Type38 = "instances.changed";
export type WatchId3 = string;
export type Type39 = "error";
export type RequestId38 = string | null;
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
  | "unknown_run"
  | "unvetted_harness"
  | "internal";
export type Message3 = string;
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
export type Detail = string;
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
export type Type40 = "run.start_compensation_failed";
export type ExitStatus1 = 12;
export type RequestId39 = string;
export type Detail1 = string;
export type Kind = "worker_start_refused";
export type InstanceId6 = string;
export type Type41 = "run.start_compensation_failed";
export type ExitStatus2 = 12;
export type RequestId40 = string;
export type Detail2 = string;
export type Kind1 = "stop_failed";
export type InstanceId7 = string;
export type Type42 = "run.start_compensation_failed";
export type ExitStatus3 = 12;
export type RequestId41 = string;
export type Detail3 = string;
export type Kind2 = "stop_unconfirmed";
export type Version12 = 1;
export type Id6 = string;
export type Outcome1 = "stopping" | "still_running" | "unsupported";

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
}
/**
 * What this host offers runs: its checkout, hardware, Docker, harness CLIs and runs.
 */
export interface MachineDescribeRequest {
  request_id: RequestId17;
  deadline_ms: DeadlineMs17;
  type: Type17;
}
/**
 * Whether ``path`` holds a VibeSys checkout, and whether this app can use it.
 */
export interface CheckoutInspectRequest {
  request_id: RequestId18;
  deadline_ms: DeadlineMs18;
  type: Type18;
  path: Path;
}
/**
 * The negotiated version and features, and what will start runs on this host.
 */
export interface HelloReply {
  request_id: RequestId19;
  type: Type19;
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
  request_id: RequestId20;
  type: Type20;
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
  run_id: RunId2;
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
  request_id: RequestId21;
  type: Type21;
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
  run_id: RunId3;
  task?: Task1;
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
  request_id: RequestId22;
  type: Type22;
  watch_id: WatchId1;
}
/**
 * The watch ended; no ``instances.changed`` for ``watch_id`` follows.
 */
export interface InstancesUnwatchReply {
  request_id: RequestId23;
  type: Type23;
  watch_id: WatchId2;
}
/**
 * The project's tasks.
 */
export interface TasksListReply {
  request_id: RequestId24;
  type: Type24;
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
  name: Name;
}
/**
 * The project's runs.
 */
export interface RunsListReply {
  request_id: RequestId25;
  type: Type25;
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
  run_id: RunId4;
  task: Task2;
  display_name: DisplayName;
  created_at: CreatedAt;
  phase: RunPhase | null;
  instance_id: InstanceId5;
}
/**
 * The started server's record, or why nothing started.
 */
export interface RunStartReply {
  request_id: RequestId26;
  type: Type26;
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
  message: Message2;
  exit_code: ExitCode;
  log_path?: LogPath1;
  live_instance?: LiveInstanceRecord | null;
}
/**
 * What the stop observed.
 */
export interface RunStopReply {
  request_id: RequestId27;
  type: Type27;
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
  request_id: RequestId28;
  type: Type28;
  run_id: RunId5;
  protocol_version: ProtocolVersion3;
}
/**
 * The channel is alive.
 */
export interface PingReply {
  request_id: RequestId29;
  type: Type29;
}
/**
 * Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``.
 */
export interface PathsCompleteReply {
  request_id: RequestId30;
  type: Type30;
  directory: Directory;
  entries: Entries;
  truncated: Truncated1;
}
/**
 * One directory that completes the typed path, with what it looks like.
 */
export interface PathEntry {
  name: Name1;
  path: Path1;
  vibesys_checkout: VibesysCheckout;
  vibesys_project: VibesysProject;
  git_repository: GitRepository;
}
/**
 * The instance's streams, oldest first.
 */
export interface LogsListReply {
  request_id: RequestId31;
  type: Type31;
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
  request_id: RequestId32;
  type: Type32;
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
  offset: Offset1;
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
  offset: Offset2;
  length: Length;
  pipe: LogPipe;
}
/**
 * The withdrawal was taken: a read ``read_id`` named that was still waiting stops waiting.
 */
export interface LogsCancelReply {
  request_id: RequestId33;
  type: Type33;
  read_id: ReadId1;
}
/**
 * The vetted pairs, grouped by provider with each provider's default first.
 */
export interface HarnessesListReply {
  request_id: RequestId34;
  type: Type34;
  harnesses: Harnesses;
}
/**
 * A vetted provider and model pair a run may start with.
 */
export interface HarnessOption {
  provider: Provider1;
  model: Model1;
  default: Default;
}
/**
 * One page of a run's recorded events, oldest first.
 */
export interface RunsReadEventsReply {
  request_id: RequestId35;
  type: Type35;
  events: Events;
  next_sequence: NextSequence;
  complete: Complete;
}
/**
 * The machine's description.
 */
export interface MachineDescribeReply {
  request_id: RequestId36;
  type: Type36;
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
  model: Model2;
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
  request_id: RequestId37;
  type: Type37;
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
 * A push on a watch: the listing as it is now, sent first and then on each change.
 */
export interface InstancesChanged {
  type: Type38;
  watch_id: WatchId3;
  listing: InstanceList;
}
/**
 * A failed request, or a line that was not one (``request_id`` is ``None`` then).
 */
export interface HelperError {
  type: Type39;
  request_id: RequestId38;
  code: HelperErrorCode;
  message: Message3;
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
  detail: Detail;
}
/**
 * The helper could not start the worker that owns the compensating stop.
 */
export interface WorkerStartRefusedCompensationFailure {
  type: Type40;
  exit_status: ExitStatus1;
  request_id: RequestId39;
  detail: Detail1;
  kind: Kind;
  instance_id: InstanceId6;
}
/**
 * The registry stop raised before returning typed evidence.
 */
export interface StopFailedCompensationFailure {
  type: Type41;
  exit_status: ExitStatus2;
  request_id: RequestId40;
  detail: Detail2;
  kind: Kind1;
  instance_id: InstanceId7;
}
/**
 * The registry returned a typed result that does not prove terminal cleanup.
 *
 * The stop result is the one source of the instance identity in this variant,
 * so a serialized report cannot carry two identities that disagree.
 */
export interface StopUnconfirmedCompensationFailure {
  type: Type42;
  exit_status: ExitStatus3;
  request_id: RequestId41;
  detail: Detail3;
  kind: Kind2;
  stop_result: UnconfirmedInstanceStopResult;
}
/**
 * A complete stop result whose outcome does not prove terminal cleanup.
 */
export interface UnconfirmedInstanceStopResult {
  version?: Version12;
  id: Id6;
  outcome: Outcome1;
  route?: StopRoute | null;
}
