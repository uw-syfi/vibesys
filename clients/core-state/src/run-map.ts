import type {RunEvent} from '@vibesys/backend-client';
import {ownProjectionInput, publishProjectionValue} from './publication.js';
import {
  type RoundKey,
  roundKeyFor,
  roundKeyToken,
  roundNumberFor,
  sameRoundKey,
} from './round-key.js';
import {
  activeTimingElapsedMs,
  closeActiveAgentTimings,
  finishAgentTiming,
  hasActiveAgentTiming,
  mergeAgentTimingPrefix,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';
import {
  appendRunMapArrayEntry,
  ensureRunMapArray,
  replaceRunMapArrayEntry,
  runMapArrayAt,
  runMapArrayFrom,
} from './run-map-array.js';

export type AgentPhaseStatus =
  | 'pending'
  | 'active'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted';
export type RoundStatus = 'active' | 'completed' | 'failed' | 'planned';

export interface RoundState extends RoundTimingState {
  readonly key: RoundKey;
  /** Numeric display and protocol join identity; null for label fallback rounds. */
  readonly number: number | null;
  readonly status: RoundStatus;
  readonly startedAt?: string;
  readonly finishedAt?: string;
  /** Internal provenance for a terminal endpoint inferred at a run boundary. */
  readonly closedByRunBoundary?: true;
  /** An explicit round_finished event may supersede a prior inferred endpoint. */
  readonly closedByRoundFinished?: true;
  /** The round completed without a fresh profile measurement. */
  readonly profileSkipped?: boolean;
}

export interface AgentPhase {
  readonly kind: string;
  readonly status: AgentPhaseStatus;
  readonly roundNumber: number | null;
  /** Stable grouping identity; null only for events without any round label. */
  readonly roundKey: RoundKey | null;
  readonly roundLabel: string | null;
  readonly executionId?: string;
  readonly invocationId?: string;
  readonly startedAt?: string;
  readonly finishedAt?: string;
  readonly driver?: string | null;
  readonly provider?: string | null;
  readonly model?: string | null;
}

/** Read-only run-map projection accepted from the published core state. */
export interface RunMapProjection {
  readonly outerLoop: string | null;
  /**
   * The agent roles the backend advertised in `run_started`; null on
   * recordings that predate the field, where `legacyExpectedRoles` applies.
   */
  readonly expectedRoles: readonly string[] | null;
  readonly rounds: readonly RoundState[];
  readonly phases: readonly AgentPhase[];
  /**
   * Timestamp of the newest event this state has folded, or null before the
   * first one.
   *
   * A run that is killed hard emits no terminal event, so the moment it stopped
   * is not recoverable from any later event: the next thing the journal carries
   * is the resumed process's `run_started`, minutes or hours afterwards.
   * Recording when the run was last seen alive is what lets the closeout stop
   * the clocks there rather than charging the downtime to the round.
   */
  readonly lastEventTimestamp: string | null;
}

/** Internal working shape. Its arrays are never published directly. */
export interface RunMapState extends RunMapProjection {
  readonly rounds: RoundState[];
  readonly phases: AgentPhase[];
}

interface PhaseSlot {
  readonly indices: readonly number[];
  readonly placeholderIndex: number | null;
  readonly activeIndices: readonly number[];
  readonly executions: ReadonlyMap<string, number>;
}

type PhaseRoleIndex = ReadonlyMap<string, PhaseSlot>;

interface PhaseIndex {
  readonly rounds: ReadonlyMap<string, PhaseRoleIndex>;
  readonly unscoped: PhaseRoleIndex;
}

const phaseIndexes = new WeakMap<AgentPhase[], PhaseIndex>();
const publishedArrays = new WeakMap<readonly unknown[], readonly unknown[]>();
const publishedInternals = new WeakMap<readonly unknown[], unknown[]>();
const RUN_MAP_INTERNAL = Symbol('runMapInternal');

interface RunMapInternal {
  readonly rounds: RoundState[];
  readonly phases: AgentPhase[];
}

type IndexedRunMapState = RunMapProjection & {[RUN_MAP_INTERNAL]?: RunMapInternal};

export function applyRunMapEvent(
  state: RunMapProjection,
  event: RunEvent,
  abandonedAt: string | null = null,
): RunMapState {
  return publishRunMapState(applyIndexedRunMapEvent(state, event, abandonedAt));
}

function applyIndexedRunMapEvent(
  state: RunMapProjection,
  event: RunEvent,
  abandonedAt: string | null,
): RunMapState {
  const internal = runMapInternal(state);
  const seen: RunMapState = {
    outerLoop: state.outerLoop,
    expectedRoles: state.expectedRoles,
    rounds: internal.rounds,
    phases: internal.phases,
    lastEventTimestamp: event.timestamp,
  };
  // Run-ending events say the run ended, not which agent ended it, so they
  // carry no `agent_kind` and no `round_label`. Every projection below is keyed
  // by that scope and would drop them, which is why the closeout runs first and
  // returns: one owner for "the run ended", sweeping the whole map rather than
  // the one round a label happened to name.
  const closing = runClosingStatus(event);
  if (closing !== null) {
    return closeOpenRunState(seen, closing, event.timestamp, event.sequence ?? null);
  }
  const base =
    event.type === 'run_started'
      ? closeAbandonedRunState(seen, abandonedAt ?? state.lastEventTimestamp ?? event.timestamp)
      : seen;
  const started =
    event.type === 'run_started' && event.data?.kind === 'run_started' ? event.data : null;
  const outerLoop = started === null ? base.outerLoop : started.outer_loop;
  const expectedRoles =
    started?.expected_roles !== undefined && started.expected_roles.length > 0
      ? ownProjectionInput(started.expected_roles)
      : base.expectedRoles;
  const rounds = applyRoundEvent(base.rounds, base.phases, event);
  const phases = applyPhaseEvent({...base, outerLoop, expectedRoles, rounds}, event);
  return {
    outerLoop,
    expectedRoles,
    rounds,
    phases,
    lastEventTimestamp: event.timestamp,
  };
}

/**
 * What an agent that was running becomes because `event` ended the run, or
 * null when this event is not where the map learns the run ended.
 *
 * `run_failed` and `run_interrupted` are the run-scoped terminal events. A
 * `stopped` status is the third way a run ends and the only one with nothing
 * after it: the controller lands an operator `/stop` at an invocation
 * boundary, records the status change, and the journal stops there (see
 * `server/controller.py`'s `land_stop_at_boundary`), so the status event is
 * where the map learns nothing will finish what is open. It reads as
 * `interrupted` for the same reason a signal does: an operator stopped the
 * run, the agent did not fail.
 *
 * The other ended statuses are deliberately absent. `completed` and `failed`
 * publish their status change immediately before their own terminal event
 * (`server/integration.py` settles the controller first, so the status change
 * orders ahead of it in the journal), and that event is the existing owner of
 * their closeout.
 */
function runClosingStatus(event: RunEvent): 'failed' | 'interrupted' | null {
  if (event.type === 'run_failed') return 'failed';
  if (event.type === 'run_interrupted') return 'interrupted';
  const data = event.data;
  return data?.kind === 'run_status_changed' && data.status === 'stopped' ? 'interrupted' : null;
}

/** Copies lazy run-map array publication from `source` onto a folded core state. */
export function adoptRunMapArrays(target: object, source: RunMapProjection): void {
  for (const property of ['rounds', 'phases'] as const) {
    const descriptor = Object.getOwnPropertyDescriptor(source, property);
    if (descriptor !== undefined) Object.defineProperty(target, property, descriptor);
  }
  const internal = (source as IndexedRunMapState)[RUN_MAP_INTERNAL];
  if (internal !== undefined) {
    Object.defineProperty(target, RUN_MAP_INTERNAL, {configurable: true, value: internal});
  }
}

/** Indexes arrays produced by a whole-history operation and publishes them lazily. */
export function indexRunMapArrays(state: RunMapProjection): void {
  // A whole-history merge can itself return an internal persistent array. Do
  // not publish that proxy: materialize one ordinary consumer array, just as
  // the incremental path does, then retain the persistent copy behind it.
  const publishedRounds = publishProjectionValue([...state.rounds]);
  const publishedPhases = publishProjectionValue([...state.phases]);
  const rounds = runMapArrayFrom(publishedRounds);
  const phases = runMapArrayFrom(publishedPhases);
  phaseIndexes.set(phases, buildPhaseIndex(phases));
  publishedArrays.set(rounds, publishedRounds);
  publishedArrays.set(phases, publishedPhases);
  publishedInternals.set(publishedRounds, rounds);
  publishedInternals.set(publishedPhases, phases);
  adoptRunMapArrays(
    state,
    publishRunMapState({
      outerLoop: state.outerLoop,
      expectedRoles: state.expectedRoles,
      rounds,
      phases,
      lastEventTimestamp: state.lastEventTimestamp,
    }),
  );
}

function runMapInternal(state: RunMapProjection): RunMapInternal {
  const existing = (state as IndexedRunMapState)[RUN_MAP_INTERNAL];
  if (existing !== undefined) return existing;
  const rounds = internalRunMapArray(state.rounds);
  const phases = internalRunMapArray(state.phases);
  if (!phaseIndexes.has(phases)) phaseIndexes.set(phases, buildPhaseIndex(phases));
  return {rounds, phases};
}

function internalRunMapArray<T>(published: readonly T[]): T[] {
  return (publishedInternals.get(published) as T[] | undefined) ?? runMapArrayFrom(published);
}

function publishRunMapState(state: RunMapState): RunMapState {
  let publishedRounds: RoundState[] | undefined;
  let publishedPhases: AgentPhase[] | undefined;
  const rounds = ensureRunMapArray(state.rounds);
  const phases = ensureRunMapArray(state.phases);
  if (!phaseIndexes.has(phases)) phaseIndexes.set(phases, buildPhaseIndex(phases));
  const internal: RunMapInternal = {rounds, phases};
  const published = {
    outerLoop: state.outerLoop,
    expectedRoles: state.expectedRoles,
    lastEventTimestamp: state.lastEventTimestamp,
  } as RunMapState;
  Object.defineProperties(published, {
    rounds: {
      configurable: true,
      enumerable: true,
      get: () => (publishedRounds ??= materializeRunMapArray(internal.rounds)),
    },
    phases: {
      configurable: true,
      enumerable: true,
      get: () => (publishedPhases ??= materializeRunMapArray(internal.phases)),
    },
    [RUN_MAP_INTERNAL]: {configurable: true, value: internal},
  });
  return published;
}

function materializeRunMapArray<T>(internal: T[]): T[] {
  const existing = publishedArrays.get(internal) as T[] | undefined;
  if (existing !== undefined) return existing;
  const published = publishProjectionValue([...internal]);
  publishedArrays.set(internal, published);
  publishedInternals.set(published, internal);
  return published;
}

/**
 * Closes every phase and round the run left open, at `timestamp`.
 *
 * `activeStatus` is what an agent that was running becomes: `interrupted` when
 * an operator or a signal stopped the run, `failed` when the run itself did. A
 * phase that never started becomes `cancelled`: it is not a failure, it is work
 * that will never be attempted. Phases that already reached a terminal status
 * keep it, so this is idempotent over the per-execution `phase_finished` events
 * a graceful teardown emits before its run-scoped event.
 *
 * Timings close on every round, not just one: an open `activeAgentStarts` entry
 * is what the elapsed selectors treat as "still running", so a round that keeps
 * one ticks forever after the process it was measuring is gone.
 */
function closeOpenRunState(
  state: RunMapState,
  activeStatus: Extract<AgentPhaseStatus, 'failed' | 'interrupted'>,
  timestamp: string,
  sequence: number | null = null,
): RunMapState {
  return {
    ...state,
    rounds: state.rounds.map(round => closeRound(round, timestamp, sequence)),
    phases: state.phases.map(phase => closePhase(phase, activeStatus, timestamp)),
  };
}

/**
 * Closes state a previous life of the same run left behind.
 *
 * A resumed process appends to the journal of a run that may have died without
 * a terminal event, because a hard kill gets no chance to emit one. Its open
 * phases and rounds belong to a process that no longer exists: nothing will
 * finish them, and their open timings would keep the round clocks running. The
 * new `run_started` is where the fold learns a new life began, so it is where
 * the old one ends, dated to when that life was last seen rather than to the
 * resume, so the downtime in between is not charged to the round. The first
 * `run_started` of a run has nothing open and leaves the state untouched.
 */
function closeAbandonedRunState(state: RunMapState, timestamp: string): RunMapState {
  if (!hasOpenRunState(state)) return state;
  return closeOpenRunState(state, 'interrupted', timestamp);
}

function hasOpenRunState(state: RunMapState): boolean {
  return (
    state.phases.some(phase => phase.status === 'active' || phase.status === 'pending') ||
    state.rounds.some(round => !isRoundClosed(round.status) || hasActiveAgentTiming(round))
  );
}

/** Whether a round has reached a status the run map never moves it off. */
function isRoundClosed(status: RoundStatus): boolean {
  return status === 'completed' || status === 'failed';
}

function closeRound(round: RoundState, timestamp: string, sequence: number | null): RoundState {
  const closed = isRoundClosed(round.status)
    ? round
    : {
        ...round,
        status: 'failed' as const,
        finishedAt: round.finishedAt ?? timestamp,
        closedByRunBoundary: true as const,
      };
  return hasActiveAgentTiming(closed)
    ? closeActiveAgentTimings(closed, timestamp, sequence)
    : closed;
}

function closePhase(
  phase: AgentPhase,
  activeStatus: Extract<AgentPhaseStatus, 'failed' | 'interrupted'>,
  timestamp: string,
): AgentPhase {
  if (phase.status === 'active') {
    return {...phase, status: activeStatus, finishedAt: phase.finishedAt ?? timestamp};
  }
  // A phase that never started has no end to record, so it takes the status and
  // no `finishedAt`: an agent that never ran did not run until `timestamp`.
  if (phase.status === 'pending') return {...phase, status: 'cancelled'};
  return phase;
}

export function phasesForRound(
  phases: readonly AgentPhase[],
  roundNumber: number | null,
): AgentPhase[] {
  return phases.filter(phase => phase.roundNumber === roundNumber);
}

/**
 * Merges a round list folded from older events under one folded from newer
 * events, as a backfilled history prefix does.
 *
 * `mergeRound` already resolves every scalar the way replay would: the newer
 * patch wins, the earliest start survives. Agent timing is the exception.
 * Intervals recorded on either side are both real, so they concatenate instead
 * of last-write-wins, and open starts union.
 *
 * Unmatched finishes retained in the newer suffix reconcile with starts in the
 * older prefix by event sequence, so a chunk boundary does not lose intervals.
 */
export function mergeRoundLists(
  older: readonly RoundState[],
  newer: readonly RoundState[],
): RoundState[] {
  const merged = new Map<string, RoundState>();
  const fallbackOrder: string[] = [];
  for (const round of older) {
    const token = roundKeyToken(round.key);
    merged.set(token, round);
    if (round.key.kind === 'label') fallbackOrder.push(token);
  }
  for (const round of newer) {
    const token = roundKeyToken(round.key);
    const existing = merged.get(token);
    merged.set(token, existing === undefined ? round : mergeRoundPrefix(existing, round));
    if (existing === undefined && round.key.kind === 'label') fallbackOrder.push(token);
  }
  const numbered = [...merged.values()]
    .filter(round => round.key.kind === 'number')
    .sort((left, right) => numberedRound(left) - numberedRound(right));
  const fallback = fallbackOrder.flatMap(token => {
    const round = merged.get(token);
    return round === undefined ? [] : [round];
  });
  return [...numbered, ...fallback];
}

/**
 * Merges a phase list folded from older events under one folded from newer
 * events.
 *
 * A phase is identified by role, round, and execution id. A newer phase that
 * carries an execution id lands on the matching older phase, else on the slot
 * the older fold seeded for that role, following `upsertPhase`'s precedence. A
 * newer phase with no execution id is a slot the newer fold seeded for itself;
 * replay would never have seeded it once the older phases existed, so it is
 * dropped when the older list already covers that role and round.
 */
export function mergePhaseLists(
  older: readonly AgentPhase[],
  newer: readonly AgentPhase[],
): AgentPhase[] {
  let merged = runMapArrayFrom(older);
  phaseIndexes.set(merged, buildPhaseIndex(merged));
  for (const phase of newer) {
    const target = prefixPhaseTarget(merged, phase);
    const existing = runMapArrayAt(merged, target);
    if (existing !== undefined) {
      merged = replacePhase(merged, target, mergePhase(existing, phase));
      continue;
    }
    if (
      phase.executionId === undefined &&
      phaseSlotFor(phaseIndexFor(merged), phase) !== undefined
    ) {
      continue;
    }
    merged = appendPhase(merged, phase);
  }
  return merged;
}

function mergeRoundPrefix(older: RoundState, newer: RoundState): RoundState {
  const round = mergeRound(older, newer);
  const timing = mergeAgentTimingPrefix(older, newer);
  const preserveRunBoundary =
    older.closedByRunBoundary === true && newer.closedByRoundFinished !== true;
  return {
    ...round,
    // A resume keeps the interrupted round closed even once its next attempt
    // starts. This matches `applyRoundEvent`, which never reopens a terminal
    // round during a chronological replay.
    ...(preserveRunBoundary
      ? {
          status: older.status,
          ...(older.finishedAt === undefined ? {} : {finishedAt: older.finishedAt}),
          closedByRunBoundary: true as const,
        }
      : isRoundClosed(older.status) && !isRoundClosed(newer.status)
        ? {status: older.status}
        : {}),
    ...timing,
  };
}

/** Where `patch` lands in `phases` under a prefix merge, or -1 to append. */
function prefixPhaseTarget(phases: AgentPhase[], patch: AgentPhase): number {
  if (patch.executionId === undefined) return -1;
  const slot = phaseSlotFor(phaseIndexFor(phases), patch);
  return (
    slot?.executions.get(patch.executionId) ??
    slot?.placeholderIndex ??
    slot?.activeIndices[0] ??
    -1
  );
}

function phaseIndexFor(phases: AgentPhase[]): PhaseIndex {
  const existing = phaseIndexes.get(phases);
  if (existing !== undefined) return existing;
  const built = buildPhaseIndex(phases);
  phaseIndexes.set(phases, built);
  return built;
}

function buildPhaseIndex(phases: AgentPhase[]): PhaseIndex {
  let index: PhaseIndex = {rounds: new Map(), unscoped: new Map()};
  for (let position = 0; position < phases.length; position += 1) {
    index = updatePhaseSlot(index, phases, position, true);
  }
  return index;
}

function phaseSlotFor(
  index: PhaseIndex,
  phase: Pick<AgentPhase, 'kind' | 'roundKey'>,
): PhaseSlot | undefined {
  const roles =
    phase.roundKey === null ? index.unscoped : index.rounds.get(roundKeyToken(phase.roundKey));
  return roles?.get(phase.kind);
}

function appendPhase(phases: AgentPhase[], phase: AgentPhase): AgentPhase[] {
  const next = appendRunMapArrayEntry(phases, phase);
  phaseIndexes.set(next, updatePhaseSlot(phaseIndexFor(phases), next, phases.length, true));
  return next;
}

function replacePhase(phases: AgentPhase[], position: number, phase: AgentPhase): AgentPhase[] {
  const previous = runMapArrayAt(phases, position);
  if (
    previous === undefined ||
    previous.kind !== phase.kind ||
    !sameRoundKey(previous.roundKey, phase.roundKey)
  ) {
    throw new Error(`Run-map phase replacement changed slot at index ${position}`);
  }
  const next = replaceRunMapArrayEntry(phases, position, phase);
  phaseIndexes.set(next, updatePhaseSlot(phaseIndexFor(phases), next, position, false));
  return next;
}

function updatePhaseSlot(
  index: PhaseIndex,
  phases: AgentPhase[],
  position: number,
  append: boolean,
): PhaseIndex {
  const phase = runMapArrayAt(phases, position);
  if (phase === undefined) return index;
  const current = phaseSlotFor(index, phase);
  const indices = append
    ? [...(current?.indices ?? []), position]
    : (current?.indices ?? [position]);
  const slot = summarizePhaseSlot(phases, indices);
  const roles =
    phase.roundKey === null
      ? new Map(index.unscoped)
      : new Map(index.rounds.get(roundKeyToken(phase.roundKey)) ?? []);
  roles.set(phase.kind, slot);
  if (phase.roundKey === null) return {...index, unscoped: roles};
  const rounds = new Map(index.rounds);
  rounds.set(roundKeyToken(phase.roundKey), roles);
  return {...index, rounds};
}

function summarizePhaseSlot(phases: AgentPhase[], indices: readonly number[]): PhaseSlot {
  let placeholderIndex: number | null = null;
  const activeIndices: number[] = [];
  const executions = new Map<string, number>();
  for (const position of indices) {
    const phase = runMapArrayAt(phases, position);
    if (phase === undefined) continue;
    if (phase.executionId === undefined) placeholderIndex ??= position;
    else if (!executions.has(phase.executionId)) executions.set(phase.executionId, position);
    if (phase.status === 'active') activeIndices.push(position);
  }
  return {indices, placeholderIndex, activeIndices, executions};
}

export function roundAgentElapsedMs(round: RoundState, now: Date): number {
  return activeTimingElapsedMs(round, now);
}

function applyPhaseEvent(state: RunMapState, event: RunEvent): AgentPhase[] {
  const kind = event.agent_kind;
  if (!kind) return state.phases;
  const roundKey = roundKeyFor(event);
  const roundNumber = roundNumberFor(roundKey);
  let phases = state.phases;
  const roles = expectedRolesForSeeding(state);
  if (roundKey !== null && roles !== null) {
    phases = seedExpectedPhases(roles, phases, roundKey);
  }
  const transition = phaseTransition(event);
  if (transition === null) return ensurePhase(phases, kind, roundKey);
  const started = transition === 'started';
  const executionId = event.execution_id ?? event.invocation_id ?? undefined;
  const data = event.data;
  const runtime =
    started && data?.kind === 'agent_execution_started'
      ? {driver: data.driver ?? null, provider: data.provider ?? null, model: data.model ?? null}
      : {};
  return upsertPhase(phases, {
    kind,
    status: started ? 'active' : terminalPhaseStatus(event.status),
    roundNumber,
    roundKey,
    roundLabel: event.round_label ?? null,
    ...(executionId ? {executionId, invocationId: executionId} : {}),
    ...(started ? {startedAt: event.timestamp} : {finishedAt: event.timestamp}),
    ...runtime,
  });
}

function phaseTransition(event: RunEvent): 'started' | 'finished' | null {
  if (event.type === 'agent_execution_started' || event.type === 'phase_started') return 'started';
  if (event.type === 'agent_execution_finished' || event.type === 'phase_finished') {
    return 'finished';
  }
  return null;
}

function terminalPhaseStatus(status: RunEvent['status']): AgentPhaseStatus {
  if (status === 'failed') return 'failed';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'interrupted') return 'interrupted';
  return 'completed';
}

function applyRoundEvent(
  rounds: RoundState[],
  phases: AgentPhase[],
  event: RunEvent,
): RoundState[] {
  const key = roundKeyFor(event);
  if (key === null || event.type === 'run_finished') return rounds;
  const number = roundNumberFor(key);
  const existingIndex = roundIndex(rounds, key);
  const existing = runMapArrayAt(rounds, existingIndex);
  // Run-scoped terminal events never reach here: `applyRunMapEvent` closes every
  // round for them, because the round a label names is not the only one open.
  const status =
    event.type === 'round_finished'
      ? event.status === 'failed'
        ? 'failed'
        : 'completed'
      : existing?.status === 'completed' || existing?.status === 'failed'
        ? existing.status
        : 'active';
  const terminal = event.type === 'round_finished';
  const patch: RoundState = {
    key,
    number,
    status,
    ...(terminal
      ? {finishedAt: event.timestamp, closedByRoundFinished: true as const}
      : {startedAt: event.timestamp}),
    ...(terminal && event.data?.kind === 'round_finished' && event.data.profile_skipped === true
      ? {profileSkipped: true}
      : {}),
  };
  const round = existing ? mergeRound(existing, patch) : patch;
  const updated = updateRoundAgentElapsed(round, phases, event);
  if (existing !== undefined && shallowEqual(existing, updated)) return rounds;
  return replaceRound(rounds, existingIndex, updated);
}

function seedExpectedPhases(
  roles: readonly string[],
  current: AgentPhase[],
  roundKey: RoundKey,
): AgentPhase[] {
  let phases = current;
  for (const kind of roles) {
    phases = ensurePhase(phases, kind, roundKey);
  }
  return phases;
}

/**
 * The roles a round seeds pending placeholders for: the set the backend
 * advertised in `run_started`, else the legacy table for recordings that
 * predate the advertised contract. Null when neither knows the loop, in which
 * case nothing is seeded and the round degrades gracefully to the phases its
 * events actually carry (`ensurePhase` still creates each observed role).
 */
export function expectedRolesForSeeding(
  state: Pick<RunMapState, 'outerLoop' | 'expectedRoles'>,
): readonly string[] | null {
  if (state.expectedRoles !== null) return state.expectedRoles;
  if (state.outerLoop === null) return null;
  return legacyExpectedRoles(state.outerLoop);
}

/**
 * Role tables for recordings whose `run_started` predates the backend's
 * advertised `expected_roles` contract. Frozen: the backend now owns which
 * roles a loop runs, so this table must never gain new loops or roles.
 */
function legacyExpectedRoles(outerLoop: string): readonly string[] | null {
  if (outerLoop === 'agent') return ['orchestrator', 'implementer', 'judge', 'profiler'];
  if (outerLoop === 'plain') return ['implementer', 'judge', 'perf_eval'];
  if (outerLoop === 'evolve') return ['implementer', 'judge', 'profiler'];
  return null;
}

function ensurePhase(phases: AgentPhase[], kind: string, roundKey: RoundKey | null): AgentPhase[] {
  const patch: AgentPhase = {
    kind,
    status: 'pending',
    roundKey,
    roundNumber: roundNumberFor(roundKey),
    roundLabel: roundKey?.kind === 'label' ? roundKey.label : null,
  };
  if (phaseSlotFor(phaseIndexFor(phases), patch) !== undefined) return phases;
  return appendPhase(phases, patch);
}

function upsertPhase(phases: AgentPhase[], patch: AgentPhase): AgentPhase[] {
  const slot = phaseSlotFor(phaseIndexFor(phases), patch);
  let existing =
    patch.executionId === undefined ? -1 : (slot?.executions.get(patch.executionId) ?? -1);
  if (existing === -1 && patch.status === 'active') {
    existing = slot?.placeholderIndex ?? -1;
  }
  if (existing === -1 && patch.status !== 'active') {
    existing = slot?.activeIndices[0] ?? -1;
  }
  if (existing === -1) return appendPhase(phases, patch);
  const phase = runMapArrayAt(phases, existing);
  return phase === undefined
    ? appendPhase(phases, patch)
    : replacePhase(phases, existing, mergePhase(phase, patch));
}

/** Applies `patch` to `phase`, keeping the identity and endpoints it already has. */
function mergePhase(phase: AgentPhase, patch: AgentPhase): AgentPhase {
  return {
    ...phase,
    ...patch,
    ...(phase.executionId !== undefined && phase.executionId !== patch.executionId
      ? {executionId: phase.executionId, invocationId: phase.invocationId}
      : {}),
    ...((patch.startedAt ?? phase.startedAt)
      ? {startedAt: patch.startedAt ?? phase.startedAt}
      : {}),
    ...((patch.finishedAt ?? phase.finishedAt)
      ? {finishedAt: patch.finishedAt ?? phase.finishedAt}
      : {}),
  };
}

function replaceRound(rounds: RoundState[], existing: number, round: RoundState): RoundState[] {
  if (existing !== -1) return replaceRunMapArrayEntry(rounds, existing, round);
  if (round.key.kind === 'label') return appendRunMapArrayEntry(rounds, round);
  const number = round.key.number;
  const insertion = rounds.findIndex(
    candidate =>
      candidate.key.kind === 'label' ||
      (candidate.key.kind === 'number' && candidate.key.number > number),
  );
  if (insertion === -1) return appendRunMapArrayEntry(rounds, round);
  return runMapArrayFrom([...rounds.slice(0, insertion), round, ...rounds.slice(insertion)]);
}

/** Returns the round's stable sorted position, or -1 when it has not been observed. */
function roundIndex(rounds: RoundState[], key: RoundKey): number {
  if (key.kind === 'label') {
    return rounds.findIndex(round => sameRoundKey(round.key, key));
  }
  // Numbered rounds stay sorted before fallback rows, so the hot path keeps
  // the logarithmic lookup used before tagged identities were introduced.
  let low = 0;
  let high = rounds.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const candidate = runMapArrayAt(rounds, middle);
    if (candidate === undefined) return -1;
    if (candidate.key.kind === 'label' || candidate.key.number > key.number) high = middle - 1;
    else if (candidate.key.number < key.number) low = middle + 1;
    else return middle;
  }
  return -1;
}

function shallowEqual(left: RoundState, right: RoundState): boolean {
  const leftKeys = Object.keys(left) as Array<keyof RoundState>;
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length && leftKeys.every(key => Object.is(left[key], right[key]))
  );
}

function mergeRound(round: RoundState, patch: RoundState): RoundState {
  const startedAt = earliestTimestamp(round.startedAt, patch.startedAt);
  return {
    ...round,
    ...patch,
    key: round.key,
    ...(startedAt ? {startedAt} : {}),
    ...((patch.finishedAt ?? round.finishedAt)
      ? {finishedAt: patch.finishedAt ?? round.finishedAt}
      : {}),
    ...((patch.agentIntervals ?? round.agentIntervals)
      ? {agentIntervals: patch.agentIntervals ?? round.agentIntervals}
      : {}),
    ...((patch.activeAgentStarts ?? round.activeAgentStarts)
      ? {activeAgentStarts: patch.activeAgentStarts ?? round.activeAgentStarts}
      : {}),
  };
}

function numberedRound(round: RoundState): number {
  return round.key.kind === 'number' ? round.key.number : Number.POSITIVE_INFINITY;
}

function earliestTimestamp(
  left: string | undefined,
  right: string | undefined,
): string | undefined {
  if (!left) return right;
  if (!right) return left;
  return new Date(right).getTime() < new Date(left).getTime() ? right : left;
}

function updateRoundAgentElapsed(
  round: RoundState,
  phases: AgentPhase[],
  event: RunEvent,
): RoundState {
  const started = event.type === 'agent_execution_started' || event.type === 'phase_started';
  const finished = event.type === 'agent_execution_finished' || event.type === 'phase_finished';
  if (!started && !finished) {
    if (event.type !== 'round_finished') return round;
    return closeActiveAgentTimings(round, event.timestamp, event.sequence ?? null);
  }
  if (compatibilityPhaseTimingAlreadyApplied(phases, event)) return round;
  return started ? startAgentTiming(round, event) : finishAgentTiming(round, event);
}

function compatibilityPhaseTimingAlreadyApplied(phases: AgentPhase[], event: RunEvent): boolean {
  if (event.type !== 'phase_started' && event.type !== 'phase_finished') return false;
  const executionId = event.execution_id ?? event.invocation_id;
  if (executionId == null) return false;
  const slot = phaseSlotFor(phaseIndexFor(phases), {
    kind: event.agent_kind ?? '',
    roundKey: roundKeyFor(event),
  });
  const existingIndex = slot?.executions.get(executionId);
  const existing = existingIndex === undefined ? undefined : runMapArrayAt(phases, existingIndex);
  if (event.type === 'phase_started') return existing?.status === 'active';
  return existing !== undefined && existing.status !== 'active';
}
