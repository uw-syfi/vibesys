/**
 * The run fold's reducers: a snapshot, an ordered batch, a rebootstrap, a replayed prefix, or one
 * event, each folded with run identity, the stream cursor, and replay provenance kept consistent.
 */
import type {RunEvent, RunSnapshot} from '@vibesys/contracts';
import {applyChatEvent, retainRegisteredChatThreads, upsertChatThread} from './chat.js';
import {applyDiagnosticEvent, upsertDiagnostic} from './diagnostics.js';
import {
  applyExecutionEventData,
  applyRunEventData,
  applyRunLifecycle,
  applyRunTranscript,
  eventDataHandlers,
} from './event-data.js';
import {reconcileExecutionStatuses} from './execution-status.js';
import {activeExecutionsFromCheckpoint, reconcileActiveExecutions} from './executions.js';
import {ownProjectionInput} from './publication.js';
import {
  acceptsRunMetadata,
  compareReplayDeliveries,
  ownedReplayEvents,
  replaceReplayEvents,
  retainedReplayEvents,
  retainReplayDeliveries,
} from './replay.js';
import {adoptRunMapArrays, applyRunMapEvent} from './run-map.js';
import {
  type ActiveExecutionCheckpoint,
  type CoreDiagnostic,
  type CoreState,
  cloneCoreState,
  cloneCoreStateWith,
  coreStateProvenance,
  type DeliveryRoute,
  hasRunEnded,
  initialCoreState,
  type MutableCoreState,
  publishCoreState,
  type ReplayDelivery,
  type RunLifetimeBoundary,
} from './state.js';
import {TranscriptFolder} from './transcript-folder.js';

/** How one event batch participates in a fragmented active-execution checkpoint. */
export interface ActiveExecutionCheckpointFold {
  readonly kind: 'deferred' | 'complete';
  readonly checkpointId: string;
}

function withoutDeferredExecutionCheckpoint(state: CoreState): CoreState {
  const provenance = coreStateProvenance(state);
  if (provenance.activeExecutionCheckpoint === undefined) return state;
  const {activeExecutionCheckpoint: _discarded, ...settled} = provenance;
  return cloneCoreStateWith(state, {provenance: settled});
}

function settleDeferredExecutionCheckpoint(state: CoreState): CoreState {
  const pending = coreStateProvenance(state).activeExecutionCheckpoint;
  if (pending === undefined) return state;
  return cloneCoreStateWith(withoutDeferredExecutionCheckpoint(state), {
    activeExecutions: pending.activeExecutions,
    executionStatuses: pending.executionStatuses,
  });
}

function checkpointWorkingState(
  state: CoreState,
  checkpoint: ActiveExecutionCheckpointFold,
  rebootstrap: boolean,
): CoreState {
  const pending = coreStateProvenance(state).activeExecutionCheckpoint;
  if (checkpoint.kind === 'complete') {
    if (pending?.checkpointId !== checkpoint.checkpointId) {
      throw new Error(
        `Active-execution checkpoint ${checkpoint.checkpointId} has no deferred fold`,
      );
    }
    return cloneCoreStateWith(state, {
      activeExecutions: pending.activeExecutions,
      executionStatuses: pending.executionStatuses,
    });
  }
  if (rebootstrap) return initialCoreState();
  if (pending?.checkpointId === checkpoint.checkpointId) {
    return cloneCoreStateWith(state, {
      activeExecutions: pending.activeExecutions,
      executionStatuses: pending.executionStatuses,
    });
  }
  return settleDeferredExecutionCheckpoint(state);
}

function deferExecutionCheckpoint(
  publicState: CoreState,
  workingState: CoreState,
  checkpointId: string,
  acceptsMetadata: boolean,
  throughSequence: number | undefined,
): CoreState {
  return cloneCoreStateWith(workingState, {
    activeExecutions: publicState.activeExecutions,
    executionStatuses: publicState.executionStatuses,
    provenance: {
      ...coreStateProvenance(workingState),
      activeExecutionCheckpoint: {
        checkpointId,
        throughSequence,
        acceptsMetadata,
        activeExecutions: workingState.activeExecutions,
        executionStatuses: workingState.executionStatuses,
      },
    },
  });
}

/** Fold one delivery route against staged execution maps without publishing them early. */
function foldRoutedEventsDuringCheckpoint(
  state: CoreState,
  events: readonly RunEvent[],
  route: DeliveryRoute,
): CoreState {
  const pending = coreStateProvenance(state).activeExecutionCheckpoint;
  if (pending === undefined) {
    const batch = foldIdentityAwareBatch(state, events, route);
    return retainReplayDeliveries(state, batch.state, batch.retainedDeliveries);
  }
  const working = cloneCoreStateWith(state, {
    activeExecutions: pending.activeExecutions,
    executionStatuses: pending.executionStatuses,
  });
  const batch = foldIdentityAwareBatch(working, events, route);
  const retained = retainReplayDeliveries(state, batch.state, batch.retainedDeliveries);
  return cloneCoreStateWith(retained, {
    activeExecutions: state.activeExecutions,
    executionStatuses: state.executionStatuses,
    provenance: {
      ...coreStateProvenance(retained),
      activeExecutionCheckpoint: {
        ...pending,
        activeExecutions: retained.activeExecutions,
        executionStatuses: retained.executionStatuses,
      },
    },
  });
}

interface CheckpointMetadataDecision {
  readonly acceptsIdentity: boolean;
  readonly throughSequence: number | undefined;
  readonly blockedBySnapshot: boolean;
  readonly publishesMetadata: boolean;
}

function checkpointMetadataDecision(
  state: CoreState,
  checkpoint: ActiveExecutionCheckpointFold | undefined,
  batchAcceptsMetadata: boolean,
  throughSequence: number | undefined,
  startsNewTransaction = false,
): CheckpointMetadataDecision {
  const pending = coreStateProvenance(state).activeExecutionCheckpoint;
  const continuesPending =
    !startsNewTransaction &&
    (checkpoint?.kind === 'complete' ||
      (checkpoint?.kind === 'deferred' && pending?.checkpointId === checkpoint.checkpointId));
  const acceptsIdentity = continuesPending
    ? (pending?.acceptsMetadata ?? false) && batchAcceptsMetadata
    : batchAcceptsMetadata;
  const candidateThrough = continuesPending
    ? (throughSequence ?? pending?.throughSequence)
    : throughSequence;
  const snapshotSequence = startsNewTransaction
    ? undefined
    : coreStateProvenance(state).activeExecutionSnapshotSequence;
  const blockedBySnapshot =
    snapshotSequence !== undefined &&
    (candidateThrough === undefined || candidateThrough <= snapshotSequence);
  return {
    acceptsIdentity,
    throughSequence: candidateThrough,
    blockedBySnapshot,
    publishesMetadata: acceptsIdentity && !blockedBySnapshot,
  };
}

function batchThroughSequence(
  events: readonly RunEvent[],
  throughSequence: number | undefined,
): number | undefined {
  return throughSequence ?? events.at(-1)?.sequence;
}

/** Keep a snapshot's liveness while still accepting event-derived status detail. */
function preserveSnapshotExecutionLiveness(
  before: CoreState,
  after: CoreState,
  metadata: CheckpointMetadataDecision,
): CoreState {
  if (!metadata.blockedBySnapshot) return after;
  return cloneCoreStateWith(after, {
    activeExecutions: before.activeExecutions,
    executionStatuses: reconcileExecutionStatuses(after.executionStatuses, before.activeExecutions),
  });
}

/** Remove the snapshot fence only after a genuinely newer checkpoint commits. */
function finishExecutionCheckpoint(state: CoreState, checkpointApplied: boolean): CoreState {
  let next = withoutDeferredExecutionCheckpoint(state);
  if (!checkpointApplied) return next;
  const provenance = coreStateProvenance(next);
  if (provenance.activeExecutionSnapshotSequence === undefined) return next;
  const {activeExecutionSnapshotSequence: _settled, ...remaining} = provenance;
  next = cloneCoreStateWith(next, {provenance: remaining});
  return next;
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
  const previousSnapshotSequence = coreStateProvenance(registered).activeExecutionSnapshotSequence;
  if (snapshotCannotAdvanceLiveness(registered, snapshot.sequence, previousSnapshotSequence)) {
    return publishCoreState(registered);
  }
  const pending = coreStateProvenance(registered).activeExecutionCheckpoint;
  const supersedesPending =
    pending !== undefined && snapshot.sequence >= (pending.throughSequence ?? registered.sequence);
  const snapshotBase = supersedesPending
    ? settleDeferredExecutionCheckpoint(registered)
    : registered;
  const activeExecutions = activeExecutionsFromCheckpoint(snapshot.active_executions ?? []);
  const executionStatuses = supersedesPending
    ? reconcileExecutionStatuses(snapshotBase.executionStatuses, activeExecutions)
    : snapshotBase.executionStatuses;
  let next = cloneCoreStateWith(snapshotBase, {
    status: snapshot.status,
    agentKind: snapshot.agent_kind ?? null,
    roundLabel: snapshot.round_label ?? null,
    activeExecutions,
    executionStatuses,
    provenance: {
      ...coreStateProvenance(snapshotBase),
      activeExecutionSnapshotSequence: Math.max(previousSnapshotSequence ?? 0, snapshot.sequence),
    },
  });
  if (supersedesPending && pending !== undefined) {
    next = cloneCoreStateWith(next, {
      provenance: {
        ...coreStateProvenance(next),
        activeExecutionCheckpoint: {
          checkpointId: pending.checkpointId,
          throughSequence: pending.throughSequence,
          acceptsMetadata: pending.acceptsMetadata,
          activeExecutions,
          executionStatuses,
        },
      },
    });
  }
  return publishCoreState(next);
}

function snapshotCannotAdvanceLiveness(
  state: CoreState,
  sequence: number,
  previousSnapshotSequence: number | undefined,
): boolean {
  if (hasRunEnded(state) && sequence <= state.sequence) return true;
  return previousSnapshotSequence !== undefined && sequence < previousSnapshotSequence;
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
  activeExecutions?: ActiveExecutionCheckpoint | null,
  throughSequence?: number,
  historyAfterSequence?: number,
  activeExecutionCheckpoint?: ActiveExecutionCheckpointFold,
): CoreState {
  if (activeExecutionCheckpoint?.kind === 'deferred' && activeExecutions !== null) {
    throw new Error('A deferred active-execution fold requires a null checkpoint');
  }
  if (activeExecutionCheckpoint?.kind === 'complete' && activeExecutions == null) {
    throw new Error('A completed active-execution fold requires an authoritative checkpoint');
  }
  const working =
    activeExecutionCheckpoint === undefined
      ? settleDeferredExecutionCheckpoint(state)
      : checkpointWorkingState(state, activeExecutionCheckpoint, false);
  const batch = foldIdentityAwareBatch(working, events);
  const retained = retainReplayDeliveries(working, batch.state, batch.retainedDeliveries);
  if (!batch.acceptsMetadata && activeExecutionCheckpoint === undefined) {
    return publishCoreState(retained);
  }
  const metadata = checkpointMetadataDecision(
    state,
    activeExecutionCheckpoint,
    batch.acceptsMetadata,
    batchThroughSequence(events, throughSequence),
  );
  const snapshotSafe = preserveSnapshotExecutionLiveness(working, retained, metadata);
  const reduced =
    !metadata.publishesMetadata || historyAfterSequence === undefined
      ? snapshotSafe
      : cloneCoreStateWith(snapshotSafe, {historyAfterSequence});
  if (activeExecutionCheckpoint?.kind === 'deferred') {
    return publishCoreState(
      deferExecutionCheckpoint(
        state,
        reduced,
        activeExecutionCheckpoint.checkpointId,
        metadata.acceptsIdentity,
        metadata.throughSequence,
      ),
    );
  }
  const checkpointApplied =
    activeExecutions != null &&
    metadata.publishesMetadata &&
    (metadata.throughSequence === undefined || metadata.throughSequence >= reduced.sequence);
  const reconciled = !checkpointApplied
    ? reduced
    : reconcileActiveExecutions(reduced, activeExecutions, metadata.throughSequence);
  return publishCoreState(finishExecutionCheckpoint(reconciled, checkpointApplied));
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
  activeExecutions: ActiveExecutionCheckpoint | null | undefined,
  throughSequence: number | undefined,
  historyAfterSequence: number,
  activeExecutionCheckpoint?: ActiveExecutionCheckpointFold,
): CoreState {
  if (activeExecutionCheckpoint?.kind === 'deferred' && activeExecutions !== null) {
    throw new Error('A deferred active-execution fold requires a null checkpoint');
  }
  if (activeExecutionCheckpoint?.kind === 'complete' && activeExecutions == null) {
    throw new Error('A completed active-execution fold requires an authoritative checkpoint');
  }
  // Null is the wire's explicit "authoritative checkpoint follows in
  // fragments" marker. Seed the fresh replay with the last complete execution
  // checkpoint so its event prefix may update that state without exposing a
  // transient empty list. Status is event-derived and belongs to the old
  // sequence space, so only replayed status events rebuild it. Omitted legacy
  // data retains the old fresh-fold behavior; a list, including [], replaces
  // after the replay.
  const empty =
    activeExecutionCheckpoint === undefined
      ? activeExecutions === null
        ? cloneCoreStateWith(initialCoreState(), {
            activeExecutions: state.activeExecutions,
          })
        : initialCoreState()
      : checkpointWorkingState(state, activeExecutionCheckpoint, true);
  const batch = foldIdentityAwareBatch(empty, events);
  let reduced = retainReplayDeliveries(empty, batch.state, batch.retainedDeliveries);
  const metadata = checkpointMetadataDecision(
    state,
    activeExecutionCheckpoint,
    batch.acceptsMetadata,
    batchThroughSequence(events, throughSequence),
    true,
  );
  if (metadata.publishesMetadata) {
    reduced = cloneCoreStateWith(reduced, {historyAfterSequence});
    if (activeExecutions !== undefined && activeExecutions !== null) {
      reduced = reconcileActiveExecutions(reduced, activeExecutions, throughSequence);
    }
  }
  // The snapshot registry is valid across a store swap only when the replay
  // proves it still describes the same run. Unknown or mixed identity resets
  // it rather than relabeling another run's threads.
  const registered =
    state.runId === null || reduced.runId !== state.runId || !batch.acceptsMetadata
      ? reduced
      : retainRegisteredChatThreads(reduced, state.chatThreads);
  if (activeExecutionCheckpoint?.kind === 'deferred') {
    return publishCoreState(
      deferExecutionCheckpoint(
        state,
        registered,
        activeExecutionCheckpoint.checkpointId,
        metadata.acceptsIdentity,
        metadata.throughSequence,
      ),
    );
  }
  return publishCoreState(withoutDeferredExecutionCheckpoint(registered));
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
  const provenance = coreStateProvenance(state);
  const pending = provenance.activeExecutionCheckpoint;
  const prefixAcceptsMetadata = acceptsRunMetadata(state.runId, events);
  const deliveries = [...ownedReplayEvents(events, 'stream'), ...retainedReplayEvents(state)].sort(
    compareReplayDeliveries,
  );
  const empty = cloneCoreStateWith(initialCoreState(), {runId: state.runId});
  const replay = foldReplayDeliveries(empty, deliveries);
  let reduced = retainRegisteredChatThreads(replay.state, state.chatThreads);
  const replayedStatuses = reduced.executionStatuses;
  let snapshotSafeHiddenStatuses: CoreState['executionStatuses'] | undefined;
  if (pending !== undefined) {
    const hiddenStatuses = mergeExecutionStatuses(replayedStatuses, pending.executionStatuses);
    snapshotSafeHiddenStatuses =
      provenance.activeExecutionSnapshotSequence === undefined
        ? hiddenStatuses
        : reconcileExecutionStatuses(hiddenStatuses, pending.activeExecutions);
  }
  reduced = cloneCoreStateWith(reduced, {
    runId: state.runId ?? replay.state.runId,
    sequence: state.sequence,
    foldedOutOfBand: state.foldedOutOfBand,
    status: state.status,
    agentKind: state.agentKind ?? replay.state.agentKind,
    roundLabel: state.roundLabel ?? replay.state.roundLabel,
    activeExecutions: state.activeExecutions,
    executionStatuses:
      pending === undefined
        ? reconcileExecutionStatuses(replayedStatuses, state.activeExecutions)
        : state.executionStatuses,
    historyAfterSequence: prefixAcceptsMetadata ? historyAfterSequence : state.historyAfterSequence,
    provenance: {
      ...coreStateProvenance(reduced),
      ...(provenance.activeExecutionSnapshotSequence === undefined
        ? {}
        : {activeExecutionSnapshotSequence: provenance.activeExecutionSnapshotSequence}),
      ...(pending === undefined
        ? {}
        : {
            activeExecutionCheckpoint: {
              ...pending,
              executionStatuses: snapshotSafeHiddenStatuses ?? pending.executionStatuses,
            },
          }),
    },
  });
  replaceReplayEvents(reduced, replay.retainedDeliveries);
  return publishCoreState(reduced);
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
  if (coreStateProvenance(state).activeExecutionCheckpoint !== undefined) {
    return publishCoreState(foldRoutedEventsDuringCheckpoint(state, [event], 'stream'));
  }
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
  return publishCoreState(foldRoutedEventsDuringCheckpoint(state, events, 'response'));
}

/** Merge status replay with staged status, keeping the newest event per execution. */
function mergeExecutionStatuses(
  replayed: CoreState['executionStatuses'],
  staged: CoreState['executionStatuses'],
): CoreState['executionStatuses'] {
  let merged = replayed;
  for (const [executionId, status] of Object.entries(staged)) {
    const current = merged[executionId];
    if (current !== undefined && current.sequence > status.sequence) continue;
    if (merged === replayed) merged = {...replayed};
    (merged as Record<string, typeof status>)[executionId] = status;
  }
  return merged;
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
