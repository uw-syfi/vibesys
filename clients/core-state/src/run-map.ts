import type {RunEvent} from '@vibesys/backend-client';
import {
  appendPersistentArrayEntry,
  materializePersistentArray,
  type PersistentArray,
  persistentArrayAt,
  persistentArrayFrom,
  rehydratePersistentArray,
  replacePersistentArrayEntry,
  setPersistentArrayIndex,
} from './persistent-array.js';
import {ownProjectionInput, publishProjectionValue} from './publication.js';
import {type RoundKey, roundKeyFor, roundNumberFor, sameRoundKey} from './round-key.js';
import {
  activeTimingElapsedMs,
  closeActiveAgentTimings,
  finishAgentTiming,
  hasActiveAgentTiming,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';

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
  /** Present on reducer-produced states; omitted only on legacy caller inputs. */
  readonly provenance?: RunMapProvenance;
}

interface PhaseSlot {
  readonly indices: readonly number[];
  readonly placeholderIndex: number | null;
  readonly activeIndices: readonly number[];
  readonly executions: Readonly<Record<string, number>>;
}

type PhaseRoleIndex = Readonly<Record<string, PhaseSlot>>;

interface PhaseIndex {
  readonly numbered: PersistentArray<PhaseRoleIndex>;
  readonly labeled: Readonly<Record<string, PhaseRoleIndex>>;
  readonly unscoped: PhaseRoleIndex;
}

export interface RunMapProvenance {
  readonly version: 1;
  readonly rounds: PersistentArray<RoundState>;
  readonly phases: PersistentArray<AgentPhase>;
  readonly phaseIndex: PhaseIndex;
}

interface RunMapWorkingState {
  readonly outerLoop: string | null;
  readonly expectedRoles: readonly string[] | null;
  readonly rounds: PersistentArray<RoundState>;
  readonly phases: PersistentArray<AgentPhase>;
  readonly phaseIndex: PhaseIndex;
  readonly lastEventTimestamp: string | null;
}

interface IndexedPhases {
  readonly values: PersistentArray<AgentPhase>;
  readonly index: PhaseIndex;
}

export function applyRunMapEvent(
  state: RunMapProjection,
  event: RunEvent,
  abandonedAt: string | null = null,
  provenance?: RunMapProvenance,
): RunMapState {
  return publishRunMapState(applyIndexedRunMapEvent(state, event, abandonedAt, provenance));
}

function applyIndexedRunMapEvent(
  state: RunMapProjection,
  event: RunEvent,
  abandonedAt: string | null,
  provenance: RunMapProvenance | undefined,
): RunMapWorkingState {
  const internal = runMapInternal(state, provenance);
  const seen: RunMapWorkingState = {
    outerLoop: state.outerLoop,
    expectedRoles: state.expectedRoles,
    rounds: internal.rounds,
    phases: internal.phases,
    phaseIndex: internal.phaseIndex,
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
  const rounds = applyRoundEvent(base.rounds, {values: base.phases, index: base.phaseIndex}, event);
  const phases = applyPhaseEvent({...base, outerLoop, expectedRoles, rounds}, event);
  return {
    outerLoop,
    expectedRoles,
    rounds,
    phases: phases.values,
    phaseIndex: phases.index,
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

/** Installs lazy public arrays from explicit serializable run-map provenance. */
export function adoptRunMapArrays(target: object, source: RunMapProjection): void {
  const provenance = (source as RunMapState).provenance;
  if (provenance === undefined) throw new Error('Run-map state has no serializable provenance');
  installRunMapArrays(target, provenance);
}

export function initialRunMapProvenance(): RunMapProvenance {
  return {
    version: 1,
    rounds: persistentArrayFrom([]),
    phases: persistentArrayFrom([]),
    phaseIndex: emptyPhaseIndex(),
  };
}

/** Restores non-serialized array materializers after a clone boundary. */
export function rehydrateRunMapProvenance(provenance: RunMapProvenance): RunMapProvenance {
  const version = (provenance as {version?: unknown}).version;
  if (version !== 1) {
    throw new Error(`Unsupported run-map provenance version: ${String(version)}`);
  }
  return {
    version: 1,
    rounds: rehydratePersistentArray(provenance.rounds),
    phases: rehydratePersistentArray(provenance.phases),
    phaseIndex: {
      ...provenance.phaseIndex,
      numbered: rehydratePersistentArray(provenance.phaseIndex.numbered),
    },
  };
}

export function installRunMapArrays(target: object, provenance: RunMapProvenance): void {
  let rounds: RoundState[] | undefined;
  let phases: AgentPhase[] | undefined;
  Object.defineProperties(target, {
    rounds: {
      configurable: true,
      enumerable: true,
      get: () => (rounds ??= publishProjectionValue(materializePersistentArray(provenance.rounds))),
    },
    phases: {
      configurable: true,
      enumerable: true,
      get: () => (phases ??= publishProjectionValue(materializePersistentArray(provenance.phases))),
    },
  });
}

function runMapInternal(
  state: RunMapProjection,
  provenance: RunMapProvenance | undefined,
): RunMapProvenance {
  const retained = provenance ?? (state as RunMapState).provenance;
  if (retained !== undefined) {
    return rehydrateRunMapProvenance(retained);
  }
  const rounds = persistentArrayFrom(state.rounds);
  const phases = persistentArrayFrom(state.phases);
  return {version: 1, rounds, phases, phaseIndex: buildPhaseIndex(phases)};
}

function publishRunMapState(state: RunMapWorkingState): RunMapState {
  const provenance: RunMapProvenance = {
    version: 1,
    rounds: state.rounds,
    phases: state.phases,
    phaseIndex: state.phaseIndex,
  };
  const published = {
    outerLoop: state.outerLoop,
    expectedRoles: state.expectedRoles,
    lastEventTimestamp: state.lastEventTimestamp,
    provenance,
  } as RunMapState;
  installRunMapArrays(published, provenance);
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
  state: RunMapWorkingState,
  activeStatus: Extract<AgentPhaseStatus, 'failed' | 'interrupted'>,
  timestamp: string,
  sequence: number | null = null,
): RunMapWorkingState {
  const phases = persistentArrayFrom(
    materializePersistentArray(state.phases).map(phase =>
      closePhase(phase, activeStatus, timestamp),
    ),
  );
  return {
    ...state,
    rounds: persistentArrayFrom(
      materializePersistentArray(state.rounds).map(round => closeRound(round, timestamp, sequence)),
    ),
    phases,
    phaseIndex: buildPhaseIndex(phases),
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
function closeAbandonedRunState(state: RunMapWorkingState, timestamp: string): RunMapWorkingState {
  if (!hasOpenRunState(state)) return state;
  return closeOpenRunState(state, 'interrupted', timestamp);
}

function hasOpenRunState(state: RunMapWorkingState): boolean {
  return (
    materializePersistentArray(state.phases).some(
      phase => phase.status === 'active' || phase.status === 'pending',
    ) ||
    materializePersistentArray(state.rounds).some(
      round => !isRoundClosed(round.status) || hasActiveAgentTiming(round),
    )
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

function emptyPhaseIndex(): PhaseIndex {
  return {numbered: persistentArrayFrom([]), labeled: {}, unscoped: {}};
}

function buildPhaseIndex(phases: PersistentArray<AgentPhase>): PhaseIndex {
  let index = emptyPhaseIndex();
  for (let position = 0; position < phases.length; position += 1) {
    index = updatePhaseSlot(index, phases, position, true);
  }
  return index;
}

function phaseSlotFor(
  index: PhaseIndex,
  phase: Pick<AgentPhase, 'kind' | 'roundKey'>,
): PhaseSlot | undefined {
  const roles = phaseRolesFor(index, phase.roundKey);
  return roles?.[phaseRoleToken(phase.kind)];
}

function phaseRolesFor(index: PhaseIndex, key: RoundKey | null): PhaseRoleIndex | undefined {
  if (key === null) return index.unscoped;
  return key.kind === 'number'
    ? (persistentArrayAt(index.numbered, key.number) ?? undefined)
    : index.labeled[roundLabelToken(key.label)];
}

function appendPhase(phases: IndexedPhases, phase: AgentPhase): IndexedPhases {
  const values = appendPersistentArrayEntry(phases.values, phase);
  return {
    values,
    index: updatePhaseSlot(phases.index, values, phases.values.length, true),
  };
}

function replacePhase(phases: IndexedPhases, position: number, phase: AgentPhase): IndexedPhases {
  const previous = persistentArrayAt(phases.values, position);
  if (
    previous === undefined ||
    previous.kind !== phase.kind ||
    !sameRoundKey(previous.roundKey, phase.roundKey)
  ) {
    throw new Error(`Run-map phase replacement changed slot at index ${position}`);
  }
  const values = replacePersistentArrayEntry(phases.values, position, phase);
  return {values, index: updatePhaseSlot(phases.index, values, position, false)};
}

function updatePhaseSlot(
  index: PhaseIndex,
  phases: PersistentArray<AgentPhase>,
  position: number,
  append: boolean,
): PhaseIndex {
  const phase = persistentArrayAt(phases, position);
  if (phase === undefined) return index;
  const current = phaseSlotFor(index, phase);
  const indices = append
    ? [...(current?.indices ?? []), position]
    : (current?.indices ?? [position]);
  const slot = summarizePhaseSlot(phases, indices);
  const roles = {
    ...phaseRolesFor(index, phase.roundKey),
    [phaseRoleToken(phase.kind)]: slot,
  };
  if (phase.roundKey === null) return {...index, unscoped: roles};
  if (phase.roundKey.kind === 'label') {
    return {
      ...index,
      labeled: {...index.labeled, [roundLabelToken(phase.roundKey.label)]: roles},
    };
  }
  return {
    ...index,
    numbered: setPersistentArrayIndex(index.numbered, phase.roundKey.number, roles),
  };
}

function summarizePhaseSlot(
  phases: PersistentArray<AgentPhase>,
  indices: readonly number[],
): PhaseSlot {
  let placeholderIndex: number | null = null;
  const activeIndices: number[] = [];
  const executions: Record<string, number> = {};
  for (const position of indices) {
    const phase = persistentArrayAt(phases, position);
    if (phase === undefined) continue;
    if (phase.executionId === undefined) placeholderIndex ??= position;
    else executions[executionToken(phase.executionId)] ??= position;
    if (phase.status === 'active') activeIndices.push(position);
  }
  return {indices, placeholderIndex, activeIndices, executions};
}

function phaseRoleToken(kind: string): string {
  return JSON.stringify(kind);
}

function roundLabelToken(label: string): string {
  return JSON.stringify(label);
}

function executionToken(executionId: string): string {
  return JSON.stringify(executionId);
}

export function roundAgentElapsedMs(round: RoundState, now: Date): number {
  return activeTimingElapsedMs(round, now);
}

function applyPhaseEvent(state: RunMapWorkingState, event: RunEvent): IndexedPhases {
  let phases: IndexedPhases = {values: state.phases, index: state.phaseIndex};
  const kind = event.agent_kind;
  if (!kind) return phases;
  const roundKey = roundKeyFor(event);
  const roundNumber = roundNumberFor(roundKey);
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
      ? {provider: data.provider ?? null, model: data.model ?? null}
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
  rounds: PersistentArray<RoundState>,
  phases: IndexedPhases,
  event: RunEvent,
): PersistentArray<RoundState> {
  const key = roundKeyFor(event);
  if (key === null || event.type === 'run_finished') return rounds;
  const number = roundNumberFor(key);
  const existingIndex = roundIndex(rounds, key);
  const existing = persistentArrayAt(rounds, existingIndex);
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
  current: IndexedPhases,
  roundKey: RoundKey,
): IndexedPhases {
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
  state: Pick<RunMapWorkingState, 'outerLoop' | 'expectedRoles'>,
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

function ensurePhase(
  phases: IndexedPhases,
  kind: string,
  roundKey: RoundKey | null,
): IndexedPhases {
  const patch: AgentPhase = {
    kind,
    status: 'pending',
    roundKey,
    roundNumber: roundNumberFor(roundKey),
    roundLabel: roundKey?.kind === 'label' ? roundKey.label : null,
  };
  if (phaseSlotFor(phases.index, patch) !== undefined) return phases;
  return appendPhase(phases, patch);
}

function upsertPhase(phases: IndexedPhases, patch: AgentPhase): IndexedPhases {
  const slot = phaseSlotFor(phases.index, patch);
  let existing =
    patch.executionId === undefined
      ? -1
      : (slot?.executions[executionToken(patch.executionId)] ?? -1);
  if (existing === -1 && patch.status === 'active') {
    existing = slot?.placeholderIndex ?? -1;
  }
  if (existing === -1 && patch.status !== 'active') {
    existing = slot?.activeIndices[0] ?? -1;
  }
  if (existing === -1) return appendPhase(phases, patch);
  const phase = persistentArrayAt(phases.values, existing);
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
  rounds: PersistentArray<RoundState>,
  existing: number,
  round: RoundState,
): PersistentArray<RoundState> {
  if (existing !== -1) return replacePersistentArrayEntry(rounds, existing, round);
  if (round.key.kind === 'label') return appendPersistentArrayEntry(rounds, round);
  const number = round.key.number;
  const materialized = materializePersistentArray(rounds);
  const insertion = materialized.findIndex(
    candidate =>
      candidate.key.kind === 'label' ||
      (candidate.key.kind === 'number' && candidate.key.number > number),
  );
  if (insertion === -1) return appendPersistentArrayEntry(rounds, round);
  return persistentArrayFrom([
    ...materialized.slice(0, insertion),
    round,
    ...materialized.slice(insertion),
  ]);
}

/** Returns the round's stable sorted position, or -1 when it has not been observed. */
function roundIndex(rounds: PersistentArray<RoundState>, key: RoundKey): number {
  if (key.kind === 'label') return labeledRoundIndex(rounds, key);
  // Numbered rounds stay sorted before fallback rows, so the hot path keeps
  // the logarithmic lookup used before tagged identities were introduced.
  let low = 0;
  let high = rounds.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const candidate = persistentArrayAt(rounds, middle);
    if (candidate === undefined) return -1;
    if (candidate.key.kind === 'label' || candidate.key.number > key.number) high = middle - 1;
    else if (candidate.key.number < key.number) low = middle + 1;
    else return middle;
  }
  return -1;
}

function labeledRoundIndex(rounds: PersistentArray<RoundState>, key: RoundKey): number {
  for (let index = 0; index < rounds.length; index += 1) {
    const round = persistentArrayAt(rounds, index);
    if (round !== undefined && sameRoundKey(round.key, key)) return index;
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
  phases: IndexedPhases,
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

function compatibilityPhaseTimingAlreadyApplied(phases: IndexedPhases, event: RunEvent): boolean {
  if (event.type !== 'phase_started' && event.type !== 'phase_finished') return false;
  const executionId = event.execution_id ?? event.invocation_id;
  if (executionId == null) return false;
  const slot = phaseSlotFor(phases.index, {
    kind: event.agent_kind ?? '',
    roundKey: roundKeyFor(event),
  });
  const existingIndex = slot?.executions[executionToken(executionId)];
  const existing =
    existingIndex === undefined ? undefined : persistentArrayAt(phases.values, existingIndex);
  if (event.type === 'phase_started') return existing?.status === 'active';
  return existing !== undefined && existing.status !== 'active';
}
