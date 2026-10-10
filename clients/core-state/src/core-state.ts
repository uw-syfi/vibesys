import type {Diagnostic, RunEvent, RunSnapshot, RunStatus} from '@vibesys/backend-client';
import {
  applyExecutionStatus,
  applyExecutionStatusUsage,
  type ExecutionStatus,
  reconcileExecutionStatuses,
  removeExecutionStatus,
} from './execution-status.js';
import {type BenchmarkRecord, benchmarkRecordFromEvent} from './performance-projection.js';
import {
  appendPersistentArrayEntry,
  materializePersistentArray,
  type PersistentArray,
  persistentArrayAt,
  persistentArrayFrom,
  rehydratePersistentArray,
  replacePersistentArrayEntry,
} from './persistent-array.js';
import {
  ownProjectionInput,
  publishProjectionFields,
  type ReadonlyProjection,
} from './publication.js';
import {type RoundKey, roundKeyFor, roundNumberFor, sameRoundKey} from './round-key.js';
import {
  type AgentPhase,
  adoptRunMapArrays,
  applyRunMapEvent,
  initialRunMapProvenance,
  installRunMapArrays,
  type RoundState,
  type RunMapProvenance,
  rehydrateRunMapProvenance,
} from './run-map.js';

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
 * the run has none configured.
 */
export interface QuotaPause {
  readonly provider: string;
  readonly condition: 'quota_exhausted' | 'rate_limited';
  readonly detail: string;
  readonly resetsAt: number | null;
  readonly resumesAt: number | null;
  readonly policy: string | null;
  readonly fallback: {readonly provider: string; readonly model: string | null} | null;
}

/**
 * The latest capacity stop fact. `sequence` is the newest quota event folded,
 * `0` before any; `pause` is `null` once that event settled the stop (resumed,
 * abandoned or switched). Whether the run is still parked is the run status's
 * to say, so a consumer shows `pause` only while the status is pausing or paused.
 */
export interface QuotaState {
  readonly sequence: number;
  readonly pause: QuotaPause | null;
}

type RunEventData = NonNullable<RunEvent['data']>;
export type TypedToolResult = ReadonlyProjection<Extract<RunEventData, {kind: 'tool_result'}>>;
/** Typed structure a producer preserved alongside the raw tool-result text. */
export type ToolResultPayload = NonNullable<TypedToolResult['payload']>;

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

export interface TranscriptEntry {
  readonly id: string;
  readonly kind:
    | 'assistant'
    | 'prompt'
    | 'analysis'
    | 'tool'
    | 'diagnostic'
    | 'subprocess'
    | 'status'
    | 'result';
  readonly content: string;
  readonly label?: string;
  readonly tone?: 'normal' | 'success' | 'failure';
  readonly agentKind?: string;
  readonly roundLabel?: string;
  readonly roundNumber?: number;
  readonly roundKey?: RoundKey;
  readonly turnId?: string;
  readonly invocationId?: string;
  readonly startsTurn?: boolean;
  readonly toolCall?: string;
  /**
   * A shell command to give code treatment instead of word-wrapped prose.
   * Populated straight from a typed `gate_started` event's `command` field,
   * or, for recorded/legacy prose, split out by `splitFrameworkValidationCommand`.
   */
  readonly command?: string;
  readonly toolResponse?: string;
  readonly toolName?: string;
  readonly toolCallId?: string;
  readonly toolArguments?: ReadonlyProjection<Record<string, unknown>>;
  readonly toolResult?: TypedToolResult;
  /** The server cut this event's payload at its recorded-size bound. */
  readonly truncated?: true;
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
}

function isRunLifetimeBoundary(event: RunEvent): boolean {
  switch (event.type) {
    case 'run_started':
    case 'run_finished':
    case 'run_failed':
    case 'run_interrupted':
      return true;
    default:
      return false;
  }
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
  readonly todos: readonly ExecutionTodos[];
  readonly usage: UsageMeter | null;
  readonly quota: QuotaState;
  readonly benchmarks: readonly BenchmarkRecord[];
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

type MutableCoreState = {-readonly [Key in keyof CoreState]: CoreState[Key]};

export function initialCoreState(): CoreState {
  const runMap = initialRunMapProvenance();
  return publishCoreState({
    provenance: {version: 1, replay: emptyReplayProvenance(), runMap},
    runId: null,
    sequence: 0,
    foldedOutOfBand: [],
    status: 'connecting',
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
    todos: [],
    usage: null,
    quota: {sequence: 0, pause: null},
    benchmarks: [],
    diagnostics: [],
    experimentsRevision: 0,
    typedToolEvents: false,
    chatTypedToolEvents: {},
    unknownEventKinds: 0,
    historyAfterSequence: 0,
  });
}

function publishCoreState(state: CoreState): CoreState {
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
function endedRunStatus(status: CoreRunStatus): EndedRunStatus | null {
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

/** The transcript for one chat thread; unknown threads read as empty. */
export function reduceSnapshot(state: CoreState, snapshot: RunSnapshot): CoreState {
  const identity = foldRunIdentity(state, snapshot.run_id, snapshot.sequence);
  if (!identity.accepted) return publishCoreState(identity.state);
  state = identity.state;
  // The thread registry is a server projection of history already written, and
  // under a tail bootstrap it names threads created before the replay window.
  // Boot issues the snapshot query and the subscription concurrently, so the
  // batch usually lands first and the liveness guard below would otherwise drop
  // the registry entirely. Applying it to a stale snapshot is always safe.
  const registered = (snapshot.chat_threads ?? []).reduce(
    (current, thread) =>
      upsertChatThread(current, {
        id: thread.thread_id,
        title: thread.title ?? '',
        provider: thread.provider,
        model: thread.model,
      }),
    state,
  );
  if (snapshot.sequence < registered.sequence) return publishCoreState(registered);
  // Boot issues the snapshot query and the subscription concurrently, so a
  // snapshot can arrive after the events it was taken alongside. Once the fold
  // has seen the run end, a snapshot no newer than the fold cannot un-end it;
  // a genuinely newer one (a resumed run) still applies.
  if (hasRunEnded(registered) && snapshot.sequence <= registered.sequence) {
    return publishCoreState(registered);
  }
  const next = cloneCoreStateWith(registered, {
    status: snapshot.status,
    agentKind: snapshot.agent_kind ?? null,
    roundLabel: snapshot.round_label ?? null,
    activeExecutions: activeExecutionsFromCheckpoint(snapshot.active_executions ?? []),
  });
  return publishCoreState(next);
}

export type ActiveExecutionCheckpoint = NonNullable<RunSnapshot['active_executions']>;

/** Checkpoints reconcile liveness without changing the event replay cursor. */
export function reconcileActiveExecutions(
  state: CoreState,
  executions: ActiveExecutionCheckpoint,
  throughSequence?: number,
): CoreState {
  if (throughSequence !== undefined && throughSequence < state.sequence) return state;
  const next = cloneCoreStateWith(state, {
    activeExecutions: activeExecutionsFromCheckpoint(executions),
  });
  return publishCoreState(next);
}

/**
 * Fold an ordered batch before applying its backend liveness checkpoint.
 *
 * Equivalent to folding the batch one event at a time, but every transcript the
 * batch touches is built in one working array and published once, instead of
 * being copied per event. Only the state this returns is observable, so the
 * intermediate states carry the batch's starting transcripts.
 *
 * `historyAfterSequence` records the floor the batch's stream declared. Omitting
 * it leaves whatever floor the state already had. A batch containing a foreign
 * identity folds its accepted events and diagnostic, but not its checkpoint or
 * history metadata because those cannot be attributed to the owned run.
 */
export function reduceEventBatch(
  state: CoreState,
  events: readonly RunEvent[],
  activeExecutions?: ActiveExecutionCheckpoint,
  throughSequence?: number,
  historyAfterSequence?: number,
): CoreState {
  const batch = foldIdentityAwareBatch(state, events);
  const retained = retainReplayDeliveries(state, batch.state, batch.retainedDeliveries);
  if (!batch.acceptsMetadata) return publishCoreState(retained);
  const reduced =
    historyAfterSequence === undefined
      ? retained
      : cloneCoreStateWith(retained, {historyAfterSequence});
  return publishCoreState(
    activeExecutions === undefined
      ? reduced
      : reconcileActiveExecutions(reduced, activeExecutions, throughSequence),
  );
}

/**
 * Folds a batch that re-bootstraps the stream at a raised history floor.
 *
 * The server re-bootstraps when the run's durable event log is attached after
 * the client subscribed: the subscription started against the server's own
 * short bootstrap log, and the batch that follows replays the run log's tail
 * plus its spine. Those sequences number a different log, so folding the batch
 * onto the existing state would silently drop every spine event at or below
 * the stale cursor, `run_started` among them.
 *
 * The batch therefore rebuilds the core state rather than extending it. The
 * chat-thread registry survives only when the batch proves the same run,
 * because a concurrent snapshot query supplies threads that no replayed tail
 * carries. A changed, unknown, or mixed identity starts with a fresh registry.
 */
export function reduceEventRebootstrap(
  state: CoreState,
  events: readonly RunEvent[],
  activeExecutions: ActiveExecutionCheckpoint | undefined,
  throughSequence: number | undefined,
  historyAfterSequence: number,
): CoreState {
  const empty = initialCoreState();
  const batch = foldIdentityAwareBatch(empty, events);
  let reduced = retainReplayDeliveries(empty, batch.state, batch.retainedDeliveries);
  if (batch.acceptsMetadata) {
    reduced = cloneCoreStateWith(reduced, {historyAfterSequence});
    if (activeExecutions !== undefined) {
      reduced = reconcileActiveExecutions(reduced, activeExecutions, throughSequence);
    }
  }
  // The snapshot registry is valid across a store swap only when the replay
  // proves it still describes the same run. Unknown or mixed identity resets
  // it rather than relabeling another run's threads.
  if (state.runId === null || reduced.runId !== state.runId || !batch.acceptsMetadata) {
    return publishCoreState(reduced);
  }
  return publishCoreState(retainRegisteredChatThreads(reduced, state.chatThreads));
}

/**
 * Folds a chunk older than the state's declared history floor.
 *
 * Core state retains the deliveries that produced it. Backfill prepends the
 * new chunk, orders the combined journal by sequence, and runs the ordinary
 * fold once. Every projection therefore uses the same code path as a full
 * replay instead of maintaining field-specific prefix merge rules.
 *
 * The backend checkpoint remains authoritative for liveness, and the newer
 * state remains authoritative for status and stream cursor metadata. Those
 * are inputs outside the journal, not alternate projection folds.
 */
export function reduceEventPrefix(
  state: CoreState,
  events: readonly RunEvent[],
  historyAfterSequence: number,
): CoreState {
  const prefixAcceptsMetadata = acceptsRunMetadata(state.runId, events);
  const deliveries = [...ownedReplayEvents(events, 'stream'), ...retainedReplayEvents(state)].sort(
    compareReplayDeliveries,
  );
  const empty = cloneCoreStateWith(initialCoreState(), {runId: state.runId});
  const replay = foldReplayDeliveries(empty, deliveries);
  let reduced = retainRegisteredChatThreads(replay.state, state.chatThreads);
  reduced = cloneCoreStateWith(reduced, {
    runId: state.runId ?? replay.state.runId,
    sequence: state.sequence,
    foldedOutOfBand: state.foldedOutOfBand,
    status: state.status,
    agentKind: state.agentKind ?? replay.state.agentKind,
    roundLabel: state.roundLabel ?? replay.state.roundLabel,
    activeExecutions: state.activeExecutions,
    executionStatuses: reconcileExecutionStatuses(
      reduced.executionStatuses,
      state.activeExecutions,
    ),
    historyAfterSequence: prefixAcceptsMetadata ? historyAfterSequence : state.historyAfterSequence,
  });
  replaceReplayEvents(reduced, replay.retainedDeliveries);
  return publishCoreState(reduced);
}

/** Adds snapshot-only thread registry facts without merging an event projection. */
function retainRegisteredChatThreads(
  state: CoreState,
  registered: readonly ChatThread[],
): CoreState {
  return registered.reduce(retainRegisteredChatThread, state);
}

function retainRegisteredChatThread(state: CoreState, thread: ChatThread): CoreState {
  const existing = state.chatThreads.find(candidate => candidate.id === thread.id);
  if (existing === undefined) return upsertChatThread(state, thread);
  const retained = {
    ...thread,
    title: thread.title || existing.title,
    provider: thread.provider ?? existing.provider,
    model: thread.model ?? existing.model,
  };
  return retained.title === existing.title &&
    retained.provider === existing.provider &&
    retained.model === existing.model
    ? state
    : cloneCoreStateWith(state, {
        chatThreads: state.chatThreads.map(candidate =>
          candidate.id === thread.id ? retained : candidate,
        ),
      });
}

/** Retain each boundary and its closest known predecessor. */
function mergeRunLifetimeBoundaries(
  older: readonly RunLifetimeBoundary[],
  newer: readonly RunLifetimeBoundary[],
): readonly RunLifetimeBoundary[] {
  const merged = new Map<number, RunLifetimeBoundary>();
  for (const boundary of [...older, ...newer]) {
    const sequence = boundary.event.sequence;
    if (sequence === undefined) continue;
    const existing = merged.get(sequence);
    if (
      existing === undefined ||
      (boundary.closeout?.sequence ?? -1) > (existing.closeout?.sequence ?? -1)
    ) {
      merged.set(sequence, boundary);
    }
  }
  return [...merged.values()].sort(
    (left, right) => (left.event.sequence ?? 0) - (right.event.sequence ?? 0),
  );
}

function boundaryFor(
  boundaries: readonly RunLifetimeBoundary[],
  event: RunEvent,
  state: CoreState,
): RunLifetimeBoundary {
  const sequence = event.sequence;
  if (sequence === undefined) return {event: ownProjectionInput(event), closeout: null};
  const known = boundaries.find(boundary => boundary.event.sequence === sequence);
  // Chat events return before the run map fold, so the map's last timestamp
  // and sequence, rather than the core cursor, identify the closeout point.
  const observed =
    state.lastRunMapSequence > 0 &&
    state.lastRunMapSequence < sequence &&
    state.lastEventTimestamp !== null
      ? {sequence: state.lastRunMapSequence, timestamp: state.lastEventTimestamp}
      : null;
  return {
    event: ownProjectionInput(event),
    closeout:
      observed !== null && observed.sequence > (known?.closeout?.sequence ?? -1)
        ? observed
        : (known?.closeout ?? null),
  };
}

export function reduceEvent(state: CoreState, event: RunEvent): CoreState {
  const folded = foldEvent(state, event, null);
  return publishCoreState(
    retainReplayDeliveries(state, folded, ownedReplayEvents([event], 'stream')),
  );
}

/**
 * Folds the events an RPC response carried, without moving the stream cursor.
 *
 * A query's response includes the journal tail written while the request was
 * in flight (`server/api/service.py`'s chat and thread-create handlers read
 * the journal directly), so those events are the subscription's own events
 * arriving early by a second route. The subscription owns `state.sequence`,
 * because that is where a reconnect resumes from: advancing it here would
 * claim the intervening events had been folded, and the batch the
 * subscription was still holding would be dropped as stale, permanently and
 * across outages.
 *
 * The response's facts still project immediately, which is what makes a chat
 * answer appear without waiting out a poll interval. The sequences it folded
 * are recorded in `foldedOutOfBand`, so the subscription's copies fold once
 * and only advance the cursor over them.
 */
export function reduceResponseEvents(state: CoreState, events: readonly RunEvent[]): CoreState {
  const batch = foldIdentityAwareBatch(state, events, 'response');
  return publishCoreState(retainReplayDeliveries(state, batch.state, batch.retainedDeliveries));
}

/** Which of the two routes a journal event reached the fold by. */
type DeliveryRoute = 'stream' | 'response';

interface ReplayDelivery {
  readonly event: RunEvent;
  readonly route: DeliveryRoute;
}

interface ReplayProvenance {
  readonly deliveries: PersistentArray<ReplayDelivery>;
  /** Pending response-route entries, normally empty and bounded by one RPC tail. */
  readonly responses: Readonly<Record<string, number>>;
}

function emptyReplayProvenance(): ReplayProvenance {
  return {deliveries: persistentArrayFrom([]), responses: {}};
}

function retainReplayDeliveries(
  source: CoreState,
  target: CoreState,
  deliveries: readonly ReplayDelivery[],
): CoreState {
  if (deliveries.length === 0 || target === source) return target;
  let replay = coreStateProvenance(source).replay;
  for (const delivery of deliveries) replay = retainReplayDelivery(replay, delivery);
  const mutable = target as MutableCoreState;
  mutable.provenance = {...coreStateProvenance(target), replay};
  return target;
}

function retainReplayDelivery(
  replay: ReplayProvenance,
  delivery: ReplayDelivery,
): ReplayProvenance {
  const sequence = delivery.event.sequence ?? 0;
  const token = sequence > 0 ? String(sequence) : null;
  const existingIndex = token === null ? undefined : replay.responses[token];
  if (existingIndex !== undefined) {
    const existing = persistentArrayAt(replay.deliveries, existingIndex);
    if (existing?.route !== 'response' || delivery.route !== 'stream') return replay;
    const responses = {...replay.responses};
    delete responses[String(sequence)];
    return {
      deliveries: replacePersistentArrayEntry(replay.deliveries, existingIndex, delivery),
      responses,
    };
  }
  const index = replay.deliveries.length;
  return {
    deliveries: appendPersistentArrayEntry(replay.deliveries, delivery),
    responses:
      token !== null && delivery.route === 'response'
        ? {...replay.responses, [token]: index}
        : replay.responses,
  };
}

function ownedReplayEvents(events: readonly RunEvent[], route: DeliveryRoute): ReplayDelivery[] {
  return events.map(event => ({event: ownProjectionInput(event) as RunEvent, route}));
}

function retainedReplayEvents(state: CoreState): ReplayDelivery[] {
  return materializePersistentArray(coreStateProvenance(state).replay.deliveries);
}

function replaceReplayEvents(state: CoreState, deliveries: readonly ReplayDelivery[]): void {
  let replay = emptyReplayProvenance();
  for (const delivery of deliveries) replay = retainReplayDelivery(replay, delivery);
  (state as MutableCoreState).provenance = {
    ...coreStateProvenance(state),
    replay,
  };
}

function compareReplayDeliveries(left: ReplayDelivery, right: ReplayDelivery): number {
  return (left.event.sequence ?? 0) - (right.event.sequence ?? 0);
}

function acceptsRunMetadata(runId: string | null, events: readonly RunEvent[]): boolean {
  let owned = runId;
  for (const event of events) {
    const incoming = event.run_id;
    if (incoming === undefined || incoming === null || incoming === '') continue;
    if (owned === null) owned = incoming;
    else if (owned !== incoming) return false;
  }
  return true;
}

interface RunIdentityFold {
  state: CoreState;
  accepted: boolean;
}

interface IdentityAwareBatchFold {
  /** State after folding exactly the accepted events and all mismatch diagnostics. */
  state: CoreState;
  /** Deliveries that changed the projection and must participate in prefix replay. */
  retainedDeliveries: readonly ReplayDelivery[];
  /** Whether checkpoint and history metadata describe one accepted identity. */
  acceptsMetadata: boolean;
}

/**
 * Fold one delivery while retaining the identity decision for its metadata and
 * secondary projections. A rejected event still contributes its stable
 * diagnostic, but cannot authorize metadata for the delivery that carried it.
 */
function foldIdentityAwareBatch(
  state: CoreState,
  events: readonly RunEvent[],
  route: DeliveryRoute = 'stream',
): IdentityAwareBatchFold {
  return foldReplayDeliveries(state, ownedReplayEvents(events, route));
}

function foldReplayDeliveries(
  state: CoreState,
  deliveries: readonly ReplayDelivery[],
): IdentityAwareBatchFold {
  const folder = new TranscriptFolder();
  let folded = state;
  let acceptsMetadata = true;
  const retainedDeliveries: ReplayDelivery[] = [];
  for (const delivery of deliveries) {
    const {event, route} = delivery;
    const before = folded;
    const sequence = event.sequence ?? 0;
    const identity = foldRunIdentity(folded, event.run_id, sequence);
    folded = identity.state;
    if (!identity.accepted) {
      acceptsMetadata = false;
      if (folded !== before) retainedDeliveries.push(delivery);
      continue;
    }
    folded = foldAcceptedEvent(folded, event, folder, route);
    if (folded !== before) retainedDeliveries.push(delivery);
  }
  return {
    state: folder.commit(folded),
    retainedDeliveries,
    acceptsMetadata,
  };
}

/** Latch one run identity, or diagnose a foreign delivery without folding it. */
function foldRunIdentity(
  state: CoreState,
  incoming: string | null | undefined,
  sequence: number,
): RunIdentityFold {
  const runId = incoming === undefined || incoming === null || incoming === '' ? null : incoming;
  if (runId === null) return {state, accepted: true};
  if (state.runId === null) {
    return {state: cloneCoreStateWith(state, {runId}), accepted: true};
  }
  if (runId === state.runId) return {state, accepted: true};
  const diagnostic: CoreDiagnostic = {
    id: 'core-state:run-identity-mismatch',
    code: 'run_identity_mismatch',
    failureKind: 'run',
    summary: `Ignored data for run ${runId}; this projection owns ${state.runId}`,
    detail: `Received run_id ${JSON.stringify(runId)} after latching ${JSON.stringify(state.runId)}.`,
    hint: 'Re-bootstrap the client before folding a different run.',
    severity: 'error',
    scope: 'run',
    source: 'core-state',
    agentKind: null,
    roundLabel: null,
    invocationId: null,
    sequence,
  };
  return {
    state: cloneCoreStateWith(state, {
      diagnostics: upsertDiagnostic(state.diagnostics, diagnostic),
    }),
    accepted: false,
  };
}

/**
 * Clone-stage contract: `foldAcceptedEvent` owns the only mutable stage of an
 * event fold. It clones first, its `apply*` stages may replace fields on that
 * clone, and the public reducer freezes published arrays only after the fold.
 * No stage may mutate arrays or objects reachable from the input state.
 */
function foldEvent(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
  route: DeliveryRoute = 'stream',
): CoreState {
  const sequence = event.sequence ?? 0;
  const identity = foldRunIdentity(state, event.run_id, sequence);
  if (!identity.accepted) return identity.state;
  return foldAcceptedEvent(identity.state, event, folder, route);
}

/** Fold an event whose identity has already been accepted. */
function foldAcceptedEvent(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
  route: DeliveryRoute,
): CoreState {
  const sequence = event.sequence ?? 0;
  if (sequence > 0 && sequence <= state.sequence) return state;
  if (sequence > 0 && state.foldedOutOfBand.includes(sequence)) {
    // Folded already, by the other route. The stream's copy still carries the
    // contiguous position forward over it; the response's does nothing.
    return route === 'stream' ? advanceStreamCursor(state, sequence) : state;
  }
  let next = cloneCoreState(state);
  const mutable = next as MutableCoreState;
  if (route === 'stream') {
    mutable.sequence = Math.max(state.sequence, sequence);
    mutable.foldedOutOfBand = aboveCursor(state.foldedOutOfBand, next.sequence);
  } else if (sequence > 0) {
    mutable.foldedOutOfBand = [...state.foldedOutOfBand, sequence];
  }
  next = applyDiagnosticEvent(next, event);
  const dataHandlers = eventDataHandlers(event.data);
  next = applyExecutionEventData(next, event, sequence, dataHandlers);
  // The chat return sits above the status fold, not below it. The chat agent
  // runs its own session with its own context window, and the backend attaches
  // that session's status block to every chat chunk it publishes, so folding
  // one would report the chat's token count as the run's. The same exclusion
  // covers chat `usage_update` events, which `applyRunFacts` never sees.
  if (event.agent_kind === 'chat') return applyChatEvent(next, event, folder);
  next = applyRunEventData(next, event, sequence, dataHandlers);
  if (event.agent_kind) (next as MutableCoreState).agentKind = event.agent_kind;
  if (event.round_label) (next as MutableCoreState).roundLabel = event.round_label;
  next = applyRunMapProjection(next, state, event, sequence);
  next = applyRunTranscript(next, event, folder);
  return applyRunLifecycle(next, event);
}

/** Carries the contiguous position over an event the response already folded. */
function advanceStreamCursor(state: CoreState, sequence: number): CoreState {
  const cursor = Math.max(state.sequence, sequence);
  return cloneCoreStateWith(state, {
    sequence: cursor,
    foldedOutOfBand: aboveCursor(state.foldedOutOfBand, cursor),
  });
}

/** Drops the sequences a moved cursor now covers, keeping identity if none do. */
function aboveCursor(sequences: readonly number[], cursor: number): readonly number[] {
  const retained = sequences.filter(sequence => sequence > cursor);
  return retained.length === sequences.length ? sequences : retained;
}

function applyRunMapProjection(
  next: CoreState,
  previous: CoreState,
  event: RunEvent,
  sequence: number,
): CoreState {
  const mutable = next as MutableCoreState;
  const boundary =
    event.type === 'run_started' && event.sequence !== undefined
      ? boundaryFor(previous.runLifetimeBoundaries, event, previous)
      : isRunLifetimeBoundary(event)
        ? {event: ownProjectionInput(event), closeout: null}
        : null;
  const provenance = coreStateProvenance(next);
  const runMap = applyRunMapEvent(
    next,
    event,
    boundary?.closeout?.timestamp ?? null,
    provenance.runMap,
  );
  mutable.outerLoop = runMap.outerLoop;
  mutable.expectedRoles = runMap.expectedRoles;
  adoptRunMapArrays(next, runMap);
  if (runMap.provenance === undefined) {
    throw new Error('Folded run-map state has no serializable provenance');
  }
  mutable.provenance = {...provenance, runMap: runMap.provenance};
  mutable.lastEventTimestamp = runMap.lastEventTimestamp;
  mutable.lastRunMapSequence = sequence;
  if (boundary !== null) {
    mutable.runLifetimeBoundaries = mergeRunLifetimeBoundaries(next.runLifetimeBoundaries, [
      boundary,
    ]);
  }
  return next;
}

type EventDataKind = RunEventData['kind'];
type EventDataHandler = (state: CoreState, event: RunEvent, sequence: number) => CoreState;
interface EventDataHandlers {
  readonly execution: EventDataHandler;
  readonly run: EventDataHandler;
}

const ignoreEventData: EventDataHandler = state => state;
const ignoredEventData: EventDataHandlers = {
  execution: ignoreEventData,
  run: ignoreEventData,
};

function runEventData(run: EventDataHandler): EventDataHandlers {
  return {execution: ignoreEventData, run};
}

function executionEventData(
  execution: EventDataHandler,
  run: EventDataHandler = ignoreEventData,
): EventDataHandlers {
  return {execution, run};
}

/** Applies execution bookkeeping before chat events branch to their transcript. */
function applyExecutionEventData(
  state: CoreState,
  event: RunEvent,
  sequence: number,
  handlers: EventDataHandlers | null | undefined,
): CoreState {
  if (handlers === null) {
    return cloneCoreStateWith(state, {unknownEventKinds: state.unknownEventKinds + 1});
  }
  return handlers?.execution(state, event, sequence) ?? state;
}

/** Applies facts owned by the run after chat-session events have branched. */
function applyRunEventData(
  state: CoreState,
  event: RunEvent,
  sequence: number,
  handlers: EventDataHandlers | null | undefined,
): CoreState {
  return handlers?.run(state, event, sequence) ?? applyTerminalExecutionStatus(state, event);
}

function composeEventDataHandlers(...handlers: readonly EventDataHandler[]): EventDataHandler {
  return (state, event, sequence) =>
    handlers.reduce((current, handler) => handler(current, event, sequence), state);
}

/**
 * One exhaustive dispatch table for every generated event-data kind.
 *
 * `satisfies` makes a protocol kind addition a compile error here. At runtime
 * #869 permits a newer server's unknown string through the wire boundary, so
 * `eventDataHandlers` reports that separate forward-compatibility case.
 */
const EVENT_DATA_HANDLERS = {
  chat: ignoredEventData,
  chat_thread_created: ignoredEventData,
  invocation_started: ignoredEventData,
  invocation_finished: ignoredEventData,
  agent_execution_started: executionEventData(
    applyAgentExecutionStartedData,
    reconcileStartedExecutionStatus,
  ),
  agent_execution_activity_changed: executionEventData(applyAgentExecutionActivityData),
  agent_execution_finished: executionEventData(
    applyAgentExecutionFinishedData,
    removeFinishedExecutionStatus,
  ),
  output: ignoredEventData,
  server_ready: ignoredEventData,
  run_started: ignoredEventData,
  run_failed: runEventData(applyTerminalExecutionStatus),
  run_interrupted: runEventData(applyTerminalExecutionStatus),
  run_status_changed: runEventData(
    composeEventDataHandlers(applyTerminalExecutionStatus, applyRunStatusData),
  ),
  experiments_changed: runEventData(applyExperimentsChangedData),
  configuration_failed: runEventData(applyTerminalExecutionStatus),
  phase: ignoredEventData,
  agent_output_chunk: runEventData(applyAgentStatusData),
  subprocess_output: ignoredEventData,
  judge_result: ignoredEventData,
  benchmark_result: runEventData(applyBenchmarkData),
  round_finished: ignoredEventData,
  tool_call: runEventData(composeEventDataHandlers(applyAgentStatusData, applyTypedToolData)),
  tool_result: runEventData(applyTypedToolData),
  todo_update: runEventData(applyTodoData),
  usage_update: runEventData(applyUsageData),
  rate_limit_update: ignoredEventData,
  quota_paused: runEventData(applyQuotaData),
  quota_resumed: runEventData(applyQuotaData),
  quota_abandoned: runEventData(applyQuotaData),
  provider_switched: runEventData(applyQuotaData),
  gate_started: ignoredEventData,
  gate_finished: runEventData(applyBenchmarkData),
  workspace_snapshot: ignoredEventData,
  run_configured: ignoredEventData,
  framework_warning: ignoredEventData,
} satisfies Record<EventDataKind, EventDataHandlers>;

function eventDataHandlers(data: RunEvent['data']): EventDataHandlers | null | undefined {
  if (data === undefined || data === null) return undefined;
  return isKnownEventDataKind(data.kind) ? EVENT_DATA_HANDLERS[data.kind] : null;
}

function isKnownEventDataKind(kind: string): kind is EventDataKind {
  return Object.hasOwn(EVENT_DATA_HANDLERS, kind);
}

function applyTypedToolData(state: CoreState): CoreState {
  return state.typedToolEvents ? state : cloneCoreStateWith(state, {typedToolEvents: true});
}

function applyTodoData(state: CoreState, event: RunEvent): CoreState {
  return cloneCoreStateWith(state, {todos: updateTodos(state.todos, event)});
}

function applyUsageData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'usage_update') return state;
  return cloneCoreStateWith(state, {
    usage: {
      inputTokens: data.input_tokens,
      contextWindow: data.context_window ?? null,
      model: data.model ?? null,
    },
  });
}

function applyQuotaData(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const quota = quotaFromEvent(state.quota, event, sequence);
  return quota === state.quota ? state : cloneCoreStateWith(state, {quota});
}

function applyBenchmarkData(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const benchmark = benchmarkRecordFromEvent(event, sequence);
  return benchmark === null
    ? state
    : cloneCoreStateWith(state, {benchmarks: [...state.benchmarks, benchmark]});
}

function applyExperimentsChangedData(
  state: CoreState,
  _event: RunEvent,
  sequence: number,
): CoreState {
  return cloneCoreStateWith(state, {experimentsRevision: sequence});
}

function applyRunStatusData(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (data?.kind !== 'run_status_changed') return state;
  // The backend owns the run's lifecycle and publishes every move through it,
  // so the projection folds the status it is told rather than inferring one.
  return applyRunStatus(state, data.status);
}

/** The quota state after `event`: a pause sets it, any event that settles the stop clears it. */
function quotaFromEvent(quota: QuotaState, event: RunEvent, sequence: number): QuotaState {
  const data = event.data;
  switch (data?.kind) {
    case 'quota_paused':
      return {
        sequence,
        pause: {
          provider: data.provider,
          condition: data.condition,
          detail: data.detail,
          resetsAt: data.resets_at ?? null,
          resumesAt: data.resumes_at ?? null,
          policy: data.policy ?? null,
          fallback:
            data.fallback_provider == null
              ? null
              : {provider: data.fallback_provider, model: data.fallback_model ?? null},
        },
      };
    case 'quota_resumed':
    case 'quota_abandoned':
    case 'provider_switched':
      return {sequence, pause: null};
    default:
      return quota;
  }
}

function applyRunTranscript(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const legacyToolChunk =
    data?.kind === 'agent_output_chunk' && data.channel === 'tool' && state.typedToolEvents;
  if (legacyToolChunk) return state;
  const entry = eventToTranscriptEntry(event);
  if (entry === null) return state;
  if (folder === null)
    (state as MutableCoreState).transcript = appendTranscript(state.transcript, entry);
  else folder.buffer(RUN_TRANSCRIPT, state.transcript).append(entry);
  return state;
}

function applyRunLifecycle(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (event.type === 'run_started') {
    const mutable = state as MutableCoreState;
    mutable.status = 'running';
    if (data?.kind === 'run_started') mutable.maxRounds = data.max_rounds ?? null;
  }
  if (event.type === 'configuration_failed') return terminate(state, 'failed');
  if (event.type === 'run_finished') return terminate(state, 'completed');
  if (event.type === 'run_failed') return terminate(state, 'failed');
  if (event.type === 'run_interrupted') return terminate(state, 'interrupted');
  return state;
}

function cloneCoreState(state: CoreState): CoreState {
  const clone = {} as CoreState;
  const record = clone as unknown as Record<string, unknown>;
  for (const property of Object.keys(state) as Array<keyof CoreState>) {
    if (property === 'rounds' || property === 'phases') continue;
    record[property] = state[property];
  }
  installRunMapArrays(clone, coreStateProvenance(state).runMap);
  return clone;
}

function coreStateProvenance(state: CoreState): CoreStateProvenance {
  const provenance = (state as Partial<CoreState>).provenance;
  if (provenance?.version !== 1) {
    const version = provenance === undefined ? 'missing' : String(provenance.version);
    throw new Error(`Unsupported CoreState provenance version: ${version}`);
  }
  return provenance;
}

function cloneCoreStateWith(state: CoreState, patch: Partial<CoreState>): CoreState {
  const next = cloneCoreState(state);
  Object.assign(next, patch);
  return next;
}

/** Returns the one diagnostic added or updated by a reducer transition. */
export function latestDiagnosticChange(
  previous: CoreState,
  current: CoreState,
): CoreDiagnostic | null {
  if (previous.diagnostics === current.diagnostics) return null;
  return (
    current.diagnostics.find((diagnostic, index) => diagnostic !== previous.diagnostics[index]) ??
    null
  );
}

/**
 * Whether `event` is the run reaching a status it never leaves.
 *
 * The same fact `applyRunLifecycle` and `applyRunStatus` route to `terminate`,
 * asked one stage earlier so the per-execution status map closes out with the
 * rest of the run. Statuses are the reason this is not just a list of event
 * types: an operator `/stop` ends the run through `run_status_changed` alone,
 * with no run-scoped terminal event after it.
 *
 * `run-map.ts`'s `runClosingStatus` is deliberately narrower: it answers what
 * to write onto work that was still open, and `completed` and `failed` already
 * have a terminal event that owns that. Clearing a status map the run has
 * finished with has no such owner and no ordering hazard.
 */
function endsRun(event: RunEvent): boolean {
  switch (event.type) {
    case 'run_finished':
    case 'run_failed':
    case 'run_interrupted':
    case 'configuration_failed':
      return true;
    default: {
      const data = event.data;
      return data?.kind === 'run_status_changed' && endedRunStatus(data.status) !== null;
    }
  }
}

/** Folds one backend-published status, ending the run when that status has. */
function applyRunStatus(state: CoreState, status: CoreRunStatus): CoreState {
  const ended = endedRunStatus(status);
  return ended === null ? cloneCoreStateWith(state, {status}) : terminate(state, ended);
}

/**
 * Ends the run, unless it already has.
 *
 * A recorded run_interrupted is followed by a run_failed for the same
 * boundary (the backend's coarse RunStatus has no `interrupted` member, so it
 * also reports the generic failure for anything that only understands that
 * one); folding both must not let the second, less specific event overwrite
 * the first. `closePhase` in `run-map.ts` holds the same line for per-phase
 * status, by leaving an already-closed phase alone. This is the run-level
 * counterpart: once a status a run never leaves is written, `terminate` no
 * longer has anything to write.
 */
function terminate(state: CoreState, status: EndedRunStatus): CoreState {
  if (hasRunEnded(state)) return state;
  return cloneCoreStateWith(state, {status, activeExecutions: {}});
}

function activeExecutionsFromCheckpoint(
  executions: ActiveExecutionCheckpoint,
): Record<string, ActiveAgentExecution> {
  return Object.fromEntries(
    executions.map(execution => [execution.execution_id, activeExecutionFromCheckpoint(execution)]),
  );
}

function activeExecutionFromCheckpoint(
  execution: ActiveExecutionCheckpoint[number],
): ActiveAgentExecution {
  const roundKey = roundKeyFor(execution);
  return {
    executionId: execution.execution_id,
    agentKind: execution.agent_kind,
    roundLabel: execution.round_label ?? null,
    roundNumber: roundNumberFor(roundKey),
    roundKey,
    stage: execution.stage,
    attempt: execution.attempt ?? null,
    assignment: execution.assignment,
    startedAt: execution.started_at,
    activity: {
      mode: execution.activity.mode,
      summary: execution.activity.summary,
      tool: execution.activity.tool ?? null,
    },
    provider: execution.provider ?? null,
    model: execution.model ?? null,
  };
}

function applyAgentExecutionStartedData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  const data = event.data;
  if (executionId == null || data?.kind !== 'agent_execution_started') return state;
  const roundKey = roundKeyFor(event);
  return cloneCoreStateWith(state, {
    activeExecutions: {
      ...state.activeExecutions,
      [executionId]: {
        executionId,
        agentKind: event.agent_kind ?? 'agent',
        roundLabel: event.round_label ?? null,
        roundNumber: roundNumberFor(roundKey),
        roundKey,
        stage: data.stage,
        attempt: data.attempt ?? null,
        assignment: data.user_prompt ?? '',
        startedAt: event.timestamp,
        activity: {
          mode: data.activity.mode,
          summary: data.activity.summary,
          tool: data.activity.tool ?? null,
        },
        provider: data.provider ?? null,
        model: data.model ?? null,
      },
    },
  });
}

function applyAgentExecutionActivityData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  const data = event.data;
  if (executionId == null || data?.kind !== 'agent_execution_activity_changed') return state;
  const current = state.activeExecutions[executionId];
  if (current === undefined) return state;
  return cloneCoreStateWith(state, {
    activeExecutions: {
      ...state.activeExecutions,
      [executionId]: {
        ...current,
        activity: {mode: data.mode, summary: data.summary, tool: data.tool ?? null},
      },
    },
  });
}

function applyAgentExecutionFinishedData(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  if (executionId == null || event.data?.kind !== 'agent_execution_finished') return state;
  const {[executionId]: _finished, ...remaining} = state.activeExecutions;
  return cloneCoreStateWith(state, {activeExecutions: remaining});
}

function reconcileStartedExecutionStatus(state: CoreState): CoreState {
  const executionStatuses = reconcileExecutionStatuses(
    state.executionStatuses,
    state.activeExecutions,
  );
  return executionStatuses === state.executionStatuses
    ? state
    : cloneCoreStateWith(state, {executionStatuses});
}

function removeFinishedExecutionStatus(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  if (executionId == null) return state;
  const executionStatuses = removeExecutionStatus(state.executionStatuses, executionId);
  return executionStatuses === state.executionStatuses
    ? state
    : cloneCoreStateWith(state, {executionStatuses});
}

function applyTerminalExecutionStatus(state: CoreState, event: RunEvent): CoreState {
  return endsRun(event) && Object.keys(state.executionStatuses).length > 0
    ? cloneCoreStateWith(state, {executionStatuses: {}})
    : state;
}

function applyAgentStatusData(state: CoreState, event: RunEvent): CoreState {
  const previousStatuses = state.executionStatuses;
  const reconciled =
    Object.keys(state.activeExecutions).length === 0
      ? previousStatuses
      : reconcileExecutionStatuses(previousStatuses, state.activeExecutions);
  const executionStatuses = applyExecutionStatus(reconciled, event);
  const usage = applyExecutionStatusUsage(state.usage, previousStatuses, executionStatuses, event);
  if (executionStatuses === previousStatuses && usage === state.usage) return state;
  return cloneCoreStateWith(state, {executionStatuses, usage});
}

function updateTodos(
  previous: readonly ExecutionTodos[],
  event: RunEvent,
): readonly ExecutionTodos[] {
  const data = event.data;
  if (data?.kind !== 'todo_update') return previous;
  const agentKind = event.agent_kind ?? null;
  const roundKey = roundKeyFor(event);
  const roundNumber = roundNumberFor(roundKey);
  const executionId = event.execution_id ?? event.invocation_id ?? null;
  const retained = previous.filter(item =>
    executionId === null
      ? item.executionId != null ||
        item.agentKind !== agentKind ||
        !sameRoundKey(item.roundKey, roundKey)
      : item.executionId !== executionId,
  );
  return [
    ...retained,
    {
      executionId,
      agentKind,
      roundNumber,
      roundKey,
      items: (data.todos ?? []).map(todo => ({
        content: String(todo.content),
        status: String(todo.status),
      })),
    },
  ].slice(-100);
}

function applyChatEvent(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const threadId = event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID;
  if (data?.kind === 'chat_thread_created') {
    return upsertChatThread(state, {
      id: data.thread_id,
      title: data.title ?? '',
      provider: data.provider,
      model: data.model,
    });
  }
  let next = state;
  const typed = data?.kind === 'tool_call' || data?.kind === 'tool_result';
  if (typed && next.chatTypedToolEvents[threadId] !== true) {
    next = cloneCoreStateWith(next, {
      chatTypedToolEvents: {...next.chatTypedToolEvents, [threadId]: true},
    });
  }
  const legacyToolChunk =
    data?.kind === 'agent_output_chunk' &&
    data.channel === 'tool' &&
    next.chatTypedToolEvents[threadId] === true;
  if (legacyToolChunk) return next;
  if (data?.kind === 'chat' && data.thread_title) {
    next = setChatThreadTitle(next, threadId, data.thread_title);
  }
  const entry = eventToTranscriptEntry(event);
  if (entry === null || (entry.kind !== 'assistant' && entry.kind !== 'result')) return next;
  return appendChatTranscript(next, threadId, entry, folder, data?.kind === 'chat');
}

/** Registers a replayed thread, or refreshes the record it already has. */
function upsertChatThread(state: CoreState, thread: ChatThread): CoreState {
  const existing = state.chatThreads.find(candidate => candidate.id === thread.id);
  const chatThreads =
    existing === undefined
      ? [...state.chatThreads, thread]
      : state.chatThreads.map(candidate =>
          candidate.id === thread.id
            ? {...thread, title: thread.title || candidate.title}
            : candidate,
        );
  const chatTranscripts =
    state.chatTranscripts[thread.id] === undefined
      ? {...state.chatTranscripts, [thread.id]: []}
      : state.chatTranscripts;
  return cloneCoreStateWith(state, {chatThreads, chatTranscripts});
}

function setChatThreadTitle(state: CoreState, threadId: string, title: string): CoreState {
  if (!state.chatThreads.some(thread => thread.id === threadId)) {
    // A titled turn for a thread whose creation event is missing from the
    // replay window still names a thread the operator can select.
    return setChatThreadTitle(
      upsertChatThread(state, {id: threadId, title: '', provider: null, model: null}),
      threadId,
      title,
    );
  }
  return cloneCoreStateWith(state, {
    chatThreads: state.chatThreads.map(thread =>
      thread.id === threadId ? {...thread, title} : thread,
    ),
  });
}

function appendChatTranscript(
  state: CoreState,
  threadId: string,
  entry: TranscriptEntry,
  folder: TranscriptFolder | null,
  finalAnswer = false,
): CoreState {
  if (folder !== null) {
    const buffer = folder.buffer(threadId, state.chatTranscripts[threadId] ?? []);
    if (!(finalAnswer && foldChatAnswer(buffer.entries, entry))) buffer.append(entry);
    return state;
  }
  const transcript = [...(state.chatTranscripts[threadId] ?? [])];
  if (!(finalAnswer && foldChatAnswer(transcript, entry))) {
    foldTranscriptEntry(transcript, entry, null);
  }
  const chatTranscripts = {...state.chatTranscripts, [threadId]: transcript};
  return cloneCoreStateWith(state, {
    chatTranscripts,
    chatTranscript: threadId === DEFAULT_CHAT_THREAD_ID ? transcript : state.chatTranscript,
  });
}

/**
 * Folds a turn's terminal answer over its own streamed chunks.
 *
 * The assistant chunks of one chat turn have already merged into a single
 * entry keyed by the turn's invocation id, and the terminal `chat` event
 * carries the same text once more. When such a turn is still open at the end
 * of the transcript, the final answer replaces it in place rather than
 * appearing as a second copy. The streamed entry's id is kept so consumers
 * tracking entries by id update instead of duplicating, and the turn id is
 * dropped because the turn is over: neither a later chunk nor a later answer
 * may fold into it. Returns false when there is no open streamed turn, in
 * which case the answer appends as its own entry.
 *
 * An answer stamped with an invocation id owns exactly the turn that streamed
 * under that id: a mismatch means the open turn was abandoned (its invocation
 * failed before a terminal answer was recorded), so the answer appends and
 * the abandoned turn stays as it streamed. Answers from journals written
 * before the id existed carry none and keep the last-open-turn fold.
 */
function foldChatAnswer(entries: TranscriptEntry[], incoming: TranscriptEntry): boolean {
  const last = entries.at(-1);
  if (last === undefined || last.kind !== 'assistant' || last.turnId === undefined) return false;
  if (incoming.invocationId !== undefined && incoming.invocationId !== last.invocationId) {
    return false;
  }
  const {turnId: _closed, ...merged} = {...last, ...incoming, id: last.id};
  entries[entries.length - 1] = merged;
  return true;
}

function applyDiagnosticEvent(state: CoreState, event: RunEvent): CoreState {
  const diagnostic = diagnosticFromEvent(event);
  if (diagnostic === null) return state;
  return cloneCoreStateWith(state, {
    diagnostics: upsertDiagnostic(state.diagnostics, diagnostic),
  });
}

/** Merges `incoming` into the diagnostic it identifies, else appends it. */
function upsertDiagnostic(
  diagnostics: readonly CoreDiagnostic[],
  incoming: CoreDiagnostic,
): CoreDiagnostic[] {
  const existingIndex = diagnostics.findIndex(existing =>
    incoming.id !== null
      ? existing.id === incoming.id
      : existing.id === null &&
        incoming.invocationId !== null &&
        existing.invocationId === incoming.invocationId,
  );
  if (existingIndex === -1) return [...diagnostics, incoming];
  return diagnostics.map((existing, index) =>
    index === existingIndex ? mergeDiagnostic(existing, incoming) : existing,
  );
}

function mergeDiagnostic(existing: CoreDiagnostic, incoming: CoreDiagnostic): CoreDiagnostic {
  return {
    ...existing,
    ...incoming,
    code: incoming.code ?? existing.code,
    detail: incoming.detail ?? existing.detail,
    hint: incoming.hint ?? existing.hint,
    severity:
      diagnosticSeverityRank(incoming.severity) > diagnosticSeverityRank(existing.severity)
        ? incoming.severity
        : existing.severity,
    // A recorded run_interrupted carries the same diagnostic id as the
    // run_failed that follows it for the same boundary (the backend's coarse
    // RunStatus has no `interrupted` member, so it also reports the generic
    // failure). Both diagnostics merge by that shared id, and `run_interruption`
    // must survive the merge regardless of which side computed it, the same way
    // `terminate` keeps the run's own status from being downgraded by the event
    // that follows it.
    failureKind:
      existing.failureKind === 'run_interruption' || incoming.failureKind === 'run_interruption'
        ? 'run_interruption'
        : incoming.failureKind,
    source: incoming.source ?? existing.source,
    agentKind: incoming.agentKind ?? existing.agentKind,
    roundLabel: incoming.roundLabel ?? existing.roundLabel,
    invocationId: incoming.invocationId ?? existing.invocationId,
    sequence: Math.max(existing.sequence, incoming.sequence),
  };
}

function diagnosticSeverityRank(severity: CoreDiagnostic['severity']): number {
  if (severity === 'fatal') return 2;
  if (severity === 'error') return 1;
  return 0;
}

function diagnosticFromEvent(event: RunEvent): CoreDiagnostic | null {
  const diagnostic = event.diagnostic;
  if (diagnostic !== null && diagnostic !== undefined) {
    return fromProtocolDiagnostic(event, diagnostic);
  }
  const fallback = fallbackDiagnosticFromEvent(event);
  return fallback === null
    ? null
    : fallbackDiagnostic(event, fallback.summary, fallback.scope, fallback.severity, fallback.code);
}

interface FallbackDiagnostic {
  summary: string;
  scope: Diagnostic['scope'];
  severity: CoreDiagnostic['severity'];
  code?: string | null;
}

/** Classifies legacy failure envelopes that predate structured diagnostics. */
function fallbackDiagnosticFromEvent(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (data?.kind === 'configuration_failed') {
    return {
      summary: configurationFailureContent(data),
      scope: 'configuration',
      severity: 'fatal',
      code: data.code,
    };
  }
  const invocation = invocationFallbackDiagnostic(event);
  if (invocation !== null) return invocation;
  return runFallbackDiagnostic(event);
}

function invocationFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (
    data?.kind !== 'invocation_finished' ||
    ((data.error === null || data.error === undefined) && event.status !== 'failed')
  ) {
    return null;
  }
  return {
    summary: data.error || event.text || 'Agent invocation failed.',
    scope: 'invocation',
    severity: 'error',
  };
}

function runFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  if (event.type !== 'run_failed' && event.type !== 'run_interrupted') return null;
  const data = event.data;
  const interruption =
    data?.kind === 'run_interrupted'
      ? `${data.reason}${data.signal === null ? '' : ` (${data.signal})`}`
      : '';
  return {
    summary:
      event.text ||
      interruption ||
      (event.type === 'run_failed' ? 'Run failed.' : 'Run interrupted.'),
    scope: 'run',
    severity: 'fatal',
  };
}

function fromProtocolDiagnostic(event: RunEvent, diagnostic: Diagnostic): CoreDiagnostic {
  return {
    id: diagnostic.id ?? null,
    code: diagnostic.code ?? null,
    failureKind: failureKind(diagnostic.scope, event.type),
    summary: diagnostic.summary,
    detail: diagnostic.detail ?? null,
    hint: diagnostic.hint ?? null,
    severity: diagnostic.severity ?? 'error',
    scope: diagnostic.scope,
    source: diagnostic.source ?? null,
    agentKind: event.agent_kind ?? null,
    roundLabel: event.round_label ?? null,
    invocationId: event.invocation_id ?? null,
    sequence: event.sequence ?? 0,
  };
}

function fallbackDiagnostic(
  event: RunEvent,
  summary: string,
  scope: Diagnostic['scope'],
  severity: CoreDiagnostic['severity'],
  code: string | null = null,
): CoreDiagnostic {
  return {
    id: null,
    code,
    failureKind: failureKind(scope, event.type),
    summary,
    detail: null,
    hint: null,
    severity,
    scope,
    source: null,
    agentKind: event.agent_kind ?? null,
    roundLabel: event.round_label ?? null,
    invocationId: event.invocation_id ?? null,
    sequence: event.sequence ?? 0,
  };
}

function failureKind(
  scope: Diagnostic['scope'],
  eventType: RunEvent['type'],
): CoreDiagnostic['failureKind'] {
  return eventType === 'run_interrupted' ? 'run_interruption' : scope;
}

// Sits at the extraction seam, not the top import block: imports hoist, and
// pending fixes cite this file's earlier regions by line number (#856).
import {
  appendTranscript,
  configurationFailureContent,
  eventToTranscriptEntry,
  foldTranscriptEntry,
  RUN_TRANSCRIPT,
  TranscriptBuffer,
  type TranscriptEntry as TranscriptModuleEntry,
} from './transcript.js';

/** The transcripts one `reduceEventBatch` call touched, folded once each. */
class TranscriptFolder {
  readonly #buffers = new Map<string, TranscriptBuffer>();

  buffer(key: string, initial: readonly TranscriptEntry[]): TranscriptBuffer {
    const existing = this.#buffers.get(key);
    if (existing !== undefined) return existing;
    const created = new TranscriptBuffer(initial);
    this.#buffers.set(key, created);
    return created;
  }

  /** Publishes every folded transcript onto the batch's final state. */
  commit(state: CoreState): CoreState {
    if (this.#buffers.size === 0) return state;
    const run = this.#buffers.get(RUN_TRANSCRIPT);
    let next = run === undefined ? state : cloneCoreStateWith(state, {transcript: run.entries});
    const threads = [...this.#buffers].filter(([key]) => key !== RUN_TRANSCRIPT);
    if (threads.length === 0) return next;
    const chatTranscripts = {...next.chatTranscripts};
    for (const [threadId, buffer] of threads) chatTranscripts[threadId] = buffer.entries;
    next = cloneCoreStateWith(next, {
      chatTranscripts,
      chatTranscript: chatTranscripts[DEFAULT_CHAT_THREAD_ID] ?? next.chatTranscript,
    });
    return next;
  }
}

/**
 * `TranscriptEntry` is declared twice on purpose. This module keeps the
 * canonical declaration where it has always been, because pending fixes cite
 * this file by line number and the declaration sits above the cited regions,
 * while `transcript.ts` carries a structural copy, because a type-only import
 * back into the extracted module would register as a dependency cycle under
 * `check:ts-architecture`. This assertion compiles only while the two
 * declarations are identical, so they cannot drift apart silently.
 */
type IdenticalDeclarations<X, Y> =
  (<T>() => T extends X ? 1 : 2) extends <T>() => T extends Y ? 1 : 2 ? true : false;
true satisfies IdenticalDeclarations<TranscriptEntry, TranscriptModuleEntry>;
