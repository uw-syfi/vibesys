/**
 * Replay provenance: the deliveries the fold retains so a prefix or a rebootstrap can refold the
 * events it owns, in sequence order, without double-counting a stream and a response delivery.
 */
import type {RunEvent} from '@vibesys/contracts';
import {
  appendPersistentArrayEntry,
  materializePersistentArray,
  persistentArrayAt,
  replacePersistentArrayEntry,
} from './persistent-array.js';
import {ownProjectionInput} from './publication.js';
import {
  type CoreState,
  coreStateProvenance,
  type DeliveryRoute,
  emptyReplayProvenance,
  type MutableCoreState,
  type ReplayDelivery,
  type ReplayProvenance,
} from './state.js';

/** Whether every event that names a run names `runId`, or the first such run when none is owned. */
export function acceptsRunMetadata(runId: string | null, events: readonly RunEvent[]): boolean {
  let owned = runId;
  for (const event of events) {
    const incoming = event.run_id;
    if (incoming === undefined || incoming === null || incoming === '') continue;
    if (owned === null) owned = incoming;
    else if (owned !== incoming) return false;
  }
  return true;
}

export function retainReplayDeliveries(
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

export function ownedReplayEvents(
  events: readonly RunEvent[],
  route: DeliveryRoute,
): ReplayDelivery[] {
  return events.map(event => ({event: ownProjectionInput(event) as RunEvent, route}));
}

export function retainedReplayEvents(state: CoreState): ReplayDelivery[] {
  return materializePersistentArray(coreStateProvenance(state).replay.deliveries);
}

export function replaceReplayEvents(state: CoreState, deliveries: readonly ReplayDelivery[]): void {
  let replay = emptyReplayProvenance();
  for (const delivery of deliveries) replay = retainReplayDelivery(replay, delivery);
  (state as MutableCoreState).provenance = {
    ...coreStateProvenance(state),
    replay,
  };
}

export function compareReplayDeliveries(left: ReplayDelivery, right: ReplayDelivery): number {
  return (left.event.sequence ?? 0) - (right.event.sequence ?? 0);
}
