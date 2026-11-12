/**
 * The run stream: every run of the host with its summary, kept up to date while a screen watches. It
 * asks for the snapshot of detail SUMMARY, so the host lists every run and sends no mailbox with it; a
 * run's history is read when a screen of the run opens (`run-history.ts`).
 */
import {SnapshotDetail} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Queue from 'effect/Queue';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {rewindRunsStream} from '../state/host-model.js';
import {
  applyHostFrame,
  beginRunsStream,
  type FeedStatus,
  type Freshness,
  freshnessOf,
  type HostFrame,
  type HostModel,
  replaceSnapshot,
  runsCursor,
} from '../state/index.js';
import {type FeedRefresh, type FeedSpec, runFeed} from './feeds.js';
import type {HostParts} from './host-store.js';
import {makeRetryGate} from './retry-gate.js';

export interface RunsView {
  /** Mount this (React: `useAtomMount`) to follow the run stream; unmounting closes the stream. */
  readonly watch: Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  readonly status: Atom.Atom<FeedStatus>;
  readonly freshness: Atom.Atom<Freshness>;
  /** The last explicit refresh request; Refreshed and Failed persist until another request. */
  readonly refreshState: Atom.Atom<RunsRefreshState>;
  /** Replace the run list from a new SUMMARY snapshot; concurrent presses coalesce. */
  readonly refresh: Effect.Effect<void>;
  /** The user asked to retry a failed run stream. */
  readonly retry: Effect.Effect<void>;
}

/** The closed lifecycle of the operator's explicit refresh, separate from feed/link freshness. */
export type RunsRefreshState =
  | {readonly _tag: 'Idle'}
  | {readonly _tag: 'Refreshing'}
  | {readonly _tag: 'Refreshed'; readonly sequence: bigint}
  | {readonly _tag: 'Failed'};

const IDLE_REFRESH: RunsRefreshState = {_tag: 'Idle'};
const REFRESHING: RunsRefreshState = {_tag: 'Refreshing'};
const FAILED_REFRESH: RunsRefreshState = {_tag: 'Failed'};

const isRunsSnapshot = (frame: HostFrame): boolean =>
  frame.from === 'runs' && frame.body.case === 'snapshot';

/** A requested snapshot replaces even a host that restarted below the sequence already shown. */
const applyRunsFrame = (model: HostModel, frame: HostFrame, replacing: boolean): HostModel =>
  replacing && frame.from === 'runs' && frame.body.case === 'snapshot'
    ? {...model, data: replaceSnapshot(model.data, frame.body.value)}
    : applyHostFrame(model, frame);

const runsSpec = ({client}: HostParts): FeedSpec<HostFrame> => ({
  open: (model, _firstOpen, replacing) =>
    client
      .watchRuns({
        afterSequence: replacing ? 0n : runsCursor(model),
        detail: SnapshotDetail.SUMMARY,
      })
      .pipe(Stream.map(response => ({from: 'runs', body: response.body}) as const)),
  begin: beginRunsStream,
  apply: applyRunsFrame,
  gapped: model => model.data.gap !== null,
  rewind: rewindRunsStream,
});

/** Whether the run stream has told this client anything yet. */
const hasRuns = (model: HostModel): boolean => model.data.sequence !== 0n;

export const makeRunsView = Effect.fn('RunsView.make')(function* (parts: HostParts) {
  const {connection, store} = parts;
  const status = yield* SubscriptionRef.make<FeedStatus>({_tag: 'Opening'});
  const refreshState = yield* SubscriptionRef.make<RunsRefreshState>(IDLE_REFRESH);
  const refreshRequests = yield* Queue.sliding<true>(1);
  const gate = yield* makeRetryGate;

  const activateRefresh = Effect.fn('RunsView.activateRefresh')(function* () {
    // Each is a no-op unless its owner is waiting. `leftTerminal` closes the race in which the
    // request reached a terminal connection just before that connection observed its retry.
    yield* connection.retry;
    yield* gate.open;
    yield* connection.leftTerminal;
  });
  const refreshSucceeded = Effect.fn('RunsView.refreshSucceeded')(function* (frame: HostFrame) {
    if (frame.from !== 'runs' || frame.body.case !== 'snapshot') {
      return yield* Effect.die(new Error('a runs refresh completed without a snapshot'));
    }
    yield* SubscriptionRef.set(refreshState, {
      _tag: 'Refreshed',
      sequence: frame.body.value.sequence,
    });
  });
  const refreshControl: FeedRefresh<HostFrame> = {
    requests: refreshRequests,
    pending: SubscriptionRef.get(refreshState).pipe(
      Effect.map(state => state._tag === 'Refreshing'),
    ),
    activate: activateRefresh(),
    isReplacement: isRunsSnapshot,
    succeeded: refreshSucceeded,
    failed: SubscriptionRef.set(refreshState, FAILED_REFRESH),
  };

  const requestRefresh = Effect.fn('RunsView.refresh')(function* () {
    const begin = yield* SubscriptionRef.modify(
      refreshState,
      (state): [boolean, RunsRefreshState] =>
        state._tag === 'Refreshing' ? [false, state] : [true, REFRESHING],
    );
    if (begin) yield* Queue.offer(refreshRequests, true);
  });
  const ports = {
    connection,
    model: store.ref,
    setStatus: (next: FeedStatus) => SubscriptionRef.set(status, next),
    awaitRetry: gate.wait,
  };
  const statusAtom = Atom.keepAlive(Atom.subscriptionRef(status));
  const refreshStateAtom = Atom.keepAlive(Atom.subscriptionRef(refreshState));
  const view: RunsView = {
    watch: Atom.make(
      runFeed(ports, runsSpec(parts), refreshControl).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting ends the feed; the status then says nothing is being followed.
        Effect.ensuring(SubscriptionRef.set(status, {_tag: 'Opening'})),
      ),
    ),
    status: statusAtom,
    freshness: Atom.make(get =>
      freshnessOf(get(connection.state), get(statusAtom), hasRuns(get(store.atom))),
    ),
    refreshState: refreshStateAtom,
    refresh: Effect.uninterruptible(requestRefresh()),
    retry: gate.open,
  };
  return view;
});
