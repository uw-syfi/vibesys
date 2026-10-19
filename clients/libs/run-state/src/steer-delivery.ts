/**
 * Steer delivery in the run fold: the steps of an operator steer's life (queued, reached a turn,
 * in force, dropped, ended, refused), each carrying the control id the operator sent it under.
 *
 * The contract for `steer_delivery` is not on this branch yet (#3224, slice A), so the event's
 * data is read structurally here, and only here, from the shape that slice publishes
 * (`SteerDeliveryData.delivery`). When its generated type lands, this module reads the typed
 * data and the kind moves into the exhaustive table of `event-data.ts`; nothing else changes.
 */
import type {RunEvent} from '@vibesys/contracts';
import {
  type CoreState,
  cloneCoreStateWith,
  type SteerDeliveryFact,
  type SteerDeliveryStep,
} from './state.js';

const STEPS: readonly string[] = [
  'queued',
  'withdraw_queued',
  'reached',
  'reached_mid_turn',
  'in_force',
  'dropped',
  'ended',
  'refused',
];

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function isStep(value: unknown): value is SteerDeliveryStep {
  return typeof value === 'string' && STEPS.includes(value);
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null;
}

/** The fact a `steer_delivery` event carries, or null for any other event or an unreadable one. */
function steerDeliveryOf(event: RunEvent, sequence: number): SteerDeliveryFact | null {
  const data: unknown = event.data;
  if (!isRecord(data) || data['kind'] !== 'steer_delivery') return null;
  const delivery = data['delivery'];
  if (!isRecord(delivery) || !isStep(delivery['state'])) return null;
  const action = delivery['action'];
  return {
    sequence,
    step: delivery['state'],
    controlId: text(delivery['control_id']),
    action: action === 'steer' || action === 'withdraw' ? action : null,
    withdraws: text(delivery['withdraws']),
    role: text(delivery['role']),
    attemptId: text(delivery['attempt_id']),
    reason: text(delivery['reason']),
  };
}

/** Keeps a steer delivery step, once per sequence. */
export function applySteerDelivery(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const fact = steerDeliveryOf(event, sequence);
  if (fact === null) return state;
  if (fact.sequence > 0 && state.steerDeliveries.some(known => known.sequence === fact.sequence)) {
    return state;
  }
  return cloneCoreStateWith(state, {steerDeliveries: [...state.steerDeliveries, fact]});
}
