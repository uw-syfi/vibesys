/**
 * Steer delivery in the run fold: the steps of an operator steer's life (queued, reached a turn,
 * in force, dropped, ended, refused), each carrying the control id the operator sent it under.
 */
import type {RunEvent} from '@vibesys/contracts';
import {type CoreState, cloneCoreStateWith, type SteerDeliveryFact} from './state.js';

/** The fact a `steer_delivery` event carries, or null for any other event. */
function steerDeliveryOf(event: RunEvent, sequence: number): SteerDeliveryFact | null {
  const data = event.data;
  if (data?.kind !== 'steer_delivery') return null;
  const delivery = data.delivery;
  return {
    sequence,
    step: delivery.state,
    controlId: delivery.control_id ?? null,
    action: delivery.action ?? null,
    withdraws: delivery.withdraws ?? null,
    role: delivery.role ?? null,
    attemptId: delivery.attempt_id ?? null,
    reason: delivery.reason ?? null,
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
