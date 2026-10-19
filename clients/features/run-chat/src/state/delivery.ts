/**
 * Where a sent steer is: a fold over the delivery events the server reports for its control id.
 * Events arrive in any order and more than once; the fold only moves forward (sent, waiting,
 * delivered, then one of the endings) and an ending is final, so a card never goes back.
 *
 * `deliveriesOf` is the one place wire events become delivery events. The server does not
 * publish steer delivery yet (#3224, slice A), so it reads none today; wiring the real events is
 * a change to it alone, and fixtures drive the fold directly meanwhile.
 */
import type {CoreState} from '@vibesys/run-state';

/** What the server says happened to a control, each carrying the control's id. */
export type DeliveryEvent =
  /** Queued for the target's next turn. */
  | {readonly kind: 'queued'}
  /** Reached the target: consumed by a turn, or delivered into one already running. */
  | {readonly kind: 'delivered'; readonly idea: number | null}
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
  | {readonly kind: 'delivered'; readonly idea: number | null}
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
      return advance(state, {kind: 'delivered', idea: event.idea});
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

/**
 * The deliveries the run's events report. The wire has no steer delivery events yet, so this
 * reads none; it is the single seam where they will be mapped.
 */
export function deliveriesOf(_core: CoreState): readonly ControlDelivery[] {
  return [];
}
