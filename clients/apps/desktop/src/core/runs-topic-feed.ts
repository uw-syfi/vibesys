/** The gateway's `runs` topic as a thin value adapter over the shared keyed-topic lifecycle. */
import {
  DrainGoal,
  type HostService,
  Outcome,
  type RunEntry,
  RunLiveness,
  type TopicEntry,
  topicPolicies,
} from '@vibesys/proto';
import type * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {
  GenericRunActivity,
  GenericRunEntry,
  GenericRunIndex,
  GenericRunState,
} from '../state/generic-run-index.js';
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
  /** The renderer-safe run-index facts at the last atomic publication boundary. */
  readonly index: Atom.Atom<GenericRunIndex>;
}

export interface RunsTopicParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

const POLICY = topicPolicies.runs;

const LIVENESS_STATE: Readonly<Record<RunLiveness, GenericRunState>> = {
  [RunLiveness.UNSPECIFIED]: 'unknown',
  [RunLiveness.ALIVE]: 'alive',
  [RunLiveness.INTERRUPTED]: 'interrupted',
  [RunLiveness.ENDED]: 'ended',
  [RunLiveness.PARKED]: 'parked',
};

const ENDED_STATE: Readonly<Record<Outcome, GenericRunState>> = {
  [Outcome.UNSPECIFIED]: 'unknown',
  [Outcome.FINISHED]: 'finished',
  [Outcome.FAILED]: 'failed',
  [Outcome.STOPPED]: 'stopped',
  [Outcome.INTERRUPTED]: 'interrupted',
};

/**
 * The generic lifecycle. A terminal persisted lifecycle stays authoritative; otherwise the
 * gateway's lock-derived interruption overrides a stale nonterminal head left by a crashed writer.
 */
function runState(entry: RunEntry): GenericRunState {
  const {head} = entry;
  const lifecycle = head?.lifecycle?.state;
  switch (lifecycle?.case) {
    case 'parked':
      return 'parked';
    case 'ended':
      return ENDED_STATE[lifecycle.value.outcome];
    case 'starting':
    case 'running':
    case 'draining':
    case undefined:
      break;
    default:
      lifecycle satisfies never;
  }
  if (entry.liveness === RunLiveness.INTERRUPTED) return 'interrupted';
  if (entry.liveness === RunLiveness.PARKED) return 'parked';
  if (entry.liveness === RunLiveness.ENDED) return 'ended';
  if (head?.awaitingLifecycle === true) return 'preparing';
  switch (lifecycle?.case) {
    case 'starting':
      return 'starting';
    case 'running':
      return lifecycle.value.hold === undefined ? 'running' : 'held';
    case 'draining':
      switch (lifecycle.value.goal) {
        case DrainGoal.PARK:
          return 'parking';
        case DrainGoal.STOP:
          return 'stopping';
        case DrainGoal.UNSPECIFIED:
          return 'unknown';
        default:
          lifecycle.value.goal satisfies never;
          return 'unknown';
      }
    case undefined:
      return LIVENESS_STATE[entry.liveness];
    default:
      lifecycle satisfies never;
      return 'unknown';
  }
}

const activityOf = (state: GenericRunState): GenericRunActivity => {
  switch (state) {
    case 'preparing':
    case 'starting':
    case 'running':
    case 'held':
    case 'parking':
    case 'stopping':
    case 'alive':
      return 'live';
    case 'parked':
    case 'finished':
    case 'failed':
    case 'stopped':
    case 'interrupted':
    case 'ended':
      return 'ended';
    case 'unknown':
      return 'unknown';
  }
};

function genericEntry(runId: string, entry: RunEntry): GenericRunEntry {
  const state = runState(entry);
  const activity =
    state === 'preparing' ? activityOf(LIVENESS_STATE[entry.liveness]) : activityOf(state);
  const objective = entry.head?.objective;
  const series = objective?.series;
  return {
    runId,
    projectRoot: entry.projectRoot ?? null,
    state,
    activity,
    objective:
      series === undefined
        ? null
        : {name: series.name, unit: series.unit, bestValue: objective?.bestValue ?? null},
    costUsd: entry.head?.usage?.costUsd ?? null,
    needsUser: entry.head?.needsUser ?? false,
  };
}

const ready = (
  value: ReadonlyMap<string, RunEntry>,
  freshness: 'current' | 'stale',
): GenericRunIndex => ({
  _tag: 'Ready',
  freshness,
  runs: new Map([...value].map(([runId, entry]) => [runId, genericEntry(runId, entry)])),
});

/** Select only stable renderer facts; never expose a reconnect's partial replacement snapshot. */
function genericIndex(state: RunsTopicState): GenericRunIndex {
  switch (state._tag) {
    case 'Dormant':
      return {_tag: 'Dormant'};
    case 'Connecting':
      return {_tag: 'Loading'};
    case 'Live':
      return ready(state.value, 'current');
    case 'Stale':
      return state.value === null
        ? {_tag: 'Unavailable', reason: 'stale'}
        : ready(state.value, 'stale');
    case 'Refused':
      return {_tag: 'Unavailable', reason: state.refusal};
    case 'Failed':
      return {
        _tag: 'Unavailable',
        reason: state.failure._tag === 'InvalidFrame' ? 'invalid' : 'transport',
      };
  }
}

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
  const stateAtom = Atom.keepAlive(Atom.subscriptionRef(state));
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
    state: stateAtom,
    index: Atom.make(get => genericIndex(get(stateAtom))),
  };
  return feed;
});
