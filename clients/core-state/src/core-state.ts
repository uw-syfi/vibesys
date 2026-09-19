import {
  AgentOutputChannel,
  type AgentOutputChunkData,
  type ChatData,
  type Diagnostic,
  EventStatus,
  EventType,
  FrameworkSource,
  type GateFinishedData,
  GateKind,
  type GateStartedData,
  JudgeVerdict,
  OutputStream,
  type RoundFinishedData,
  RoundJudgeVerdict,
  type RunConfiguredData,
  type RunEvent,
  type RunSnapshot,
  type ToolCallData,
  type ToolResultData,
  timestampToIso,
  type ActiveAgentExecution as WireActiveExecution,
  type WorkspaceSnapshotData,
} from '@vibesys/backend-client';
import {
  type AgentExecutionMode,
  activityFrom,
  type CoreRunStatus,
  coreRunStatus,
  type DiagnosticScopeName,
  type DiagnosticSeverityName,
  enumWord,
  scopeName,
  severityName,
} from './wire-names.js';

export type {AgentExecutionMode, CoreRunStatus, DiagnosticScopeName, DiagnosticSeverityName};

import {
  applyExecutionStatus,
  applyExecutionStatusUsage,
  type ExecutionStatus,
  mergeExecutionStatusesPrefix,
  mergeExecutionStatusUsagePrefix,
  reconcileExecutionStatuses,
  removeExecutionStatus,
} from './execution-status.js';
import {
  type AgentPhase,
  adoptRunMapArrays,
  applyRunMapEvent,
  indexRunMapArrays,
  mergePhaseLists,
  mergeRoundLists,
  type RoundSummary,
  roundNumberFromLabel,
} from './run-map.js';

export interface ActiveAgentExecution {
  executionId: string;
  agentKind: string;
  roundLabel: string | null;
  roundNumber: number | null;
  stage: string;
  attempt: number | null;
  assignment: string;
  startedAt: string;
  activity: {mode: AgentExecutionMode; summary: string; tool?: string | null};
  driver?: string | null;
  provider?: string | null;
  model?: string | null;
}

export interface TodoItem {
  content: string;
  status: string;
}

export interface ExecutionTodos {
  executionId?: string | null;
  agentKind: string | null;
  roundNumber: number | null;
  items: TodoItem[];
}

export interface UsageMeter {
  inputTokens: number;
  contextWindow: number | null;
  model: string | null;
}

export interface BenchmarkRecord {
  sequence: number;
  roundNumber: number | null;
  metric: string;
  value: number;
  unit: string;
}

/** A round as core state carries the run map's timing, status, and profile result. */
export type RoundState = RoundSummary;

export type TypedToolResult = ToolResultData;
/** Typed structure a producer preserved alongside the raw tool-result text. */
export type ToolResultPayload = Exclude<ToolResultData['payload'], {case: undefined}>;

/** Thread id used for chat events recorded without one. */
export const DEFAULT_CHAT_THREAD_ID = 'default';

/**
 * One experiment-chat thread, replayed from `chat_thread_created` events. The
 * default thread is implicit: it exists from the first frame and carries no
 * agent selection of its own.
 */
export interface ChatThread {
  id: string;
  /** Backend-owned title; empty until the server has derived or been given one. */
  title: string;
  driver: string | null;
  provider: string | null;
  model: string | null;
}

export interface TranscriptEntry {
  id: string;
  kind:
    | 'assistant'
    | 'prompt'
    | 'analysis'
    | 'tool'
    | 'diagnostic'
    | 'subprocess'
    | 'status'
    | 'result';
  content: string;
  label?: string;
  tone?: 'normal' | 'success' | 'failure';
  agentKind?: string;
  roundLabel?: string;
  roundNumber?: number;
  turnId?: string;
  invocationId?: string;
  startsTurn?: boolean;
  toolCall?: string;
  /**
   * A shell command to give code treatment instead of word-wrapped prose.
   * Populated straight from a typed `gate_started` event's `command` field,
   * or, for recorded/legacy prose, split out by `splitFrameworkValidationCommand`.
   */
  command?: string;
  toolResponse?: string;
  toolName?: string;
  toolCallId?: string;
  toolArguments?: Record<string, unknown>;
  toolResult?: TypedToolResult;
}

/** Backend diagnostic facts. Visibility and dismissal belong to the UI. */
export interface CoreDiagnostic {
  id: string | null;
  code: string | null;
  failureKind: DiagnosticScopeName | 'run_interruption';
  summary: string;
  detail: string | null;
  hint: string | null;
  severity: DiagnosticSeverityName;
  scope: DiagnosticScopeName;
  /** Which subsystem raised it, e.g. `loop` on a framework warning (#692). */
  source: string | null;
  agentKind: string | null;
  roundLabel: string | null;
  invocationId: string | null;
  sequence: number;
}

export interface RunLifetimeBoundary {
  event: RunEvent;
  /** Last event observed before a resumed run_started boundary, if known. */
  closeout: {sequence: number; timestamp: string} | null;
}

function isRunLifetimeBoundary(event: RunEvent): boolean {
  switch (event.type) {
    case EventType.RUN_STARTED:
    case EventType.RUN_FINISHED:
    case EventType.RUN_FAILED:
    case EventType.RUN_INTERRUPTED:
      return true;
    default:
      return false;
  }
}

/** The statuses a run never leaves. Named once; `terminate` writes only these. */
export type EndedRunStatus = Extract<CoreRunStatus, 'completed' | 'failed' | 'stopped'>;

export interface CoreState {
  sequence: number;
  status: CoreRunStatus;
  agentKind: string | null;
  roundLabel: string | null;
  outerLoop: string | null;
  /**
   * The agent roles the backend advertised in `run_started`; null on
   * recordings that predate the field (see `expectedRolesForSeeding`).
   */
  expectedRoles: readonly string[] | null;
  maxRounds: number | null;
  rounds: RoundState[];
  phases: AgentPhase[];
  /**
   * Timestamp of the newest event the run map folded, or null before the first
   * one. The run map owns the field and its meaning; see `RunMapState`.
   */
  lastEventTimestamp: string | null;
  /** Sequence of the latest non-chat event folded through the run map. */
  lastRunMapSequence: number;
  /** Run lifetime boundaries retained from the replayed event stream. */
  runLifetimeBoundaries: readonly RunLifetimeBoundary[];
  activeExecutions: Record<string, ActiveAgentExecution>;
  /** Freshest structured status for each active or not-yet-checkpointed execution. */
  executionStatuses: Record<string, ExecutionStatus>;
  transcript: TranscriptEntry[];
  /** The default thread's transcript; equals `chatTranscripts[DEFAULT_CHAT_THREAD_ID]`. */
  chatTranscript: TranscriptEntry[];
  /** Every thread's transcript, keyed by thread id. */
  chatTranscripts: Record<string, TranscriptEntry[]>;
  /** The default thread first, then created threads in replay order. */
  chatThreads: ChatThread[];
  todos: ExecutionTodos[];
  usage: UsageMeter | null;
  benchmarks: BenchmarkRecord[];
  diagnostics: CoreDiagnostic[];
  /** Sequence of the latest semantic experiment invalidation. */
  experimentsRevision: number;
  typedToolEvents: boolean;
  /** Per thread id: typed tool events seen, so legacy tool chunks are dropped. */
  chatTypedToolEvents: Record<string, boolean>;
  /**
   * Every event this state folded had `sequence > historyAfterSequence`.
   *
   * `0` means the state covers the whole history. A tail bootstrap starts it at
   * the newest sequence the client skipped and lowers it to `0` as older chunks
   * are folded in through `reduceEventPrefix`.
   */
  historyAfterSequence: number;
}

export function initialCoreState(): CoreState {
  return {
    sequence: 0,
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
    chatThreads: [
      {id: DEFAULT_CHAT_THREAD_ID, title: '', driver: null, provider: null, model: null},
    ],
    todos: [],
    usage: null,
    benchmarks: [],
    diagnostics: [],
    experimentsRevision: 0,
    typedToolEvents: false,
    chatTypedToolEvents: {},
    historyAfterSequence: 0,
  };
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
      return status;
    case 'connecting':
    case 'starting':
    case 'running':
    case 'pausing':
    case 'paused':
    case 'stopping':
      return null;
    default: {
      const unhandled: never = status;
      return unhandled;
    }
  }
}

/** The transcript for one chat thread; unknown threads read as empty. */
export function chatTranscriptFor(state: CoreState, threadId: string): TranscriptEntry[] {
  return state.chatTranscripts[threadId] ?? [];
}

export function reduceSnapshot(state: CoreState, snapshot: RunSnapshot): CoreState {
  // The thread registry is a server projection of history already written, and
  // under a tail bootstrap it names threads created before the replay window.
  // Boot issues the snapshot query and the subscription concurrently, so the
  // batch usually lands first and the liveness guard below would otherwise drop
  // the registry entirely. Applying it to a stale snapshot is always safe.
  const registered = snapshot.chatThreads.reduce(
    (current, thread) =>
      upsertChatThread(current, {
        id: thread.threadId,
        title: thread.title,
        driver: thread.driver,
        provider: thread.provider,
        model: thread.model,
      }),
    state,
  );
  if (snapshot.sequence < registered.sequence) return registered;
  // Boot issues the snapshot query and the subscription concurrently, so a
  // snapshot can arrive after the events it was taken alongside. Once the fold
  // has seen the run end, a snapshot no newer than the fold cannot un-end it;
  // a genuinely newer one (a resumed run) still applies.
  if (hasRunEnded(registered) && snapshot.sequence <= registered.sequence) return registered;
  const next = cloneCoreStateWith(registered, {
    status: coreRunStatus(snapshot.status) ?? registered.status,
    agentKind: snapshot.agentKind ?? null,
    roundLabel: snapshot.roundLabel ?? null,
    activeExecutions: activeExecutionsFromCheckpoint(snapshot.activeExecutions),
  });
  return next;
}

export type ActiveExecutionCheckpoint = readonly WireActiveExecution[];

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
  return next;
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
 * it leaves whatever floor the state already had.
 */
export function reduceEventBatch(
  state: CoreState,
  events: readonly RunEvent[],
  activeExecutions?: ActiveExecutionCheckpoint,
  throughSequence?: number,
  historyAfterSequence?: number,
): CoreState {
  const folder = new TranscriptFolder();
  let folded = state;
  for (const event of events) folded = foldEvent(folded, event, folder);
  const committed = folder.commit(folded);
  const reduced =
    historyAfterSequence === undefined
      ? committed
      : cloneCoreStateWith(committed, {historyAfterSequence});
  return activeExecutions === undefined
    ? reduced
    : reconcileActiveExecutions(reduced, activeExecutions, throughSequence);
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
 * The batch therefore rebuilds the core state rather than extending it. Only
 * the chat-thread registry survives, because a concurrent snapshot query
 * supplies threads that no replayed tail carries.
 */
export function reduceEventRebootstrap(
  state: CoreState,
  events: readonly RunEvent[],
  activeExecutions: ActiveExecutionCheckpoint | undefined,
  throughSequence: number | undefined,
  historyAfterSequence: number,
): CoreState {
  const base: CoreState = {...initialCoreState(), chatThreads: state.chatThreads};
  return reduceEventBatch(base, events, activeExecutions, throughSequence, historyAfterSequence);
}

/**
 * Folds a chunk of events strictly older than everything `state` has folded,
 * i.e. every `event.sequence <= state.historyAfterSequence`.
 *
 * The chunk folds into a fresh state rather than onto `state`: `foldEvent` drops
 * events the cursor already covers, so folding backwards onto the live state
 * would be a no-op, and a fresh fold keeps the work proportional to the chunk.
 * The two states then merge with prefix semantics, the newer one winning
 * wherever a field is last-write-wins. `historyAfterSequence` is the new floor.
 */
export function reduceEventPrefix(
  state: CoreState,
  events: readonly RunEvent[],
  historyAfterSequence: number,
): CoreState {
  const sequences = new Set(events.map(event => event.sequence));
  const boundaries = state.runLifetimeBoundaries.filter(
    boundary => !sequences.has(boundary.event.sequence),
  );
  // Fold ordinary prefix data once. Run-level spine events may carry terminal
  // transcript and diagnostic facts that the tail already has, so replaying
  // them through this fold would duplicate those facts during the merge.
  const olderData = reduceEventBatch(initialCoreState(), events);
  // Rebuild just the run-map projection with every retained lifetime boundary.
  // This keeps a narrow older chunk in the same run configuration as a full
  // replay, and re-applies terminal/resume boundaries without duplicating
  // their non-map projections.
  const runMapReplay = reduceEventBatch(
    {...initialCoreState(), runLifetimeBoundaries: state.runLifetimeBoundaries},
    [...events, ...boundaries.map(boundary => boundary.event)].sort(
      (left, right) => left.sequence - right.sequence,
    ),
  );
  const older = cloneCoreStateWith(olderData, {
    outerLoop: runMapReplay.outerLoop,
    expectedRoles: runMapReplay.expectedRoles,
    lastEventTimestamp: runMapReplay.lastEventTimestamp,
    lastRunMapSequence: runMapReplay.lastRunMapSequence,
    runLifetimeBoundaries: mergeRunLifetimeBoundaries(
      runMapReplay.runLifetimeBoundaries,
      state.runLifetimeBoundaries,
    ),
  });
  adoptRunMapArrays(older, runMapReplay);
  const chatTranscripts = mergeChatTranscriptsPrefix(older.chatTranscripts, state.chatTranscripts);
  const merged: CoreState = {
    sequence: state.sequence,
    // The newer events own run termination.
    status: state.status,
    agentKind: state.agentKind ?? older.agentKind,
    roundLabel: state.roundLabel ?? older.roundLabel,
    outerLoop: state.outerLoop ?? older.outerLoop,
    expectedRoles: state.expectedRoles ?? older.expectedRoles,
    maxRounds: state.maxRounds ?? older.maxRounds,
    rounds: mergeRoundLists(older.rounds, state.rounds),
    phases: mergePhaseLists(older.phases, state.phases),
    // The newer batch folded the newer events, so it saw the run more recently.
    lastEventTimestamp: state.lastEventTimestamp ?? older.lastEventTimestamp,
    lastRunMapSequence:
      state.lastRunMapSequence > 0 ? state.lastRunMapSequence : older.lastRunMapSequence,
    runLifetimeBoundaries: mergeRunLifetimeBoundaries(
      older.runLifetimeBoundaries,
      state.runLifetimeBoundaries,
    ),
    // Liveness comes from the backend checkpoint, never from replayed history.
    activeExecutions: state.activeExecutions,
    executionStatuses: mergeExecutionStatusesPrefix(
      older.executionStatuses,
      state.executionStatuses,
      state.activeExecutions,
    ),
    transcript: mergeTranscriptPrefix(older.transcript, state.transcript),
    chatTranscripts,
    chatTranscript: chatTranscripts[DEFAULT_CHAT_THREAD_ID] ?? [],
    chatThreads: mergeChatThreadsPrefix(older.chatThreads, state.chatThreads),
    todos: mergeTodosPrefix(older.todos, state.todos),
    usage: mergeExecutionStatusUsagePrefix(
      state.usage ?? older.usage,
      mergeExecutionStatusesPrefix(
        older.executionStatuses,
        state.executionStatuses,
        state.activeExecutions,
      ),
      older.executionStatuses,
      events,
      older.usage,
    ),
    // Sorted rather than concatenated for the same reason the transcript is
    // merged: a tail batch can carry events from below its own floor.
    benchmarks: [...older.benchmarks, ...state.benchmarks].sort(
      (left, right) => left.sequence - right.sequence,
    ),
    diagnostics: state.diagnostics.reduce(upsertDiagnostic, older.diagnostics),
    experimentsRevision: Math.max(older.experimentsRevision, state.experimentsRevision),
    typedToolEvents: older.typedToolEvents || state.typedToolEvents,
    chatTypedToolEvents: mergeTypedToolFlags(older.chatTypedToolEvents, state.chatTypedToolEvents),
    historyAfterSequence,
  };
  indexRunMapArrays(merged);
  return merged;
}

/**
 * Folds two transcripts into the one a full replay of both their event streams
 * would have built.
 *
 * A plain concatenation is wrong twice over. The fold merges entries (streamed
 * text concatenates, a tool result lands on its open call) and those merges
 * straddle the chunk boundary. And `newer` is not entirely newer: a tail
 * subscription's batch also carries the run-level spine from below its floor,
 * so a backfilled chunk interleaves with what the state already holds rather
 * than sitting wholly before it.
 *
 * So the two sequence-ordered lists are merged in sequence order and each entry
 * re-folded through `foldTranscriptEntry`, with terminal chat answers taking the
 * `foldChatAnswer` step instead so an answer folds over the streamed turn it
 * closes even when the two straddle the floor. That is exact: entries are already
 * maximally merged within each list and the step is idempotent over an
 * already merged entry, so re-folding in replay order reproduces replay.
 *
 * O(older + newer) with an O(1) step per entry. Re-folding only the entries near
 * the boundary would be faster by a constant, but no bounded window is provably
 * enough, so every entry is re-folded.
 *
 * Known boundaries, neither worth machinery:
 * - `capTranscript` evicts the oldest round once a transcript passes
 *   MAX_TRANSCRIPT_ENTRIES, so a transcript that grew past the cap by replay and
 *   one that grew past it by backfill are not required to agree.
 * - Two typed `tool_result` events carrying the same `call_id`, which only a
 *   malformed producer emits, diverge.
 */

function mergeTranscriptPrefix(
  older: readonly TranscriptEntry[],
  newer: readonly TranscriptEntry[],
): TranscriptEntry[] {
  const replay = new TranscriptPrefixReplay();
  const ordered = new ReplayOrderedTranscriptEntries(older, newer);
  for (let entry = ordered.next(); entry !== undefined; entry = ordered.next()) {
    replay.append(entry);
  }
  return replay.entries;
}

/**
 * Aligns two already ordered transcript projections into replay order.
 *
 * Entries with the same sequence retain the older projection first. That is
 * the original event order at the prefix boundary and lets the newer entry
 * update or extend it through the normal transcript fold.
 */
class ReplayOrderedTranscriptEntries {
  readonly #older: readonly TranscriptEntry[];
  readonly #newer: readonly TranscriptEntry[];
  #olderAt = 0;
  #newerAt = 0;

  constructor(older: readonly TranscriptEntry[], newer: readonly TranscriptEntry[]) {
    this.#older = older;
    this.#newer = newer;
  }

  next(): TranscriptEntry | undefined {
    if (this.#olderAt >= this.#older.length) return this.#takeNewer();
    if (this.#newerAt >= this.#newer.length) return this.#takeOlder();
    if (entryOrder(this.#newer[this.#newerAt]) < entryOrder(this.#older[this.#olderAt])) {
      return this.#takeNewer();
    }
    return this.#takeOlder();
  }

  #takeOlder(): TranscriptEntry | undefined {
    const entry = this.#older[this.#olderAt];
    this.#olderAt += 1;
    return entry;
  }

  #takeNewer(): TranscriptEntry | undefined {
    const entry = this.#newer[this.#newerAt];
    this.#newerAt += 1;
    return entry;
  }
}

/** Applies ordinary transcript folding to entries selected for prefix replay. */
class TranscriptPrefixReplay {
  readonly entries: TranscriptEntry[] = [];
  readonly #openTools = new OpenToolCallIndex();

  append(entry: TranscriptEntry): void {
    // A terminal chat answer carries no turn id and, in replay, folds over its
    // own still-open streamed turn through `foldChatAnswer` (which matches the
    // answer's invocation id, so an abandoned turn's stream is never claimed).
    // When the turn's chunks sit below the history floor and the answer above
    // it, the two arrive from opposite lists, so reconcile them here as replay
    // would; a second entry would otherwise survive. Anything else takes the
    // normal step.
    if (isTerminalChatAnswer(entry) && foldChatAnswer(this.entries, entry)) return;
    foldTranscriptEntry(this.entries, entry, this.#openTools);
  }
}

/** Whether `entry` is eligible to close a streamed chat turn during replay. */
function isTerminalChatAnswer(entry: TranscriptEntry): boolean {
  return entry.kind === 'assistant' && entry.turnId === undefined;
}

/**
 * Replay position of an entry, from the sequence its id was built from.
 *
 * An entry recorded from an event with no sequence has a non-numeric id. It
 * cannot be placed against the other list, so it sorts last within its own,
 * which keeps it after the entries it followed there.
 */
function entryOrder(entry: TranscriptEntry | undefined): number {
  if (entry === undefined) return Number.POSITIVE_INFINITY;
  const sequence = Number(entry.id);
  return Number.isFinite(sequence) ? sequence : Number.POSITIVE_INFINITY;
}

function mergeChatTranscriptsPrefix(
  older: Record<string, TranscriptEntry[]>,
  newer: Record<string, TranscriptEntry[]>,
): Record<string, TranscriptEntry[]> {
  const merged: Record<string, TranscriptEntry[]> = {};
  for (const [threadId, entries] of Object.entries(older)) {
    merged[threadId] = mergeTranscriptPrefix(entries, newer[threadId] ?? []);
  }
  for (const [threadId, entries] of Object.entries(newer)) {
    if (merged[threadId] === undefined) merged[threadId] = [...entries];
  }
  return merged;
}

/**
 * Keeps `older`'s replay order, then upserts `newer`'s threads on top.
 *
 * The implicit default thread heads both lists, so it stays first and is never
 * duplicated. A newer record that only names a thread (a titled turn whose
 * `chat_thread_created` fell in the chunk) must not erase the agent selection
 * the chunk carried, hence `??` rather than a plain overwrite.
 */
function mergeChatThreadsPrefix(
  older: readonly ChatThread[],
  newer: readonly ChatThread[],
): ChatThread[] {
  const merged = [...older];
  for (const thread of newer) {
    const at = merged.findIndex(candidate => candidate.id === thread.id);
    const existing = merged[at];
    if (existing === undefined) {
      merged.push(thread);
      continue;
    }
    merged[at] = {
      id: thread.id,
      title: thread.title || existing.title,
      driver: thread.driver ?? existing.driver,
      provider: thread.provider ?? existing.provider,
      model: thread.model ?? existing.model,
    };
  }
  return merged;
}

function mergeTodosPrefix(
  older: readonly ExecutionTodos[],
  newer: readonly ExecutionTodos[],
): ExecutionTodos[] {
  const retained = older.filter(item => !newer.some(incoming => sameTodoTarget(item, incoming)));
  return [...retained, ...newer].slice(-100);
}

/** The identity `updateTodos` replaces on: execution id, else role and round. */
function sameTodoTarget(candidate: ExecutionTodos, incoming: ExecutionTodos): boolean {
  if (incoming.executionId != null) return candidate.executionId === incoming.executionId;
  return (
    candidate.executionId == null &&
    candidate.agentKind === incoming.agentKind &&
    candidate.roundNumber === incoming.roundNumber
  );
}

function mergeTypedToolFlags(
  older: Record<string, boolean>,
  newer: Record<string, boolean>,
): Record<string, boolean> {
  const merged = {...older};
  for (const [threadId, seen] of Object.entries(newer)) {
    merged[threadId] = merged[threadId] === true || seen;
  }
  return merged;
}

/** Retain each boundary and its closest known predecessor. */
function mergeRunLifetimeBoundaries(
  older: readonly RunLifetimeBoundary[],
  newer: readonly RunLifetimeBoundary[],
): readonly RunLifetimeBoundary[] {
  const merged = new Map<number, RunLifetimeBoundary>();
  for (const boundary of [...older, ...newer]) {
    const sequence = boundary.event.sequence;
    const existing = merged.get(sequence);
    if (
      existing === undefined ||
      (boundary.closeout?.sequence ?? -1) > (existing.closeout?.sequence ?? -1)
    ) {
      merged.set(sequence, boundary);
    }
  }
  return [...merged.values()].sort((left, right) => left.event.sequence - right.event.sequence);
}

function boundaryFor(
  boundaries: readonly RunLifetimeBoundary[],
  event: RunEvent,
  state: CoreState,
): RunLifetimeBoundary {
  const sequence = event.sequence;
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
    event,
    closeout:
      observed !== null && observed.sequence > (known?.closeout?.sequence ?? -1)
        ? observed
        : (known?.closeout ?? null),
  };
}

export function reduceEvent(state: CoreState, event: RunEvent): CoreState {
  return foldEvent(state, event, null);
}

function foldEvent(state: CoreState, event: RunEvent, folder: TranscriptFolder | null): CoreState {
  const sequence = event.sequence;
  if (sequence > 0 && sequence <= state.sequence) return state;
  let next = cloneCoreState(state);
  next.sequence = Math.max(state.sequence, sequence);
  next = applyDiagnosticEvent(next, event);
  next = applyAgentExecutionEvent(next, event);
  next = applyAgentStatusEvent(next, event);
  if (event.agentKind === 'chat') return applyChatEvent(next, event, folder);
  if (event.agentKind) next.agentKind = event.agentKind;
  if (event.roundLabel) next.roundLabel = event.roundLabel;
  next = applyRunMapProjection(next, state, event, sequence);
  next = applyRunFacts(next, event, sequence);
  next = applyRunTranscript(next, event, folder);
  return applyRunLifecycle(next, event);
}

function applyRunMapProjection(
  next: CoreState,
  previous: CoreState,
  event: RunEvent,
  sequence: number,
): CoreState {
  const boundary =
    event.type === EventType.RUN_STARTED
      ? boundaryFor(previous.runLifetimeBoundaries, event, previous)
      : isRunLifetimeBoundary(event)
        ? {event, closeout: null}
        : null;
  const runMap = applyRunMapEvent(next, event, boundary?.closeout?.timestamp ?? null);
  next.outerLoop = runMap.outerLoop;
  next.expectedRoles = runMap.expectedRoles;
  adoptRunMapArrays(next, runMap);
  next.lastEventTimestamp = runMap.lastEventTimestamp;
  next.lastRunMapSequence = sequence;
  if (boundary !== null) {
    next.runLifetimeBoundaries = mergeRunLifetimeBoundaries(next.runLifetimeBoundaries, [boundary]);
  }
  return next;
}

function applyRunFacts(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const data = event.data;
  switch (data.case) {
    case 'toolCall':
    case 'toolResult':
      state.typedToolEvents = true;
      break;
    case 'todoUpdate':
      state.todos = updateTodos(state.todos, event);
      break;
    case 'usageUpdate':
      state.usage = {
        inputTokens: data.value.inputTokens,
        contextWindow: data.value.contextWindow ?? null,
        model: data.value.model ?? null,
      };
      break;
    case 'experimentsChanged':
      state.experimentsRevision = sequence;
      break;
    default:
      break;
  }
  const benchmark = benchmarkFromEvent(event, sequence);
  if (benchmark !== null) state.benchmarks = [...state.benchmarks, benchmark];
  // The backend owns the run's lifecycle and publishes every move through it,
  // so the projection folds the status it is told rather than inferring one.
  if (data.case === 'runStatusChanged') {
    const status = coreRunStatus(data.value.status);
    return status === undefined ? state : applyRunStatus(state, status);
  }
  return state;
}

function benchmarkFromEvent(event: RunEvent, sequence: number): BenchmarkRecord | null {
  const data = event.data;
  if (data.case === 'benchmarkResult') {
    return {
      sequence,
      roundNumber: roundNumberFromLabel(event.roundLabel),
      metric: data.value.metric,
      value: data.value.value,
      unit: data.value.unit,
    };
  }
  // A completed benchmark gate carries the measurement `benchmark_result`
  // used to, so it feeds the same fold; old journals have only the legacy
  // kind and new journals only this one (#692).
  if (
    data.case !== 'gateFinished' ||
    data.value.gate !== GateKind.BENCHMARK ||
    event.status === EventStatus.FAILED ||
    data.value.metric === undefined ||
    data.value.value === undefined
  ) {
    return null;
  }
  return {
    sequence,
    roundNumber: roundNumberFromLabel(event.roundLabel),
    metric: data.value.metric,
    value: data.value.value,
    unit: data.value.unit ?? data.value.metric,
  };
}

function applyRunTranscript(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const legacyToolChunk =
    data.case === 'agentOutputChunk' &&
    data.value.channel === AgentOutputChannel.TOOL &&
    state.typedToolEvents;
  if (legacyToolChunk) return state;
  const entry = eventToTranscriptEntry(event);
  if (entry === null) return state;
  if (folder === null) state.transcript = appendTranscript(state.transcript, entry);
  else folder.buffer(RUN_TRANSCRIPT, state.transcript).append(entry);
  return state;
}

function applyRunLifecycle(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (event.type === EventType.RUN_STARTED) {
    state.status = 'running';
    if (data.case === 'runStarted') state.maxRounds = data.value.maxRounds;
  }
  if (event.type === EventType.CONFIGURATION_FAILED) return terminate(state, 'failed');
  if (event.type === EventType.RUN_FINISHED) return terminate(state, 'completed');
  if (event.type === EventType.RUN_FAILED || event.type === EventType.RUN_INTERRUPTED) {
    return terminate(state, 'failed');
  }
  return state;
}

function cloneCoreState(state: CoreState): CoreState {
  return Object.create(
    Object.getPrototypeOf(state),
    Object.getOwnPropertyDescriptors(state),
  ) as CoreState;
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

/** Folds one backend-published status, ending the run when that status has. */
function applyRunStatus(state: CoreState, status: CoreRunStatus): CoreState {
  const ended = endedRunStatus(status);
  return ended === null ? cloneCoreStateWith(state, {status}) : terminate(state, ended);
}

function terminate(state: CoreState, status: EndedRunStatus): CoreState {
  return cloneCoreStateWith(state, {status, activeExecutions: {}});
}

function activeExecutionsFromCheckpoint(
  executions: ActiveExecutionCheckpoint,
): Record<string, ActiveAgentExecution> {
  return Object.fromEntries(
    executions.map(execution => [
      execution.executionId,
      {
        executionId: execution.executionId,
        agentKind: execution.agentKind,
        roundLabel: execution.roundLabel || null,
        roundNumber: roundNumberFromLabel(execution.roundLabel),
        stage: execution.stage,
        attempt: execution.attempt ?? null,
        assignment: execution.assignment,
        startedAt: timestampToIso(execution.startedAt),
        activity: activityFrom(execution.activity),
        driver: execution.driver ?? null,
        provider: execution.provider ?? null,
        model: execution.model ?? null,
      },
    ]),
  );
}

function applyAgentExecutionEvent(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.executionId;
  const data = event.data;
  if (executionId === undefined) return state;
  if (data.case === 'agentExecutionStarted') {
    return cloneCoreStateWith(state, {
      activeExecutions: {
        ...state.activeExecutions,
        [executionId]: {
          executionId,
          agentKind: event.agentKind ?? 'agent',
          roundLabel: event.roundLabel ?? null,
          roundNumber: roundNumberFromLabel(event.roundLabel),
          stage: data.value.stage,
          attempt: data.value.attempt ?? null,
          assignment: data.value.userPrompt,
          startedAt: timestampToIso(event.timestamp),
          activity: activityFrom(data.value.activity),
          driver: data.value.driver ?? null,
          provider: data.value.provider ?? null,
          model: data.value.model ?? null,
        },
      },
    });
  }
  if (data.case === 'agentExecutionActivityChanged') {
    const current = state.activeExecutions[executionId];
    if (current === undefined) return state;
    return cloneCoreStateWith(state, {
      activeExecutions: {
        ...state.activeExecutions,
        [executionId]: {...current, activity: activityFrom(data.value)},
      },
    });
  }
  if (data.case === 'agentExecutionFinished') {
    const {[executionId]: _finished, ...remaining} = state.activeExecutions;
    return cloneCoreStateWith(state, {activeExecutions: remaining});
  }
  return state;
}

function applyAgentStatusEvent(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  const executionId = event.executionId;
  let executionStatuses =
    Object.keys(state.activeExecutions).length === 0
      ? state.executionStatuses
      : reconcileExecutionStatuses(state.executionStatuses, state.activeExecutions);
  if (data.case === 'agentExecutionStarted') {
    executionStatuses = reconcileExecutionStatuses(executionStatuses, state.activeExecutions);
  } else if (data.case === 'agentExecutionFinished' && executionId !== undefined) {
    executionStatuses = removeExecutionStatus(executionStatuses, executionId);
  } else if (
    event.type === EventType.RUN_FINISHED ||
    event.type === EventType.RUN_FAILED ||
    event.type === EventType.RUN_INTERRUPTED
  ) {
    executionStatuses = {};
  } else {
    executionStatuses = applyExecutionStatus(executionStatuses, event);
  }
  const usage = applyExecutionStatusUsage(
    state.usage,
    state.executionStatuses,
    executionStatuses,
    state.activeExecutions,
    event,
  );
  if (executionStatuses === state.executionStatuses && usage === state.usage) return state;
  return cloneCoreStateWith(state, {executionStatuses, usage});
}

function updateTodos(previous: ExecutionTodos[], event: RunEvent): ExecutionTodos[] {
  const data = event.data;
  if (data.case !== 'todoUpdate') return previous;
  const agentKind = event.agentKind ?? null;
  const roundNumber = roundNumberFromLabel(event.roundLabel);
  const executionId = event.executionId ?? null;
  const retained = previous.filter(item =>
    executionId === null
      ? item.executionId != null || item.agentKind !== agentKind || item.roundNumber !== roundNumber
      : item.executionId !== executionId,
  );
  return [
    ...retained,
    {
      executionId,
      agentKind,
      roundNumber,
      items: data.value.todos.map(todo => ({content: todo.content, status: todo.status})),
    },
  ].slice(-100);
}

function applyChatEvent(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const threadId = event.chatThreadId ?? DEFAULT_CHAT_THREAD_ID;
  if (data.case === 'chatThreadCreated') {
    return upsertChatThread(state, {
      id: data.value.threadId,
      title: data.value.title,
      driver: data.value.driver,
      provider: data.value.provider,
      model: data.value.model,
    });
  }
  let next = state;
  const typed = data.case === 'toolCall' || data.case === 'toolResult';
  if (typed && next.chatTypedToolEvents[threadId] !== true) {
    next = cloneCoreStateWith(next, {
      chatTypedToolEvents: {...next.chatTypedToolEvents, [threadId]: true},
    });
  }
  const legacyToolChunk =
    data.case === 'agentOutputChunk' &&
    data.value.channel === AgentOutputChannel.TOOL &&
    next.chatTypedToolEvents[threadId] === true;
  if (legacyToolChunk) return next;
  if (data.case === 'chat' && data.value.threadTitle) {
    next = setChatThreadTitle(next, threadId, data.value.threadTitle);
  }
  const entry = eventToTranscriptEntry(event);
  if (entry === null || (entry.kind !== 'assistant' && entry.kind !== 'result')) return next;
  return appendChatTranscript(next, threadId, entry, folder, data.case === 'chat');
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
      upsertChatThread(state, {id: threadId, title: '', driver: null, provider: null, model: null}),
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
  if (diagnostic !== undefined) return fromProtocolDiagnostic(event, diagnostic);
  const fallback = fallbackDiagnosticFromEvent(event);
  return fallback === null
    ? null
    : fallbackDiagnostic(event, fallback.summary, fallback.scope, fallback.severity, fallback.code);
}

interface FallbackDiagnostic {
  summary: string;
  scope: DiagnosticScopeName;
  severity: CoreDiagnostic['severity'];
  code?: string | null;
}

/** Classifies legacy failure envelopes that predate structured diagnostics. */
function fallbackDiagnosticFromEvent(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (data.case === 'configurationFailed') {
    return {
      summary: configurationFailureContent(data.value),
      scope: 'configuration',
      severity: 'fatal',
      code: data.value.code,
    };
  }
  const invocation = invocationFallbackDiagnostic(event);
  if (invocation !== null) return invocation;
  return runFallbackDiagnostic(event);
}

function invocationFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (
    data.case !== 'invocationFinished' ||
    (data.value.error === undefined && event.status !== EventStatus.FAILED)
  ) {
    return null;
  }
  return {
    summary: data.value.error || event.text || 'Agent invocation failed.',
    scope: 'invocation',
    severity: 'error',
  };
}

function runFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  if (event.type !== EventType.RUN_FAILED && event.type !== EventType.RUN_INTERRUPTED) return null;
  return {
    summary:
      event.text ||
      interruptionText(event) ||
      (event.type === EventType.RUN_FAILED ? 'Run failed.' : 'Run interrupted.'),
    scope: 'run',
    severity: 'fatal',
  };
}

function interruptionText(event: RunEvent): string {
  const data = event.data;
  if (data.case !== 'runInterrupted') return '';
  return `${data.value.reason}${data.value.signal === undefined ? '' : ` (${data.value.signal})`}`;
}

function fromProtocolDiagnostic(event: RunEvent, diagnostic: Diagnostic): CoreDiagnostic {
  const scope = scopeName(diagnostic.scope);
  return {
    id: diagnostic.id || null,
    code: diagnostic.code || null,
    failureKind: failureKind(scope, event.type),
    summary: diagnostic.summary,
    detail: diagnostic.detail ?? null,
    hint: diagnostic.hint ?? null,
    severity: severityName(diagnostic.severity),
    scope,
    source: diagnostic.source ?? null,
    agentKind: event.agentKind ?? null,
    roundLabel: event.roundLabel ?? null,
    invocationId: event.executionId ?? null,
    sequence: event.sequence,
  };
}

function fallbackDiagnostic(
  event: RunEvent,
  summary: string,
  scope: DiagnosticScopeName,
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
    agentKind: event.agentKind ?? null,
    roundLabel: event.roundLabel ?? null,
    invocationId: event.executionId ?? null,
    sequence: event.sequence,
  };
}

function failureKind(
  scope: DiagnosticScopeName,
  eventType: EventType,
): CoreDiagnostic['failureKind'] {
  return eventType === EventType.RUN_INTERRUPTED ? 'run_interruption' : scope;
}

function eventToTranscriptEntry(event: RunEvent): TranscriptEntry | null {
  const fields = transcriptFields(event);
  const dataEntry = eventDataToTranscriptEntry(event, fields);
  return dataEntry === undefined ? eventTypeToTranscriptEntry(event, fields) : dataEntry;
}

interface TranscriptFields {
  id: string;
  agentFields: {agentKind?: string};
  roundFields: {roundLabel?: string; roundNumber?: number};
}

function transcriptFields(event: RunEvent): TranscriptFields {
  const roundNumber = roundNumberFromLabel(event.roundLabel);
  return {
    id: String(event.sequence),
    agentFields: event.agentKind ? {agentKind: event.agentKind} : {},
    roundFields: {
      ...(event.roundLabel ? {roundLabel: event.roundLabel} : {}),
      ...(roundNumber === null ? {} : {roundNumber}),
    },
  };
}

/** Projects typed payloads in wire order, preserving mixed-envelope precedence. */
function eventDataToTranscriptEntry(
  event: RunEvent,
  fields: TranscriptFields,
): TranscriptEntry | null | undefined {
  const data = event.data;
  switch (data.case) {
    case 'configurationFailed':
      return {
        id: fields.id,
        kind: 'result',
        content: configurationFailureContent(data.value),
        label: 'Configuration failed',
        tone: 'failure',
      };
    case 'chat':
      return chatTranscriptEntry(data.value, fields);
    case 'agentOutputChunk':
      return outputTranscriptEntry(event, data.value, fields);
    case 'toolCall':
      return toolCallTranscriptEntry(event, data.value, fields);
    case 'toolResult':
      return toolResultTranscriptEntry(event, data.value, fields);
    case 'subprocessOutput':
      return {
        id: fields.id,
        kind: 'subprocess',
        content: data.value.content,
        label: `${data.value.processKind} · ${enumWord(OutputStream, data.value.stream)}`,
        ...fields.agentFields,
        ...fields.roundFields,
      };
    // The warning rides the envelope's `diagnostic`, which `applyDiagnosticEvent`
    // has already folded into `diagnostics`; it is not transcript prose.
    case 'frameworkWarning':
      return null;
    case 'judgeResult':
      return judgeResultEntry(data.value, fields);
    case 'benchmarkResult':
      return {
        id: fields.id,
        kind: 'result',
        content: `${data.value.metric}: ${data.value.value} ${data.value.unit}`,
        label: 'Benchmark',
        tone: 'success',
        ...fields.agentFields,
        ...fields.roundFields,
      };
    case 'roundFinished':
      return roundFinishedEntry(event, data.value, fields);
    case 'gateStarted':
      return gateStartedEntry(event, data.value, fields);
    case 'gateFinished':
      return gateFinishedEntry(event, data.value, fields.id, fields.roundFields);
    case 'workspaceSnapshot':
      return {
        id: fields.id,
        kind: 'status',
        content: workspaceSnapshotContent(data.value),
        label: frameworkLabel(frameworkSourceName(data.value.source, undefined), event),
        ...fields.roundFields,
      };
    case 'runConfigured':
      return {
        id: fields.id,
        kind: 'status',
        content: runConfiguredContent(data.value),
        label: frameworkLabel(frameworkSourceName(data.value.source, undefined), event),
        ...fields.roundFields,
      };
    default:
      return undefined;
  }
}

function eventTypeToTranscriptEntry(
  event: RunEvent,
  fields: TranscriptFields,
): TranscriptEntry | null {
  if (event.type === EventType.PHASE_STARTED) {
    return {
      id: fields.id,
      kind: 'status',
      content: 'started',
      label: labelFor(event, 'phase'),
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  if (event.type === EventType.RUN_FAILED || event.type === EventType.RUN_INTERRUPTED) {
    const interrupted = event.type === EventType.RUN_INTERRUPTED;
    return {
      id: fields.id,
      kind: 'result',
      content:
        event.text || interruptionText(event) || (interrupted ? 'Run interrupted.' : 'Run failed.'),
      label: interrupted ? 'Run interrupted' : 'Run failed',
      tone: 'failure',
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  return null;
}

function chatTranscriptEntry(data: ChatData, fields: TranscriptFields): TranscriptEntry {
  // The invocation id names the turn this answer closes. Records written before
  // the field existed carry none.
  const invocationId = data.invocationId;
  return {
    id: fields.id,
    kind: 'assistant',
    content: data.answer,
    label: 'Answer',
    ...fields.agentFields,
    ...fields.roundFields,
    ...(invocationId === undefined ? {} : {invocationId}),
  };
}

function outputTranscriptEntry(
  event: RunEvent,
  data: AgentOutputChunkData,
  fields: TranscriptFields,
): TranscriptEntry {
  const kind = outputKind(data.channel);
  const invocationId = event.executionId;
  const gate = kind === 'diagnostic' ? splitFrameworkValidationCommand(data.content) : null;
  return {
    id: fields.id,
    kind,
    content: gate?.content ?? data.content,
    label: labelFor(event, enumWord(AgentOutputChannel, data.channel)),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.id,
    ...(invocationId === undefined ? {} : {invocationId}),
    ...(kind === 'tool' && data.content.trimStart().startsWith('→ ')
      ? {startsTurn: true, toolCall: data.content}
      : {}),
    ...(gate?.command === undefined ? {} : {command: gate.command}),
  };
}

function toolCallTranscriptEntry(
  event: RunEvent,
  data: ToolCallData,
  fields: TranscriptFields,
): TranscriptEntry {
  const invocationId = event.executionId;
  return {
    id: fields.id,
    kind: 'tool',
    content: '',
    label: labelFor(event, 'tool'),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.id,
    ...(invocationId === undefined ? {} : {invocationId}),
    startsTurn: true,
    toolName: data.tool,
    toolArguments: data.args ?? {},
    ...(data.callId === undefined ? {} : {toolCallId: data.callId}),
  };
}

function toolResultTranscriptEntry(
  event: RunEvent,
  data: ToolResultData,
  fields: TranscriptFields,
): TranscriptEntry {
  const invocationId = event.executionId;
  return {
    id: fields.id,
    kind: 'tool',
    content: data.content,
    label: labelFor(event, 'tool'),
    ...(data.isError ? {tone: 'failure' as const} : {}),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.id,
    toolName: data.tool,
    toolResult: data,
    ...(data.callId === undefined ? {} : {toolCallId: data.callId}),
    ...(invocationId === undefined ? {} : {invocationId}),
  };
}

function judgeResultEntry(
  data: {verdict: JudgeVerdict; feedback: string},
  fields: TranscriptFields,
): TranscriptEntry {
  const verdict = enumWord(JudgeVerdict, data.verdict);
  return {
    id: fields.id,
    kind: 'result',
    content: data.feedback || `Judge returned ${verdict}.`,
    label: `Judge · ${verdict.toUpperCase()}`,
    tone: data.verdict === JudgeVerdict.PASS ? 'success' : 'failure',
    ...fields.agentFields,
    ...fields.roundFields,
  };
}

function roundFinishedEntry(
  event: RunEvent,
  data: RoundFinishedData,
  fields: TranscriptFields,
): TranscriptEntry {
  const tone =
    data.judgeVerdict === RoundJudgeVerdict.PASS
      ? 'success'
      : data.judgeVerdict === RoundJudgeVerdict.FAIL
        ? 'failure'
        : 'normal';
  return {
    id: fields.id,
    kind: 'result',
    content: `${data.attempts} attempt(s)`,
    label: `${event.roundLabel ?? 'Round'} · ${enumWord(RoundJudgeVerdict, data.judgeVerdict).toUpperCase()}`,
    tone,
    ...fields.agentFields,
    ...fields.roundFields,
  };
}

function gateStartedEntry(
  event: RunEvent,
  data: GateStartedData,
  fields: TranscriptFields,
): TranscriptEntry {
  const recipe = data.recipe === undefined ? '' : ` ${data.recipe}`;
  return {
    id: fields.id,
    kind: 'status',
    content: `running${recipe}`,
    label: frameworkLabel(`framework-${enumWord(GateKind, data.gate)}`, event),
    ...fields.roundFields,
    ...(data.command === undefined ? {} : {command: data.command}),
  };
}

function configurationFailureContent(data: {
  message: string;
  usage?: string | undefined;
  code: string;
  stage: string;
}): string {
  const sections = [data.message];
  if (data.usage) sections.push(data.usage);
  sections.push(`Code: ${data.code} · Stage: ${data.stage}`);
  return sections.join('\n\n');
}

/**
 * Legacy/recorded-prose adapter: a live backend on `main` now emits a typed
 * `gate_started` event whose `command` field `eventToTranscriptEntry` reads
 * directly (see above), but a run recorded before #697 (e.g. the dev harness
 * fixture `clients/tui/dev/fixtures/bad-cpp-round1.jsonl`, replayed byte for
 * byte as `agent_output_chunk`/diagnostic) still carries the gate command as
 * free text: loop.py's old `ctx.lprint(f"[framework-validation] running
 * {recipe.name}: {recipe.command}")`. This is the one place that text is
 * folded into an entry, so it is split here rather than let the TUI
 * word-wrap a shell command as prose.
 *
 * Deliberately narrow: only the exact "[framework-validation] running
 * <recipe>: " prefix qualifies, so ordinary diagnostic prose (a colon, the
 * word "running", a bracket tag with a different shape, such as the sibling
 * `[framework-validation] PASS` / `reused PASS: ...` lines) is never mistaken
 * for a command.
 */
const FRAMEWORK_VALIDATION_RUN = /^\[framework-validation\] running [^:\n]+: ([\s\S]*)$/;

function splitFrameworkValidationCommand(content: string): {content: string; command?: string} {
  const match = FRAMEWORK_VALIDATION_RUN.exec(content);
  if (match === null) return {content};
  const raw = match[1] ?? '';
  const command = raw.endsWith('\n') ? raw.slice(0, -1) : raw;
  if (command === '') return {content};
  return {content: content.slice(0, content.length - raw.length), command};
}

function outputKind(channel: AgentOutputChannel): TranscriptEntry['kind'] {
  switch (channel) {
    case AgentOutputChannel.ASSISTANT:
      return 'assistant';
    case AgentOutputChannel.PROMPT:
      return 'prompt';
    case AgentOutputChannel.ANALYSIS:
      return 'analysis';
    case AgentOutputChannel.TOOL:
      return 'tool';
    default:
      return 'diagnostic';
  }
}

function labelFor(event: RunEvent, fallback: string): string {
  const phase = event.agentKind ?? fallback;
  return event.roundLabel ? `${phase} · ${event.roundLabel}` : phase;
}

type RoundFields = Partial<Pick<TranscriptEntry, 'roundLabel' | 'roundNumber'>>;

/**
 * The transcript name of a framework subsystem. Exhaustive over the protocol's
 * closed source set, so a new subsystem is a compile error, with `source_label`
 * as the escape hatch the `other` member carries.
 */
function frameworkSourceName(
  source: FrameworkSource,
  sourceLabel: string | null | undefined,
): string {
  switch (source) {
    case FrameworkSource.GIT_TRACKING:
      return 'git-tracking';
    case FrameworkSource.GPU:
      return 'gpu';
    case FrameworkSource.SKYPILOT:
      return 'skypilot';
    case FrameworkSource.OTHER:
      return sourceLabel ?? 'framework';
    case FrameworkSource.GATES:
    case FrameworkSource.LOOP:
    case FrameworkSource.UNSPECIFIED:
      return 'framework';
    default: {
      const unhandled: never = source;
      return unhandled;
    }
  }
}

/** `labelFor`'s round suffix without its agent fallback: framework, not agent. */
function frameworkLabel(base: string, event: RunEvent): string {
  return event.roundLabel ? `${base} · ${event.roundLabel}` : base;
}

/**
 * A gate outcome; the envelope's `status` carries pass or fail. A completed
 * benchmark measurement keeps rendering as the Benchmark result card
 * `benchmark_result` produced, so the card survives that event's retirement.
 */
function gateFinishedEntry(
  event: RunEvent,
  data: GateFinishedData,
  id: string,
  roundFields: RoundFields,
): TranscriptEntry {
  const label = frameworkLabel(`framework-${enumWord(GateKind, data.gate)}`, event);
  if (event.status === EventStatus.FAILED) {
    const heading = data.recipe === undefined ? 'FAIL' : `FAIL: ${data.recipe}`;
    return {
      id,
      kind: 'diagnostic',
      content: data.outputTail ? `${heading}\n${data.outputTail}` : heading,
      label,
      tone: 'failure',
      ...roundFields,
    };
  }
  const measurement =
    data.metric !== undefined && data.value !== undefined
      ? `${data.metric}: ${data.value} ${data.unit ?? data.metric}`
      : null;
  if (data.gate === GateKind.BENCHMARK && measurement !== null && !data.reused) {
    return {
      id,
      kind: 'result',
      content: measurement,
      label: 'Benchmark',
      tone: 'success',
      ...roundFields,
    };
  }
  const passed = data.reused ? 'reused PASS' : 'PASS';
  const detail = data.recipe ?? measurement;
  return {
    id,
    kind: 'status',
    content: detail === null ? passed : `${passed}: ${detail}`,
    label,
    tone: 'success',
    ...roundFields,
  };
}

/** Exactly one aspect is populated per event; see `WorkspaceSnapshotData`. */
function workspaceSnapshotContent(data: WorkspaceSnapshotData): string {
  if (data.baseline !== undefined) return `trusted input baseline: ${shortCommit(data.baseline)}`;
  const excluded = data.excludedPaths;
  if (excluded.length > 0) {
    return `excluded ${excluded.length} path${excluded.length === 1 ? '' : 's'} from snapshots`;
  }
  if (data.commit === undefined) return `no changes to commit for '${data.label}'`;
  return `snapshot '${data.label}' at ${shortCommit(data.commit)}`;
}

function shortCommit(commit: string): string {
  return commit.slice(0, 7);
}

function runConfiguredContent(data: RunConfiguredData): string {
  const lines: string[] = [];
  if (data.objective) lines.push(`objective: ${data.objective}`);
  if (data.model) lines.push(`model: ${data.model}`);
  if (data.searchPolicy) lines.push(`search policy: ${data.searchPolicy}`);
  return lines.length > 0 ? lines.join('\n') : 'run configured';
}

/**
 * Appends one entry to a copy of `previous`, leaving `previous` untouched.
 *
 * Used by the single-event path, where the caller owns an immutable array. A
 * batch folds through `TranscriptBuffer` instead, which applies the same step
 * to one working array.
 */
function appendTranscript(
  previous: readonly TranscriptEntry[],
  incoming: TranscriptEntry,
): TranscriptEntry[] {
  const next = [...previous];
  foldTranscriptEntry(next, incoming, null);
  return next;
}

/**
 * The transcript fold step, applied in place to `entries`.
 *
 * `index` accelerates the open-tool-call lookup; passing null falls back to
 * scanning, which is what the single-event path does. Both must agree, so the
 * index reproduces `findToolCall`'s search order exactly.
 */
function foldTranscriptEntry(
  entries: TranscriptEntry[],
  incoming: TranscriptEntry,
  index: OpenToolCallIndex | null,
): void {
  if (incoming.kind === 'tool' && !incoming.startsTurn && incoming.toolName !== undefined) {
    const target =
      index === null ? findToolCall(entries, incoming) : index.match(entries, incoming);
    const call = entries[target];
    if (call !== undefined) {
      entries[target] = mergeToolResult(call, incoming);
      return;
    }
  }
  const last = entries.at(-1);
  if (
    last?.kind === 'tool' &&
    incoming.kind === 'tool' &&
    last.invocationId === incoming.invocationId &&
    !incoming.startsTurn &&
    // Gluing onto the last tool entry is the legacy tool-chunk rule, where a
    // response chunk has no way to name its call. A typed result names one, so
    // if the search above found nothing the call is outside the replay window
    // and the result stands alone. Without this, two results whose calls both
    // predate the window would collapse into a single entry.
    incoming.toolCallId === undefined
  ) {
    entries[entries.length - 1] = mergeToolResult(last, incoming);
    return;
  }
  if (
    last !== undefined &&
    last.kind === incoming.kind &&
    // Chunks of one turn share its id; an entry without a turn (a terminal
    // chat answer, or one already closed by `foldChatAnswer`) is complete and
    // must not glue onto a neighbor that is just as complete.
    last.turnId !== undefined &&
    last.turnId === incoming.turnId &&
    (incoming.kind === 'assistant' ||
      incoming.kind === 'prompt' ||
      incoming.kind === 'analysis' ||
      incoming.kind === 'diagnostic')
  ) {
    entries[entries.length - 1] = {...last, content: last.content + glue(last, incoming)};
    return;
  }
  entries.push(incoming);
  index?.record(incoming, entries.length - 1);
  capTranscript(entries, index);
}

/**
 * What joins `incoming` onto the entry it glues into.
 *
 * Assistant, prompt, and analysis chunks are mid-sentence fragments of a token
 * stream and must concatenate raw; inserting anything between them would break
 * words. Diagnostic chunks are whole lines a driver already terminated in
 * meaning but not in text (`[codex turn started]` carries no newline), so
 * concatenating them raw produced one squished blob per turn.
 */
function glue(last: TranscriptEntry, incoming: TranscriptEntry): string {
  const separator =
    incoming.kind === 'diagnostic' && last.content !== '' && !last.content.endsWith('\n')
      ? '\n'
      : '';
  return separator + incoming.content;
}

const MAX_TRANSCRIPT_ENTRIES = 20_000;

/** Evicts the oldest round in place once the transcript passes its cap. */
function capTranscript(entries: TranscriptEntry[], index: OpenToolCallIndex | null): void {
  if (entries.length <= MAX_TRANSCRIPT_ENTRIES) return;
  const oldestRound = entries.find(entry => entry.roundNumber !== undefined)?.roundNumber;
  const kept =
    oldestRound === undefined
      ? entries.length
      : retainInPlace(
          entries,
          entry => entry.roundNumber === undefined || entry.roundNumber > oldestRound,
        );
  if (kept === entries.length) entries.splice(0, entries.length - MAX_TRANSCRIPT_ENTRIES);
  else entries.length = kept;
  index?.reindex(entries);
}

/** Compacts the kept entries to the front and returns how many survived. */
function retainInPlace(
  entries: TranscriptEntry[],
  keep: (entry: TranscriptEntry) => boolean,
): number {
  let write = 0;
  for (const entry of entries) {
    if (!keep(entry)) continue;
    entries[write] = entry;
    write += 1;
  }
  return write;
}

function findToolCall(previous: readonly TranscriptEntry[], result: TranscriptEntry): number {
  const indices = Array.from(previous.keys());
  if (result.toolCallId !== undefined) indices.reverse();
  for (const index of indices) {
    const candidate = previous[index];
    if (
      candidate?.kind !== 'tool' ||
      (candidate.toolCall === undefined && candidate.toolArguments === undefined) ||
      candidate.toolResponse !== undefined ||
      candidate.toolResult !== undefined ||
      candidate.invocationId !== result.invocationId
    ) {
      continue;
    }
    if (result.toolCallId !== undefined) {
      if (candidate.toolCallId === result.toolCallId) return index;
    } else if (candidate.toolName === result.toolName) return index;
  }
  return -1;
}

/** A tool call still waiting for its result: what `findToolCall` accepts. */
function isOpenToolCall(entry: TranscriptEntry): boolean {
  return (
    entry.kind === 'tool' &&
    (entry.toolCall !== undefined || entry.toolArguments !== undefined) &&
    entry.toolResponse === undefined &&
    entry.toolResult === undefined
  );
}

// NUL appears in no invocation id, tool name, call id, or thread id, so it
// separates the halves of a key without any real value colliding.
const TOOL_KEY_SEPARATOR = '\u0000';

function toolKey(invocationId: string | undefined, discriminator: string): string {
  return `${invocationId ?? ''}${TOOL_KEY_SEPARATOR}${discriminator}`;
}

/**
 * Locates the tool call a result merges into without scanning the transcript.
 *
 * `findToolCall` scans by call id from the end (latest open call wins) and by
 * tool name from the start (earliest open call wins), so this keeps one bucket
 * per key holding candidate positions in ascending order and reads the matching
 * end. Positions of calls that have since been answered are dropped lazily: a
 * call never reopens, so a stale position can only ever be discarded.
 */
class OpenToolCallIndex {
  readonly #byCallId = new Map<string, number[]>();
  readonly #byName = new Map<string, number[]>();

  /** Records `entry` at position `at` if it is a call awaiting a result. */
  record(entry: TranscriptEntry, at: number): void {
    if (!isOpenToolCall(entry)) return;
    if (entry.toolCallId !== undefined) {
      bucket(this.#byCallId, toolKey(entry.invocationId, entry.toolCallId)).push(at);
    }
    if (entry.toolName !== undefined) {
      bucket(this.#byName, toolKey(entry.invocationId, entry.toolName)).push(at);
    }
  }

  /** Rebuilds every bucket after positions shift, i.e. after cap eviction. */
  reindex(entries: readonly TranscriptEntry[]): void {
    this.#byCallId.clear();
    this.#byName.clear();
    for (let at = 0; at < entries.length; at += 1) {
      const entry = entries[at];
      if (entry !== undefined) this.record(entry, at);
    }
  }

  /** The position `findToolCall` would return for `result`, or -1. */
  match(entries: readonly TranscriptEntry[], result: TranscriptEntry): number {
    if (result.toolCallId !== undefined) {
      const key = toolKey(result.invocationId, result.toolCallId);
      return this.#take(this.#byCallId, key, entries, 'last');
    }
    if (result.toolName === undefined) return -1;
    return this.#take(
      this.#byName,
      toolKey(result.invocationId, result.toolName),
      entries,
      'first',
    );
  }

  #take(
    buckets: Map<string, number[]>,
    key: string,
    entries: readonly TranscriptEntry[],
    end: 'first' | 'last',
  ): number {
    const positions = buckets.get(key);
    if (positions === undefined) return -1;
    while (positions.length > 0) {
      const at = (end === 'last' ? positions.at(-1) : positions[0]) as number;
      const candidate = entries[at];
      if (candidate !== undefined && isOpenToolCall(candidate)) return at;
      if (end === 'last') positions.pop();
      else positions.shift();
    }
    buckets.delete(key);
    return -1;
  }
}

function bucket(buckets: Map<string, number[]>, key: string): number[] {
  const existing = buckets.get(key);
  if (existing !== undefined) return existing;
  const created: number[] = [];
  buckets.set(key, created);
  return created;
}

/**
 * One transcript folded in place across a batch.
 *
 * The array is mutable only while the batch is folding; `entries` is handed to
 * the committed `CoreState` once, after which nothing writes to it again.
 */
class TranscriptBuffer {
  readonly entries: TranscriptEntry[];
  readonly #index = new OpenToolCallIndex();

  constructor(initial: readonly TranscriptEntry[]) {
    this.entries = [...initial];
    this.#index.reindex(this.entries);
  }

  append(incoming: TranscriptEntry): void {
    foldTranscriptEntry(this.entries, incoming, this.#index);
  }
}

/** Key for the run transcript, kept out of the chat thread id space. */
const RUN_TRANSCRIPT = `${TOOL_KEY_SEPARATOR}run`;

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

function mergeToolResult(call: TranscriptEntry, result: TranscriptEntry): TranscriptEntry {
  if (call.toolArguments !== undefined && result.toolResult !== undefined) {
    return {
      ...call,
      content: result.toolResult.content,
      toolResult: result.toolResult,
      ...(result.tone === undefined ? {} : {tone: result.tone}),
    };
  }
  const separator = call.content.endsWith('\n') || result.content.startsWith('\n') ? '' : '\n';
  return {
    ...call,
    content: call.content + separator + result.content,
    toolResponse: (call.toolResponse ?? '') + (call.toolResponse ? separator : '') + result.content,
    ...(result.tone === undefined ? {} : {tone: result.tone}),
  };
}
