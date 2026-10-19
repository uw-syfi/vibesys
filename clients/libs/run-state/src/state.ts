/**
 * The run fold's state: `CoreState` and the read-only types it publishes, its initial value, and
 * the clone and publish helpers every step of the fold goes through.
 */
import type {Diagnostic, RunEvent, RunSnapshot, RunStatus} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import type {BenchmarkRecord} from './benchmark.js';
import type {ExecutionStatus} from './execution-status.js';
import {
  type PersistentArray,
  persistentArrayFrom,
  rehydratePersistentArray,
} from './persistent-array.js';
import {publishProjectionFields, type ReadonlyProjection} from './publication.js';
import type {RoundKey} from './round-key.js';
import {
  type AgentPhase,
  initialRunMapProvenance,
  installRunMapArrays,
  type RoundState,
  type RunMapProvenance,
  rehydrateRunMapProvenance,
} from './run-map.js';
import {EMPTY_LEDGER, type TokenLedger} from './token-usage.js';
import type {TranscriptEntry} from './transcript-fold.js';
import type {ExecutionStarts} from './usage-records.js';
import type {WorkstreamMap} from './workstream.js';

export type AgentExecutionMode = 'thinking' | 'responding' | 'tool' | 'waiting';

export interface ActiveAgentExecution {
  readonly executionId: string;
  readonly agentKind: string;
  readonly roundLabel: string | null;
  readonly roundNumber: number | null;
  readonly roundKey: RoundKey | null;
  readonly stage: string;
  readonly attempt: number | null;
  readonly assignment: string;
  readonly startedAt: string;
  readonly activity: Readonly<{mode: AgentExecutionMode; summary: string; tool?: string | null}>;
  readonly provider?: string | null;
  readonly model?: string | null;
}

export interface TodoItem {
  readonly content: string;
  readonly status: string;
}

export interface ExecutionTodos {
  readonly executionId?: string | null;
  readonly agentKind: string | null;
  readonly roundNumber: number | null;
  readonly roundKey: RoundKey | null;
  readonly items: readonly TodoItem[];
}

export interface UsageMeter {
  readonly inputTokens: number;
  readonly contextWindow: number | null;
  readonly model: string | null;
}

/**
 * The provider capacity stop a run is parked on.
 *
 * `resetsAt` is when the provider said capacity returns and `resumesAt` when the
 * run's quota policy resumes it by itself, both epoch seconds or `null`.
 * `fallback` is the provider and model an operator may resume with, `null` when
 * the run has none configured. `waiting` is the operator's request to resume when capacity
 * returns (`quota_waiting`), with when the run expects to (`resumesAt`, epoch seconds, `null` when
 * the provider gave no reset time); `null` when no wait is under way (none asked, or
 * `quota_wait_cancelled`).
 */
export interface QuotaPause {
  readonly provider: string;
  /** Never `unavailable`: an outage holds only its own turn and never pauses the run. */
  readonly condition: 'quota_exhausted' | 'rate_limited';
  readonly detail: string;
  readonly resetsAt: number | null;
  readonly resumesAt: number | null;
  readonly policy: string | null;
  readonly fallback: {readonly provider: string; readonly model: string | null} | null;
  readonly waiting: {readonly resumesAt: number | null} | null;
}

/**
 * One provider replaced by its fallback, folded from `provider_switched`.
 *
 * `sequence` is that event's own folded sequence, so a consumer can tell this is still the fact
 * that settled the current `pause` (`sequence === QuotaState.sequence`) rather than a switch an
 * intervening pause/resume/abandon has since superseded.
 */
export interface ProviderSwitch {
  readonly sequence: number;
  readonly fromProvider: string;
  readonly toProvider: string;
  readonly toModel: string;
  readonly reason: 'policy' | 'operator';
  readonly detail: string;
}

/**
 * The latest capacity stop fact. `sequence` is the newest quota event folded,
 * `0` before any; `pause` is `null` once that event settled the stop (resumed,
 * abandoned or switched). Whether the run is still parked is the run status's
 * to say, so a consumer shows `pause` only while the status is pausing or paused.
 *
 * `lastSwitch` is sticky: a provider switch is a fact about the run's history, not a transient
 * stop, so it survives later pauses and resumes once set. `null` before the run's first switch.
 */
export interface QuotaState {
  readonly sequence: number;
  readonly pause: QuotaPause | null;
  readonly lastSwitch: ProviderSwitch | null;
}

/**
 * The run's chosen result, folded from `winner_selected`. `selection` says whether a candidate
 * beat the trusted input or it was kept unchanged; `settlementId` names the settled attempt that
 * produced a candidate, `null` for the baseline. `null` before that once-per-run event.
 */
export interface WinnerSelection {
  readonly selection: 'candidate' | 'baseline';
  readonly revision: string;
  readonly settlementId: string | null;
}

/**
 * How a candidate's measurement compared with its base's, folded from `measurement_compared`:
 * `better`, `within_noise` or `worse` once the noise is allowed for, `inconclusive` before it can be. This is the framework's own
 * reading of whether an idea's effect held, whatever its ratio to the input says.
 */
export interface MeasurementComparison {
  readonly verdict: 'better' | 'within_noise' | 'worse' | 'inconclusive';
  /** The candidate's value over its base's. */
  readonly ratio: number;
  /** The metric compared: the name of the headline reading it states the value of. */
  readonly metric: string;
  /** The candidate's value as the comparison used it: the mean of its `candidateSamples` runs. */
  readonly candidateValue: number;
  /** The base's value as the comparison used it: the mean of its `baseSamples` runs. */
  readonly baseValue: number;
  /** The commit the candidate was compared with: the input's, or another idea's. */
  readonly baseCommit: string;
  readonly candidateSamples: number;
  readonly baseSamples: number;
}

/**
 * One stop of an agent turn on a provider capacity limit, folded from the `quota_resumed` or
 * `quota_abandoned` event that ended it. The turn did no work for `waitedSeconds`, which ended at
 * the event's `endedAt` (the host's wall time, as every event stamps it).
 */
export interface QuotaStop {
  readonly sequence: number;
  readonly provider: string;
  /** The limit it stopped on; null in events recorded before the field existed. */
  readonly condition: 'quota_exhausted' | 'rate_limited' | 'unavailable' | null;
  readonly outcome: 'resumed' | 'abandoned';
  readonly waitedSeconds: number;
  /** The event's timestamp: when the stop ended. */
  readonly endedAt: string;
}

type RunStartupProgressData = Extract<
  NonNullable<RunEvent['data']>,
  {kind: 'run_startup_progress'}
>;

/**
 * The latest startup stage and progress fact, while the run's environment opens.
 *
 * `null` before the first `run_startup_progress` event. The backend stops
 * reporting once the run is ready, so this is never cleared once set; a consumer shows it only while `status` is
 * `starting`, the same discipline `QuotaState` documents for `pause`.
 */
export interface RunStartupState {
  readonly stage: RunStartupProgressData['stage'];
  /**
   * What the stage is doing: `running` as it begins and at each step, `waiting` while blocked on
   * something outside the run host (`condition`), `finished` once it has ended.
   */
  readonly state: NonNullable<RunStartupProgressData['state']>;
  /** What a `waiting` stage waits on; `null` in any other state. */
  readonly condition: NonNullable<RunStartupProgressData['condition']> | null;
  /** Seconds waited so far (`waiting`) or the stage took (`finished`); `null` while `running`. */
  readonly elapsedSeconds: number | null;
  /** `null` while the stage reports no progress of its own (resolving, say). */
  readonly progress: Readonly<{done: number; total: number | null; label: string}> | null;
  /**
   * The envelope timestamp of the first startup event of the current stage; it resets when the
   * stage changes. A consumer subtracts it from its own clock to say how long the stage has run,
   * so the fold stays free of wall-clock reads.
   */
  readonly stageSince: string;
  /** The envelope timestamp of the latest startup event: the last time the backend was heard. */
  readonly lastActivityAt: string;
}

/** Thread id used for chat events recorded without one. */
export const DEFAULT_CHAT_THREAD_ID = 'default';

/**
 * One experiment-chat thread, replayed from `chat_thread_created` events. The
 * default thread is implicit: it exists from the first frame and carries no
 * agent selection of its own.
 */
export interface ChatThread {
  readonly id: string;
  /** Backend-owned title; empty until the server has derived or been given one. */
  readonly title: string;
  readonly provider: string | null;
  readonly model: string | null;
}

/** A question put to a chat thread, as the terminal `chat` event recorded it. */
export interface ChatQuestion {
  /** The event's sequence as text, unique within the run. */
  readonly id: string;
  readonly text: string;
}

/**
 * A steer a thread's agent proposed (`steer_proposed`): the fields a `SteerRequest` is prefilled
 * with. Nothing here reached the run; only the operator's `command.control` does.
 */
export interface ChatProposal {
  /** The server's id for the proposal, unique within the run. */
  readonly id: string;
  readonly sequence: number;
  readonly text: string;
  readonly target: {readonly kind: 'run' | 'role' | 'attempt' | 'item'; readonly id: string | null};
  readonly mode: 'next_turn' | 'standing' | 'interrupt';
}

/** Where one operator steer or withdrawal stands, as the wire names it. */
export type SteerDeliveryStep = Extract<
  NonNullable<RunEvent['data']>,
  {kind: 'steer_delivery'}
>['delivery']['state'];

/** One step in a steer's life, as the server reported it. */
export interface SteerDeliveryFact {
  readonly sequence: number;
  readonly step: SteerDeliveryStep;
  /** The id the operator sent the steer or withdrawal under; null for a control sent without one. */
  readonly controlId: string | null;
  readonly action: 'steer' | 'withdraw' | null;
  /** For a queued withdrawal: the control id of the steer it ends. */
  readonly withdraws: string | null;
  /** The role whose turn took the steer. */
  readonly role: string | null;
  readonly attemptId: string | null;
  /** Why a steer ended unreached or was refused. */
  readonly reason: string | null;
}

/** Backend diagnostic facts. Visibility and dismissal belong to the UI. */
export interface CoreDiagnostic {
  readonly id: string | null;
  readonly code: string | null;
  readonly failureKind: Diagnostic['scope'] | 'run_interruption';
  readonly summary: string;
  readonly detail: string | null;
  readonly hint: string | null;
  readonly severity: 'warning' | 'error' | 'fatal';
  readonly scope: Diagnostic['scope'];
  /** Which subsystem raised it, e.g. `loop` on a framework warning (#692). */
  readonly source: string | null;
  readonly agentKind: string | null;
  readonly roundLabel: string | null;
  readonly invocationId: string | null;
  readonly sequence: number;
}

export interface RunLifetimeBoundary {
  readonly event: ReadonlyProjection<RunEvent>;
  /** Last event observed before a resumed run_started boundary, if known. */
  readonly closeout: Readonly<{sequence: number; timestamp: string}> | null;
}

interface CoreStateProvenance {
  readonly version: 1;
  readonly replay: ReplayProvenance;
  readonly runMap: RunMapProvenance;
  /**
   * Newest snapshot whose active-execution view has been accepted. Event
   * checkpoints may replace that view only when their own watermark advances
   * beyond this sequence. Snapshots do not move the subscription cursor, so
   * this fence must be serializable reducer provenance rather than inferred
   * from `CoreState.sequence`.
   */
  readonly activeExecutionSnapshotSequence?: number;
  /**
   * Execution-only working state while a fragmented authoritative checkpoint
   * is incomplete. The public projection keeps its last complete maps; this
   * serializable provenance lets later event frames advance the hidden maps
   * without retaining the replay events themselves.
   */
  readonly activeExecutionCheckpoint?: Readonly<{
    checkpointId: string;
    throughSequence: number | undefined;
    acceptsMetadata: boolean;
    activeExecutions: Readonly<Record<string, ActiveAgentExecution>>;
    executionStatuses: Readonly<Record<string, ExecutionStatus>>;
  }>;
}

/**
 * Run status as core state carries it: every status the backend reports, plus
 * the client-only `connecting` that precedes the first snapshot or event, and
 * the client-only `interrupted`. The backend has no `interrupted` member of
 * `RunStatus`: it reports an interruption through the separate `run_interrupted`
 * event rather than through `run_status_changed`, so the client derives this
 * status itself instead of receiving it on the wire.
 */
export type CoreRunStatus = RunStatus | 'connecting' | 'interrupted';

/** The statuses a run never leaves. Named once; `terminate` writes only these. */
export type EndedRunStatus = Extract<
  CoreRunStatus,
  'completed' | 'failed' | 'stopped' | 'interrupted'
>;

export interface CoreState {
  /** Versioned reducer provenance retained across JSON and structured clone. */
  readonly provenance: CoreStateProvenance;
  /**
   * The first non-empty backend run identity folded into this projection.
   * Foreign data is diagnosed and rejected; only `reduceEventRebootstrap` may
   * build a replacement projection that adopts another identity.
   */
  readonly runId: string | null;
  /**
   * The contiguous stream position: every event up to and including this
   * sequence has been folded, so a subscription resumes from here.
   *
   * Only `reduceEvent` and the batch reducers, which fold the subscription's
   * ordered events, move it. `reduceResponseEvents` folds out of band and
   * leaves it where it was.
   */
  readonly sequence: number;
  /**
   * Sequences folded out of band, all of them strictly above `sequence`.
   *
   * `reduceResponseEvents` records what it folded here so the subscription's
   * copy of the same event is recognized as already folded rather than folded
   * twice. An entry is dropped once `sequence` reaches it, which is why the
   * list is bounded by how far one RPC response can outrun the stream rather
   * than by the length of the run.
   */
  readonly foldedOutOfBand: readonly number[];
  readonly status: CoreRunStatus;
  readonly startup: RunStartupState | null;
  /**
   * Seconds each startup stage took, summed over its `finished` reports (a stage runs again after
   * a restart); a stage that never reported finishing is absent. Unlike `startup`, it keeps every
   * stage the run opened its environment with.
   */
  readonly startupTook: Readonly<Partial<Record<RunStartupState['stage'], number>>>;
  /** @deprecated Last event cursor; use `activeRunFocus` for current work. */
  readonly agentKind: string | null;
  /** @deprecated Last event cursor; use `activeRunFocus` for current work. */
  readonly roundLabel: string | null;
  readonly outerLoop: string | null;
  /**
   * The agent roles the backend advertised in `run_started`; null on
   * recordings that predate the field (see `expectedRolesForSeeding`).
   */
  readonly expectedRoles: readonly string[] | null;
  readonly maxRounds: number | null;
  readonly rounds: readonly RoundState[];
  readonly phases: readonly AgentPhase[];
  /**
   * Timestamp of the newest event the run map folded, or null before the first
   * one. The run map owns the field and its meaning; see `RunMapState`.
   */
  readonly lastEventTimestamp: string | null;
  /** Sequence of the latest non-chat event folded through the run map. */
  readonly lastRunMapSequence: number;
  /** Run lifetime boundaries retained from the replayed event stream. */
  readonly runLifetimeBoundaries: readonly RunLifetimeBoundary[];
  readonly activeExecutions: Readonly<Record<string, ActiveAgentExecution>>;
  /** Freshest structured status for each active or not-yet-checkpointed execution. */
  readonly executionStatuses: Readonly<Record<string, ExecutionStatus>>;
  readonly transcript: readonly TranscriptEntry[];
  /** The default thread's transcript; equals `chatTranscripts[DEFAULT_CHAT_THREAD_ID]`. */
  readonly chatTranscript: readonly TranscriptEntry[];
  /** Every thread's transcript, keyed by thread id. */
  readonly chatTranscripts: Readonly<Record<string, readonly TranscriptEntry[]>>;
  /** The default thread first, then created threads in replay order. */
  readonly chatThreads: readonly ChatThread[];
  /** The thread the server last wrote to, from the snapshot; null before one names it. */
  readonly lastChatThreadId: string | null;
  /** The steps of operator steers' lives the server reported, in journal order. */
  readonly steerDeliveries: readonly SteerDeliveryFact[];
  /** Each thread's answered questions, in journal order, keyed by thread id. */
  readonly chatQuestions: Readonly<Record<string, readonly ChatQuestion[]>>;
  /** The steers each thread's agent proposed, in journal order, keyed by thread id. */
  readonly chatProposals: Readonly<Record<string, readonly ChatProposal[]>>;
  readonly todos: readonly ExecutionTodos[];
  readonly usage: UsageMeter | null;
  readonly quota: QuotaState;
  /** Every stop on a capacity limit that held a turn for some time, in journal order. */
  readonly quotaStops: readonly QuotaStop[];
  readonly winner: WinnerSelection | null;
  /**
   * The latest comparison with its base of each candidate commit, keyed by `candidate_commit`.
   * Folded from events, so a snapshot read does not carry it.
   */
  readonly comparisons: Readonly<Record<string, MeasurementComparison>>;
  readonly benchmarks: readonly BenchmarkRecord[];
  /**
   * Per-workstream (round) terminal facts folded from `workstream_finished` and
   * `turn_usage_recorded`, keyed internally by the event's `round_label`. Join with `rounds` by
   * `RoundKey` (see `sameRoundKey`) for a round's lifecycle timing; a round with neither event yet
   * has no entry here.
   */
  readonly workstreams: WorkstreamMap;
  /**
   * Token spend folded from `turn_usage_recorded`: the run's total and each agent-turn span's.
   * Join a span's usage to its agent through the trace (see `TokenLedger`).
   */
  readonly tokens: TokenLedger;
  /** The agent executions that started, by execution id: who ran each turn (see `UsageRecord`). */
  readonly executionStarts: ExecutionStarts;
  /** One record per finished agent turn, in journal order; their tokens sum to `tokens.total`. */
  readonly usageRecords: readonly UsageRecord[];
  readonly diagnostics: readonly CoreDiagnostic[];
  /** Sequence of the latest semantic experiment invalidation. */
  readonly experimentsRevision: number;
  readonly typedToolEvents: boolean;
  /** Per thread id: typed tool events seen, so legacy tool chunks are dropped. */
  readonly chatTypedToolEvents: Readonly<Record<string, boolean>>;
  /** Count of forward-compatible event-data kinds this client could not project. */
  readonly unknownEventKinds: number;
  /**
   * Every event this state folded had `sequence > historyAfterSequence`.
   *
   * `0` means the state covers the whole history. A tail bootstrap starts it at
   * the newest sequence the client skipped and lowers it to `0` as older chunks
   * are folded in through `reduceEventPrefix`.
   */
  readonly historyAfterSequence: number;
}

export type MutableCoreState = {-readonly [Key in keyof CoreState]: CoreState[Key]};

export function initialCoreState(): CoreState {
  const runMap = initialRunMapProvenance();
  return publishCoreState({
    provenance: {version: 1, replay: emptyReplayProvenance(), runMap},
    runId: null,
    sequence: 0,
    foldedOutOfBand: [],
    status: 'connecting',
    startup: null,
    startupTook: {},
    agentKind: null,
    roundLabel: null,
    outerLoop: null,
    expectedRoles: null,
    maxRounds: null,
    rounds: [],
    phases: [],
    lastEventTimestamp: null,
    lastRunMapSequence: 0,
    runLifetimeBoundaries: [],
    activeExecutions: {},
    executionStatuses: {},
    transcript: [],
    chatTranscript: [],
    chatTranscripts: {[DEFAULT_CHAT_THREAD_ID]: []},
    // The default thread has no backend record, so it has no backend-owned
    // title either. Naming it is the consumer's job.
    chatThreads: [{id: DEFAULT_CHAT_THREAD_ID, title: '', provider: null, model: null}],
    lastChatThreadId: null,
    steerDeliveries: [],
    chatQuestions: {},
    chatProposals: {},
    todos: [],
    usage: null,
    quota: {sequence: 0, pause: null, lastSwitch: null},
    quotaStops: [],
    winner: null,
    comparisons: {},
    benchmarks: [],
    workstreams: {},
    tokens: EMPTY_LEDGER,
    executionStarts: {},
    usageRecords: [],
    diagnostics: [],
    experimentsRevision: 0,
    typedToolEvents: false,
    chatTypedToolEvents: {},
    unknownEventKinds: 0,
    historyAfterSequence: 0,
  });
}

export function publishCoreState(state: CoreState): CoreState {
  // Lazy run-map accessors publish their arrays when first read. Every eager
  // projection reference is frozen here; projection sites own any protocol
  // references first, so neither caller values nor the hidden index are frozen.
  return publishProjectionFields(state);
}

/** Restores reducer-owned lazy views after a JSON or structured-clone boundary. */
export function rehydrateCoreState(state: CoreState): CoreState {
  const provenance = coreStateProvenance(state);
  const clone = cloneCoreState(state);
  (clone as MutableCoreState).provenance = {
    version: 1,
    replay: {
      deliveries: rehydratePersistentArray(provenance.replay.deliveries),
      responses: provenance.replay.responses,
    },
    runMap: rehydrateRunMapProvenance(provenance.runMap),
    ...(provenance.activeExecutionSnapshotSequence === undefined
      ? {}
      : {activeExecutionSnapshotSequence: provenance.activeExecutionSnapshotSequence}),
    ...(provenance.activeExecutionCheckpoint === undefined
      ? {}
      : {activeExecutionCheckpoint: provenance.activeExecutionCheckpoint}),
  };
  installRunMapArrays(clone, coreStateProvenance(clone).runMap);
  return publishCoreState(clone);
}

/**
 * Whether the run has reached a status it never leaves.
 *
 * Whether a run has ended is a function of its status, so core state stores the
 * status alone and every reader asks this predicate. The switch is exhaustive:
 * a new status in the protocol is a compile error here rather than a silent
 * `false`.
 */
export function hasRunEnded(state: CoreState): boolean {
  return endedRunStatus(state.status) !== null;
}

/** The ended status `status` is, or `null` while the run can still move on. */
export function endedRunStatus(status: CoreRunStatus): EndedRunStatus | null {
  switch (status) {
    case 'completed':
    case 'failed':
    case 'stopped':
    case 'interrupted':
      return status;
    case 'connecting':
    case 'starting':
    case 'running':
    case 'pausing':
    case 'paused':
    case 'stopping':
      return null;
    default:
      return unknownRunStatus(status);
  }
}

/** #869 accepts future statuses; an unknown member carries no evidence of termination. */
function unknownRunStatus(_status: never): null {
  return null;
}

export type ActiveExecutionCheckpoint = NonNullable<RunSnapshot['active_executions']>;

/** Which of the two routes a journal event reached the fold by. */
export type DeliveryRoute = 'stream' | 'response';

export interface ReplayDelivery {
  readonly event: RunEvent;
  readonly route: DeliveryRoute;
}

export interface ReplayProvenance {
  readonly deliveries: PersistentArray<ReplayDelivery>;
  /** Pending response-route entries, normally empty and bounded by one RPC tail. */
  readonly responses: Readonly<Record<string, number>>;
}

export function emptyReplayProvenance(): ReplayProvenance {
  return {deliveries: persistentArrayFrom([]), responses: {}};
}

export function cloneCoreState(state: CoreState): CoreState {
  const clone = {} as CoreState;
  const record = clone as unknown as Record<string, unknown>;
  for (const property of Object.keys(state) as Array<keyof CoreState>) {
    if (property === 'rounds' || property === 'phases') continue;
    record[property] = state[property];
  }
  installRunMapArrays(clone, coreStateProvenance(state).runMap);
  return clone;
}

export function coreStateProvenance(state: CoreState): CoreStateProvenance {
  const provenance = (state as Partial<CoreState>).provenance;
  if (provenance?.version !== 1) {
    const version = provenance === undefined ? 'missing' : String(provenance.version);
    throw new Error(`Unsupported CoreState provenance version: ${version}`);
  }
  return provenance;
}

export function cloneCoreStateWith(state: CoreState, patch: Partial<CoreState>): CoreState {
  const next = cloneCoreState(state);
  Object.assign(next, patch);
  return next;
}
