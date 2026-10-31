/**
 * What a window knows of a subscription: main's control state and its own fold of the data
 * updates, folded by the same reducer as main's. The screen's state is derived from the two, so it
 * has a value only while main says `live` or `stale`, and a broken fold is a failure, never a value.
 */
import {EMPTY_FOLD, type Fold, foldUpdate, type SubscriptionUpdate, snapshotValue} from './fold.js';
import {CONNECTING, type Control, type SubscriptionState} from './state.js';
import type {Topic} from './topics.js';

export interface WindowState {
  readonly control: Control;
  readonly fold: Fold;
}

export const INITIAL_WINDOW: WindowState = {control: CONNECTING, fold: EMPTY_FOLD};

export function applyUpdate(
  topic: Topic<unknown>,
  state: WindowState,
  update: SubscriptionUpdate,
): WindowState {
  if (update.kind === 'control') return {...state, control: update.control};
  return {...state, fold: foldUpdate(topic, state.fold, update)};
}

const INVALID: SubscriptionState<never> = {
  kind: 'failed',
  reason: 'invalid_frame',
  retrying: false,
};

/** The state a screen reads. */
export function stateOf<S>(topic: Topic<S>, state: WindowState): SubscriptionState<S> {
  const {control, fold} = state;
  if (fold.broken) return INVALID;
  if (control.kind !== 'live' && control.kind !== 'stale') return control;
  const value = snapshotValue(topic, fold);
  // Main publishes a snapshot before it says `live`, so a value is there unless the data broke.
  if (value === null) return INVALID;
  return control.kind === 'live'
    ? {kind: 'live', value, since: control.since}
    : {
        kind: 'stale',
        value,
        reason: control.reason,
        attempt: control.attempt,
        valueAt: control.valueAt,
      };
}

export function sameControl(left: Control, right: Control): boolean {
  switch (left.kind) {
    case 'connecting':
      return (
        right.kind === 'connecting' && left.step === right.step && left.attempt === right.attempt
      );
    case 'live':
      return right.kind === 'live' && left.since === right.since;
    case 'stale':
      return (
        right.kind === 'stale' &&
        left.reason === right.reason &&
        left.attempt === right.attempt &&
        left.valueAt === right.valueAt
      );
    case 'failed':
      return (
        right.kind === 'failed' && left.reason === right.reason && left.retrying === right.retrying
      );
  }
}
