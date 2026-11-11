import type {Fault} from './fault.js';
import type {TopicRefusal} from './topic-delivery.js';

/** Why a formerly live value is no longer current while the feed can recover. */
type TopicStaleReason = 'refreshing' | 'silent' | 'closed' | 'disconnected' | 'fell-behind';

/** A failure that a fresh stream on the same link cannot repair. */
type TopicFailure =
  | {readonly _tag: 'InvalidFrame'; readonly detail: string}
  | {readonly _tag: 'Transport'; readonly fault: Fault};

/**
 * The current state of one keyed latest-value topic. A stream builds a private snapshot in
 * `Connecting`, or in `Stale.snapshot` during a refresh. Only `Synced` publishes that snapshot,
 * so a partial reconnect can neither hide nor retain a run by accident.
 */
export type KeyedTopicState<V> =
  | {readonly _tag: 'Dormant'}
  | {readonly _tag: 'Connecting'; readonly snapshot: ReadonlyMap<string, V>}
  | {readonly _tag: 'Live'; readonly value: ReadonlyMap<string, V>}
  | {
      readonly _tag: 'Stale';
      readonly value: ReadonlyMap<string, V> | null;
      readonly why: TopicStaleReason;
      /** A replacement snapshot being read; null while no stream is open. */
      readonly snapshot: ReadonlyMap<string, V> | null;
    }
  | {readonly _tag: 'Refused'; readonly refusal: TopicRefusal}
  | {readonly _tag: 'Failed'; readonly failure: TopicFailure};

export type KeyedTopicEvent<V> =
  | {readonly _tag: 'Open'}
  | {readonly _tag: 'Entry'; readonly key: string; readonly value: V}
  | {readonly _tag: 'Removed'; readonly key: string}
  | {readonly _tag: 'Synced'}
  | {readonly _tag: 'Stale'; readonly why: Exclude<TopicStaleReason, 'refreshing'>}
  | {readonly _tag: 'Refused'; readonly refusal: TopicRefusal}
  | {readonly _tag: 'Failed'; readonly failure: TopicFailure}
  | {readonly _tag: 'Stop'};

const DORMANT = {_tag: 'Dormant'} as const;

export const dormantKeyedTopic = <V>(): KeyedTopicState<V> => DORMANT;

const changed = <V>(
  values: ReadonlyMap<string, V>,
  event: Extract<KeyedTopicEvent<V>, {readonly _tag: 'Entry' | 'Removed'}>,
  maxKeys: number,
): ReadonlyMap<string, V> | null => {
  const next = new Map(values);
  if (event._tag === 'Entry') next.set(event.key, event.value);
  else next.delete(event.key);
  return next.size > maxKeys ? null : next;
};

const invalidBound = (maxKeys: number): KeyedTopicState<never> => ({
  _tag: 'Failed',
  failure: {
    _tag: 'InvalidFrame',
    detail: `more than ${maxKeys} ${maxKeys === 1 ? 'key' : 'keys'}`,
  },
});

function applyChange<V>(
  state: KeyedTopicState<V>,
  event: Extract<KeyedTopicEvent<V>, {readonly _tag: 'Entry' | 'Removed'}>,
  maxKeys: number,
): KeyedTopicState<V> {
  switch (state._tag) {
    case 'Connecting': {
      const snapshot = changed(state.snapshot, event, maxKeys);
      return snapshot === null ? invalidBound(maxKeys) : {...state, snapshot};
    }
    case 'Live': {
      const value = changed(state.value, event, maxKeys);
      return value === null ? invalidBound(maxKeys) : {...state, value};
    }
    case 'Stale': {
      if (state.snapshot === null) return state;
      const snapshot = changed(state.snapshot, event, maxKeys);
      return snapshot === null ? invalidBound(maxKeys) : {...state, snapshot};
    }
    case 'Dormant':
    case 'Refused':
    case 'Failed':
      return state;
  }
}

/** Fold one validated delivery event into the topic's immutable state. */
export function applyKeyedTopicEvent<V>(
  state: KeyedTopicState<V>,
  event: KeyedTopicEvent<V>,
  maxKeys: number,
): KeyedTopicState<V> {
  switch (event._tag) {
    case 'Stop':
      return dormantKeyedTopic();
    case 'Open':
      return state._tag === 'Live' || (state._tag === 'Stale' && state.value !== null)
        ? {
            _tag: 'Stale',
            value: state.value,
            why: 'refreshing',
            snapshot: new Map(),
          }
        : {_tag: 'Connecting', snapshot: new Map()};
    case 'Entry':
    case 'Removed':
      return applyChange(state, event, maxKeys);
    case 'Synced':
      if (state._tag === 'Connecting') return {_tag: 'Live', value: state.snapshot};
      return state._tag === 'Stale' && state.snapshot !== null
        ? {_tag: 'Live', value: state.snapshot}
        : state;
    case 'Stale': {
      const value =
        state._tag === 'Live' ? state.value : state._tag === 'Stale' ? state.value : null;
      return {_tag: 'Stale', value, why: event.why, snapshot: null};
    }
    case 'Refused':
      return {_tag: 'Refused', refusal: event.refusal};
    case 'Failed':
      return {_tag: 'Failed', failure: event.failure};
  }
}
