import type {Diagnostic, RunEvent, RunSnapshot, RunStatus} from '@vibesys/backend-client';
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

export type AgentExecutionMode = 'thinking' | 'responding' | 'tool' | 'waiting';

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

type RunEventData = NonNullable<RunEvent['data']>;
export type TypedToolResult = Extract<RunEventData, {kind?: 'tool_result'}>;
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
  failureKind: Diagnostic['scope'] | 'run_interruption';
  summary: string;
  detail: string | null;
  hint: string | null;
  severity: 'warning' | 'error' | 'fatal';
  scope: Diagnostic['scope'];
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
    case 'interrupted':
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
  const registered = (snapshot.chat_threads ?? []).reduce(
    (current, thread) =>
      upsertChatThread(current, {
        id: thread.thread_id,
        title: thread.title ?? '',
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
    status: snapshot.status,
    agentKind: snapshot.agent_kind ?? null,
    roundLabel: snapshot.round_label ?? null,
    activeExecutions: activeExecutionsFromCheckpoint(snapshot.active_executions ?? []),
  });
  return next;
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
  const sequences = new Set(
    events.map(event => event.sequence).filter(sequence => sequence !== undefined),
  );
  const boundaries = state.runLifetimeBoundaries.filter(
    boundary => boundary.event.sequence !== undefined && !sequences.has(boundary.event.sequence),
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
      (left, right) => (left.sequence ?? 0) - (right.sequence ?? 0),
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
  if (sequence === undefined) return {event, closeout: null};
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
  const sequence = event.sequence ?? 0;
  if (sequence > 0 && sequence <= state.sequence) return state;
  let next = cloneCoreState(state);
  next.sequence = Math.max(state.sequence, sequence);
  next = applyDiagnosticEvent(next, event);
  next = applyAgentExecutionEvent(next, event);
  next = applyAgentStatusEvent(next, event);
  if (event.agent_kind === 'chat') return applyChatEvent(next, event, folder);
  if (event.agent_kind) next.agentKind = event.agent_kind;
  if (event.round_label) next.roundLabel = event.round_label;
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
    event.type === 'run_started' && event.sequence !== undefined
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
  if (data?.kind === 'tool_call' || data?.kind === 'tool_result') state.typedToolEvents = true;
  if (data?.kind === 'todo_update') state.todos = updateTodos(state.todos, event);
  if (data?.kind === 'usage_update') {
    state.usage = {
      inputTokens: data.input_tokens,
      contextWindow: data.context_window ?? null,
      model: data.model ?? null,
    };
  }
  const benchmark = benchmarkFromEvent(event, sequence);
  if (benchmark !== null) state.benchmarks = [...state.benchmarks, benchmark];
  if (data?.kind === 'experiments_changed') state.experimentsRevision = sequence;
  // The backend owns the run's lifecycle and publishes every move through it,
  // so the projection folds the status it is told rather than inferring one.
  if (data?.kind === 'run_status_changed') return applyRunStatus(state, data.status);
  return state;
}

function benchmarkFromEvent(event: RunEvent, sequence: number): BenchmarkRecord | null {
  const data = event.data;
  if (data?.kind === 'benchmark_result') {
    return {
      sequence,
      roundNumber: roundNumberFromLabel(event.round_label),
      metric: data.metric,
      value: data.value,
      unit: data.unit,
    };
  }
  // A completed benchmark gate carries the measurement `benchmark_result`
  // used to, so it feeds the same fold; old journals have only the legacy
  // kind and new journals only this one (#692).
  if (
    data?.kind !== 'gate_finished' ||
    data.gate !== 'benchmark' ||
    event.status === 'failed' ||
    data.metric == null ||
    data.value == null
  ) {
    return null;
  }
  return {
    sequence,
    roundNumber: roundNumberFromLabel(event.round_label),
    metric: data.metric,
    value: data.value,
    unit: data.unit ?? data.metric,
  };
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
  if (folder === null) state.transcript = appendTranscript(state.transcript, entry);
  else folder.buffer(RUN_TRANSCRIPT, state.transcript).append(entry);
  return state;
}

function applyRunLifecycle(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  if (event.type === 'run_started') {
    state.status = 'running';
    if (data?.kind === 'run_started') state.maxRounds = data.max_rounds;
  }
  if (event.type === 'configuration_failed') return terminate(state, 'failed');
  if (event.type === 'run_finished') return terminate(state, 'completed');
  if (event.type === 'run_failed') return terminate(state, 'failed');
  if (event.type === 'run_interrupted') return terminate(state, 'interrupted');
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
    executions.map(execution => [
      execution.execution_id,
      {
        executionId: execution.execution_id,
        agentKind: execution.agent_kind,
        roundLabel: execution.round_label ?? null,
        roundNumber: roundNumberFromLabel(execution.round_label),
        stage: execution.stage,
        attempt: execution.attempt ?? null,
        assignment: execution.assignment,
        startedAt: execution.started_at,
        activity: {
          mode: execution.activity.mode,
          summary: execution.activity.summary,
          tool: execution.activity.tool ?? null,
        },
        driver: execution.driver ?? null,
        provider: execution.provider ?? null,
        model: execution.model ?? null,
      },
    ]),
  );
}

function applyAgentExecutionEvent(state: CoreState, event: RunEvent): CoreState {
  const executionId = event.execution_id;
  const data = event.data;
  if (executionId == null) return state;
  if (data?.kind === 'agent_execution_started') {
    return cloneCoreStateWith(state, {
      activeExecutions: {
        ...state.activeExecutions,
        [executionId]: {
          executionId,
          agentKind: event.agent_kind ?? 'agent',
          roundLabel: event.round_label ?? null,
          roundNumber: roundNumberFromLabel(event.round_label),
          stage: data.stage,
          attempt: data.attempt ?? null,
          assignment: data.user_prompt ?? '',
          startedAt: event.timestamp,
          activity: {
            mode: data.activity.mode,
            summary: data.activity.summary,
            tool: data.activity.tool ?? null,
          },
          driver: data.driver ?? null,
          provider: data.provider ?? null,
          model: data.model ?? null,
        },
      },
    });
  }
  if (data?.kind === 'agent_execution_activity_changed') {
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
  if (data?.kind === 'agent_execution_finished') {
    const {[executionId]: _finished, ...remaining} = state.activeExecutions;
    return cloneCoreStateWith(state, {activeExecutions: remaining});
  }
  return state;
}

function applyAgentStatusEvent(state: CoreState, event: RunEvent): CoreState {
  const data = event.data;
  const executionId = event.execution_id;
  let executionStatuses =
    Object.keys(state.activeExecutions).length === 0
      ? state.executionStatuses
      : reconcileExecutionStatuses(state.executionStatuses, state.activeExecutions);
  if (data?.kind === 'agent_execution_started') {
    executionStatuses = reconcileExecutionStatuses(executionStatuses, state.activeExecutions);
  } else if (data?.kind === 'agent_execution_finished' && executionId != null) {
    executionStatuses = removeExecutionStatus(executionStatuses, executionId);
  } else if (
    event.type === 'run_finished' ||
    event.type === 'run_failed' ||
    event.type === 'run_interrupted'
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
  if (data?.kind !== 'todo_update') return previous;
  const agentKind = event.agent_kind ?? null;
  const roundNumber = roundNumberFromLabel(event.round_label);
  const executionId = event.execution_id ?? event.invocation_id ?? null;
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
      driver: data.driver,
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
  OpenToolCallIndex,
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
