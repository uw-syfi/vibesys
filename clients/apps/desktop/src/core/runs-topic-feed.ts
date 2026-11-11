/** The gateway's `runs` topic as a thin value adapter over the shared keyed-topic lifecycle. */
import {type HostService, type RunEntry, type TopicEntry, topicPolicies} from '@vibesys/proto';
import type * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  applyKeyedTopicEvent,
  dormantKeyedTopic,
  type KeyedTopicEvent,
  type KeyedTopicState,
} from '../state/keyed-topic.js';
import type {TopicEnd} from '../state/topic-delivery.js';
import {topicRetry} from '../state/topic-delivery.js';
import type {EffectClient} from './client/make-client.js';
import type {HostConnectionApi} from './connection.js';
import {type KeyedTopicDescriptor, makeKeyedTopicWatch} from './keyed-topic-feed.js';

type HostClient = EffectClient<typeof HostService.method>;

export type RunsTopicState = KeyedTopicState<RunEntry>;

export interface RunsTopicFeed {
  /** Mount while a consumer needs the host's run index; interruption closes the stream. */
  readonly watch: Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  /** The current run index and its explicit freshness/failure state. */
  readonly state: Atom.Atom<RunsTopicState>;
}

export interface RunsTopicParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

const POLICY = topicPolicies.runs;

function entryOf(
  entry: TopicEntry,
): {readonly event: KeyedTopicEvent<RunEntry>} | {readonly invalid: string} {
  switch (entry.value.case) {
    case 'runs':
      return {event: {_tag: 'Entry', key: entry.key, value: entry.value.value}};
    case 'machineStats':
      return {invalid: 'an entry of the machine_stats topic'};
    case undefined:
      return {invalid: 'an entry with no value'};
    default:
      entry.value satisfies never;
      return {invalid: 'an entry of an unknown topic'};
  }
}

function endEvent(end: TopicEnd): KeyedTopicEvent<RunEntry> {
  switch (end._tag) {
    case 'Refused':
      return {_tag: 'Refused', refusal: end.refusal};
    case 'Silent':
      return {_tag: 'Stale', why: 'silent'};
    case 'Closed':
      return {_tag: 'Stale', why: 'closed'};
    case 'Invalid':
      return {_tag: 'Failed', failure: {_tag: 'InvalidFrame', detail: end.detail}};
    case 'Failed':
      if (end.fault.kind === 'FellBehind') return {_tag: 'Stale', why: 'fell-behind'};
      return topicRetry(end) === 'lost'
        ? {_tag: 'Stale', why: 'disconnected'}
        : {_tag: 'Failed', failure: {_tag: 'Transport', fault: end.fault}};
  }
}

export const makeRunsTopicFeed = Effect.fn('RunsTopicFeed.make')(function* (parts: RunsTopicParts) {
  const state = yield* SubscriptionRef.make<RunsTopicState>(dormantKeyedTopic());
  const apply = (event: KeyedTopicEvent<RunEntry>) =>
    SubscriptionRef.updateAndGet(state, current =>
      applyKeyedTopicEvent(current, event, POLICY.maxKeys),
    );
  const descriptor: KeyedTopicDescriptor<RunsTopicState, KeyedTopicEvent<RunEntry>> = {
    request: {topic: {case: 'runs', value: {}}},
    policy: POLICY,
    open: {_tag: 'Open'},
    synced: {_tag: 'Synced'},
    aged: null,
    stop: {_tag: 'Stop'},
    entry: entryOf,
    removed: key => ({_tag: 'Removed', key}),
    ended: endEvent,
    apply,
    invalid: current =>
      current._tag === 'Failed' && current.failure._tag === 'InvalidFrame'
        ? {_tag: 'Invalid', detail: current.failure.detail}
        : null,
  };
  const feed: RunsTopicFeed = {
    watch: makeKeyedTopicWatch(parts, descriptor),
    state: Atom.keepAlive(Atom.subscriptionRef(state)),
  };
  return feed;
});
