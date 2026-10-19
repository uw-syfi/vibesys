/**
 * Where a sent steer is: a fold over the delivery events the server reports for its control id.
 * Events arrive in any order and more than once; the fold only moves forward (sent, waiting,
 * delivered, then one of the endings) and an ending is final, so a card never goes back.
 *
 * `deliveriesOf` is the one place the server's steer delivery steps become delivery events.
 */
import {roleName} from '@vibesys/contracts';
import type {CoreState, SteerDeliveryFact} from '@vibesys/run-state';

/** What the server says happened to a control, each carrying the control's id. */
export type DeliveryEvent =
  /** Queued for the target's next turn. */
  | {readonly kind: 'queued'}
  /** Reached the target: consumed by a turn, or delivered into one already running. */
  | {
      readonly kind: 'delivered';
      /** The idea (its number in the run) whose turn took it, when the caller can say. */
      readonly idea: number | null;
      /** The role or attempt whose turn took it. */
      readonly where: string | null;
    }
  /** Dropped before it reached a turn (the target ended first). */
  | {readonly kind: 'dropped'}
  /** A standing steer's guidance ended with its target. */
  | {readonly kind: 'ended'}
  | {readonly kind: 'withdrawn'}
  | {readonly kind: 'refused'; readonly reason: string | null};

export interface ControlDelivery {
  readonly controlId: string;
  readonly event: DeliveryEvent;
}

export type DeliveryState =
  | {readonly kind: 'sent'}
  | {readonly kind: 'waiting'}
  | {readonly kind: 'delivered'; readonly idea: number | null; readonly where: string | null}
  | {readonly kind: 'dropped'}
  | {readonly kind: 'ended'}
  | {readonly kind: 'withdrawn'}
  | {readonly kind: 'refused'; readonly reason: string | null};

export const SENT: DeliveryState = {kind: 'sent'};

const RANK: Readonly<Record<DeliveryState['kind'], number>> = {
  sent: 0,
  waiting: 1,
  delivered: 2,
  dropped: 3,
  ended: 3,
  withdrawn: 3,
  refused: 3,
};

/** Whether the delivery is over: no later event changes it. */
export function deliveryEnded(state: DeliveryState): boolean {
  return RANK[state.kind] === 3;
}

function advance(state: DeliveryState, next: DeliveryState): DeliveryState {
  return RANK[next.kind] > RANK[state.kind] ? next : state;
}

/** The delivery after `event`; an event that would move it back changes nothing. */
export function foldDelivery(state: DeliveryState, event: DeliveryEvent): DeliveryState {
  switch (event.kind) {
    case 'queued':
      return advance(state, {kind: 'waiting'});
    case 'delivered':
      return advance(state, {kind: 'delivered', idea: event.idea, where: event.where});
    case 'dropped':
      // A steer that reached a turn was not dropped.
      return state.kind === 'delivered' ? state : advance(state, {kind: 'dropped'});
    case 'ended':
      return advance(state, {kind: 'ended'});
    case 'withdrawn':
      return advance(state, {kind: 'withdrawn'});
    case 'refused':
      // A steer that reached a turn cannot be refused afterwards.
      return state.kind === 'delivered'
        ? state
        : advance(state, {kind: 'refused', reason: event.reason});
  }
}

/** The number of the idea an attempt belongs to, as the run screen counts them; null if unknown. */
export type IdeaOf = (attemptId: string) => number | null;

/** The delivery a server step means for the steer card it names; null for a step no card shows. */
function deliveryOfFact(fact: SteerDeliveryFact, ideaOf: IdeaOf): ControlDelivery | null {
  if (fact.controlId === null) return null;
  const controlId = fact.controlId;
  switch (fact.step) {
    case 'queued':
      return fact.action === 'withdraw' ? null : {controlId, event: {kind: 'queued'}};
    case 'withdraw_queued':
      // The card already shows its withdrawal as requested.
      return null;
    case 'reached':
    case 'reached_mid_turn':
    case 'in_force':
      return {
        controlId,
        event: {
          kind: 'delivered',
          idea: fact.attemptId === null ? null : (ideaOf(fact.attemptId) ?? null),
          where: fact.role === null ? fact.attemptId : roleName(fact.role),
        },
      };
    case 'dropped':
      return {controlId, event: {kind: 'dropped'}};
    case 'ended':
      return {
        controlId: fact.withdraws ?? controlId,
        event: {kind: fact.reason === 'withdrawn' ? 'withdrawn' : 'ended'},
      };
    case 'refused':
      return {controlId, event: {kind: 'refused', reason: fact.reason}};
  }
}

/**
 * The deliveries the run's events report, in journal order. This is the single place the server's
 * `steer_delivery` steps become delivery events; the fold itself is `run-state`'s.
 */
export function deliveriesOf(
  core: CoreState,
  ideaOf: IdeaOf = () => null,
): readonly ControlDelivery[] {
  return core.steerDeliveries.flatMap(fact => deliveryOfFact(fact, ideaOf) ?? []);
}
