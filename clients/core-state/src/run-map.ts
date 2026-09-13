import type {RunEvent} from '@vibesys/backend-client';
import {
  activeTimingElapsedMs,
  closeActiveAgentTimings,
  finishAgentTiming,
  hasActiveAgentTiming,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';
import {
  appendRunMapArrayEntry,
  ensureRunMapArray,
  replaceRunMapArrayEntry,
  runMapArrayAt,
  runMapArrayFrom,
  setRunMapArrayEntry,
} from './run-map-array.js';

export type AgentPhaseStatus =
  | 'pending'
  | 'active'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted';
export type RoundStatus = 'active' | 'completed' | 'failed' | 'planned';

export interface RoundSummary extends RoundTimingState {
  number: number;
  status: RoundStatus;
  startedAt?: string;
  finishedAt?: string;
  /** Internal provenance for a terminal endpoint inferred at a run boundary. */
  closedByRunBoundary?: true;
  /** An explicit round_finished event may supersede a prior inferred endpoint. */
  closedByRoundFinished?: true;
  /** The round completed without a fresh profile measurement. */
  profileSkipped?: boolean;
}

export interface AgentPhase {
  kind: string;
  status: AgentPhaseStatus;
  roundNumber: number | null;
  roundLabel: string | null;
  executionId?: string;
  invocationId?: string;
  startedAt?: string;
  finishedAt?: string;
  driver?: string | null;
  provider?: string | null;
  model?: string | null;
}

export interface RunMapState {
  outerLoop: string | null;
  /**
   * The agent roles the backend advertised in `run_started`; null on
   * recordings that predate the field, where `legacyExpectedRoles` applies.
   */
  expectedRoles: readonly string[] | null;
  rounds: RoundSummary[];
  phases: AgentPhase[];
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
  lastEventTimestamp: string | null;
}

interface PhaseSlot {
  readonly indices: readonly number[];
  readonly placeholderIndex: number | null;
  readonly activeIndices: readonly number[];
  readonly executions: ReadonlyMap<string, number>;
}

type PhaseRoleIndex = ReadonlyMap<string, PhaseSlot>;

interface PhaseIndex {
  readonly rounds: Array<PhaseRoleIndex | undefined>;
  readonly unscoped: PhaseRoleIndex;
}

const phaseIndexes = new WeakMap<AgentPhase[], PhaseIndex>();
const publishedArrays = new WeakMap<readonly unknown[], unknown[]>();
const publishedInternals = new WeakMap<readonly unknown[], unknown[]>();
const RUN_MAP_INTERNAL = Symbol('runMapInternal');

interface RunMapInternal {
  readonly rounds: RoundSummary[];
  readonly phases: AgentPhase[];
}

type IndexedRunMapState = RunMapState & {[RUN_MAP_INTERNAL]?: RunMapInternal};

export function applyRunMapEvent(
  state: RunMapState,
  event: RunEvent,
  abandonedAt: string | null = null,
): RunMapState {
  return publishRunMapState(applyIndexedRunMapEvent(state, event, abandonedAt));
}

function applyIndexedRunMapEvent(
  state: RunMapState,
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
  // Run-scoped terminal events say the run ended, not which agent ended it, so
  // they carry no `agent_kind` and no `round_label`. Every projection below is
  // keyed by that scope and would drop them, which is why the closeout runs
  // first and returns: one owner for "the run ended", sweeping the whole map
  // rather than the one round a label happened to name.
  if (event.type === 'run_failed') return closeOpenRunState(seen, 'failed', event.timestamp);
  if (event.type === 'run_interrupted') {
    return closeOpenRunState(seen, 'interrupted', event.timestamp);
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
      ? started.expected_roles
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

/** Copies lazy run-map array publication from `source` onto a folded core state. */
export function adoptRunMapArrays(target: RunMapState, source: RunMapState): void {
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
export function indexRunMapArrays(state: RunMapState): void {
  const publishedRounds = state.rounds;
  const publishedPhases = state.phases;
  const rounds = runMapArrayFrom(publishedRounds);
  const phases = runMapArrayFrom(publishedPhases);
  phaseIndexes.set(phases, buildPhaseIndex(phases));
  publishedArrays.set(rounds, publishedRounds);
  publishedArrays.set(phases, publishedPhases);
  publishedInternals.set(publishedRounds, rounds);
  publishedInternals.set(publishedPhases, phases);
  adoptRunMapArrays(state, publishRunMapState({...state, rounds, phases}));
}

function runMapInternal(state: RunMapState): RunMapInternal {
  const existing = (state as IndexedRunMapState)[RUN_MAP_INTERNAL];
  if (existing !== undefined) return existing;
  const rounds = internalRunMapArray(state.rounds);
  const phases = internalRunMapArray(state.phases);
  if (!phaseIndexes.has(phases)) phaseIndexes.set(phases, buildPhaseIndex(phases));
  return {rounds, phases};
}

function internalRunMapArray<T>(published: T[]): T[] {
  return (publishedInternals.get(published) as T[] | undefined) ?? runMapArrayFrom(published);
}

function publishRunMapState(state: RunMapState): RunMapState {
  let publishedRounds: RoundSummary[] | undefined;
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
  const published = [...internal];
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
): RunMapState {
  return {
    ...state,
    rounds: state.rounds.map(round => closeRound(round, timestamp)),
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

function closeRound(round: RoundSummary, timestamp: string): RoundSummary {
  const closed = isRoundClosed(round.status)
    ? round
    : {
        ...round,
        status: 'failed' as const,
        finishedAt: round.finishedAt ?? timestamp,
        closedByRunBoundary: true as const,
      };
  return hasActiveAgentTiming(closed) ? closeActiveAgentTimings(closed, timestamp) : closed;
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

export function roundNumberFromLabel(label: string | null | undefined): number | null {
  if (!label) return null;
  const match = label.match(/(?:round|iter(?:ation)?)\D*(\d+)/i);
  return match ? Number(match[1]) : null;
}

export function phasesForRound(phases: AgentPhase[], roundNumber: number | null): AgentPhase[] {
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
 * Known boundary: an agent execution whose start is in `older` and whose finish
 * is in `newer` loses its interval. The newer fold saw a finish with no start
 * and dropped it, and the finish timestamp is not recoverable from the merged
 * state. Rounds that do not straddle the boundary are exact.
 */
export function mergeRoundLists(
  older: readonly RoundSummary[],
  newer: readonly RoundSummary[],
): RoundSummary[] {
  const merged = new Map<number, RoundSummary>();
  for (const round of older) merged.set(round.number, round);
  for (const round of newer) {
    const existing = merged.get(round.number);
    merged.set(round.number, existing === undefined ? round : mergeRoundPrefix(existing, round));
  }
  return [...merged.values()].sort((left, right) => left.number - right.number);
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

function mergeRoundPrefix(older: RoundSummary, newer: RoundSummary): RoundSummary {
  const round = mergeRound(older, newer);
  const agentIntervals =
    older.agentIntervals === undefined && newer.agentIntervals === undefined
      ? undefined
      : [...(older.agentIntervals ?? []), ...(newer.agentIntervals ?? [])];
  const activeAgentStarts =
    older.activeAgentStarts === undefined && newer.activeAgentStarts === undefined
      ? undefined
      : {...older.activeAgentStarts, ...newer.activeAgentStarts};
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
    ...(agentIntervals === undefined ? {} : {agentIntervals}),
    ...(activeAgentStarts === undefined ? {} : {activeAgentStarts}),
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
  let index: PhaseIndex = {rounds: [], unscoped: new Map()};
  for (let position = 0; position < phases.length; position += 1) {
    index = updatePhaseSlot(index, phases, position, true);
  }
  return index;
}

function phaseSlotFor(
  index: PhaseIndex,
  phase: Pick<AgentPhase, 'kind' | 'roundNumber'>,
): PhaseSlot | undefined {
  const roles =
    phase.roundNumber === null ? index.unscoped : runMapArrayAt(index.rounds, phase.roundNumber);
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
    previous.roundNumber !== phase.roundNumber
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
    phase.roundNumber === null
      ? new Map(index.unscoped)
      : new Map(runMapArrayAt(index.rounds, phase.roundNumber) ?? []);
  roles.set(phase.kind, slot);
  if (phase.roundNumber === null) return {...index, unscoped: roles};
  return {...index, rounds: setRunMapArrayEntry(index.rounds, phase.roundNumber, roles)};
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

export function roundAgentElapsedMs(round: RoundSummary, now: Date): number {
  return activeTimingElapsedMs(round, now);
}

function applyPhaseEvent(state: RunMapState, event: RunEvent): AgentPhase[] {
  const kind = event.agent_kind;
  if (!kind) return state.phases;
  const roundNumber = roundNumberFromLabel(event.round_label);
  let phases = state.phases;
  const roles = expectedRolesForSeeding(state);
  if (roundNumber !== null && roles !== null) {
    phases = seedExpectedPhases(roles, phases, roundNumber);
  }
  const started = event.type === 'agent_execution_started' || event.type === 'phase_started';
  const finished = event.type === 'agent_execution_finished' || event.type === 'phase_finished';
  if (!started && !finished) return ensurePhase(phases, kind, roundNumber);
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
    roundLabel: event.round_label ?? null,
    ...(executionId ? {executionId, invocationId: executionId} : {}),
    ...(started ? {startedAt: event.timestamp} : {finishedAt: event.timestamp}),
    ...runtime,
  });
}

function terminalPhaseStatus(status: RunEvent['status']): AgentPhaseStatus {
  if (status === 'failed') return 'failed';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'interrupted') return 'interrupted';
  return 'completed';
}

function applyRoundEvent(
  rounds: RoundSummary[],
  phases: AgentPhase[],
  event: RunEvent,
): RoundSummary[] {
  const number = roundNumberFromLabel(event.round_label);
  if (number === null || event.type === 'run_finished') return rounds;
  const existingIndex = roundIndex(rounds, number);
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
  const patch: RoundSummary = {
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
  roundNumber: number,
): AgentPhase[] {
  let phases = current;
  for (const kind of roles) {
    phases = ensurePhase(phases, kind, roundNumber);
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

function ensurePhase(phases: AgentPhase[], kind: string, roundNumber: number | null): AgentPhase[] {
  const patch: AgentPhase = {kind, status: 'pending', roundNumber, roundLabel: null};
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

function replaceRound(
  rounds: RoundSummary[],
  existing: number,
  round: RoundSummary,
): RoundSummary[] {
  if (existing !== -1) return replaceRunMapArrayEntry(rounds, existing, round);
  const last = runMapArrayAt(rounds, rounds.length - 1);
  if (last === undefined || last.number < round.number)
    return appendRunMapArrayEntry(rounds, round);
  return runMapArrayFrom([...rounds, round].sort((left, right) => left.number - right.number));
}

/** Returns the round's stable sorted position, or -1 when it has not been observed. */
function roundIndex(rounds: RoundSummary[], number: number): number {
  let low = 0;
  let high = rounds.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const candidate = runMapArrayAt(rounds, middle);
    if (candidate === undefined) return -1;
    if (candidate.number === number) return middle;
    if (candidate.number < number) low = middle + 1;
    else high = middle - 1;
  }
  return -1;
}

function shallowEqual(left: RoundSummary, right: RoundSummary): boolean {
  const leftKeys = Object.keys(left) as Array<keyof RoundSummary>;
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length && leftKeys.every(key => Object.is(left[key], right[key]))
  );
}

function mergeRound(round: RoundSummary, patch: RoundSummary): RoundSummary {
  const startedAt = earliestTimestamp(round.startedAt, patch.startedAt);
  return {
    ...round,
    ...patch,
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

function earliestTimestamp(
  left: string | undefined,
  right: string | undefined,
): string | undefined {
  if (!left) return right;
  if (!right) return left;
  return new Date(right).getTime() < new Date(left).getTime() ? right : left;
}

function updateRoundAgentElapsed(
  round: RoundSummary,
  phases: AgentPhase[],
  event: RunEvent,
): RoundSummary {
  const started = event.type === 'agent_execution_started' || event.type === 'phase_started';
  const finished = event.type === 'agent_execution_finished' || event.type === 'phase_finished';
  if (!started && !finished) {
    if (event.type !== 'round_finished') return round;
    return closeActiveAgentTimings(round, event.timestamp);
  }
  if (event.type === 'phase_started' || event.type === 'phase_finished') {
    const executionId = event.execution_id ?? event.invocation_id;
    const slot = phaseSlotFor(phaseIndexFor(phases), {
      kind: event.agent_kind ?? '',
      roundNumber: roundNumberFromLabel(event.round_label),
    });
    const existingIndex = executionId == null ? undefined : slot?.executions.get(executionId);
    const existing = existingIndex === undefined ? undefined : runMapArrayAt(phases, existingIndex);
    if (
      (started && existing?.status === 'active') ||
      (finished && existing !== undefined && existing.status !== 'active')
    ) {
      return round;
    }
  }
  return started ? startAgentTiming(round, event) : finishAgentTiming(round, event);
}
