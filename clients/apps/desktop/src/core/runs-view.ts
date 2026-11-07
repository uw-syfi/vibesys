/**
 * The run stream: every run of the host with its summary, kept up to date while a screen watches. It
 * asks for the snapshot of detail SUMMARY, so the host lists every run and sends no mailbox with it; a
 * run's history is read when a screen of the run opens (`run-history.ts`).
 */
import {SnapshotDetail} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  applyHostFrame,
  beginRunsStream,
  type FeedStatus,
  type Freshness,
  freshnessOf,
  type HostFrame,
  type HostModel,
  runsCursor,
} from '../state/index.js';
import {type FeedSpec, runFeed} from './feeds.js';
import type {HostParts} from './host-store.js';
import {makeRetryGate} from './retry-gate.js';

export interface RunsView {
  /** Mount this (React: `useAtomMount`) to follow the run stream; unmounting closes the stream. */
  readonly watch: Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  readonly status: Atom.Atom<FeedStatus>;
  readonly freshness: Atom.Atom<Freshness>;
  /** The user asked to retry a failed run stream. */
  readonly retry: Effect.Effect<void>;
}

const runsSpec = ({client}: HostParts): FeedSpec<HostFrame> => ({
  open: model =>
    client
      .watchRuns({afterSequence: runsCursor(model), detail: SnapshotDetail.SUMMARY})
      .pipe(Stream.map(response => ({from: 'runs', body: response.body}) as const)),
  begin: beginRunsStream,
  apply: applyHostFrame,
  gapped: model => model.data.gap !== null,
});

/** Whether the run stream has told this client anything yet. */
const hasRuns = (model: HostModel): boolean => model.data.sequence !== 0n;

export const makeRunsView = Effect.fn('RunsView.make')(function* (parts: HostParts) {
  const {connection, store} = parts;
  const status = yield* SubscriptionRef.make<FeedStatus>({_tag: 'Opening'});
  const gate = yield* makeRetryGate;
  const ports = {
    connection,
    model: store.ref,
    setStatus: (next: FeedStatus) => SubscriptionRef.set(status, next),
    awaitRetry: gate.wait,
  };
  const statusAtom = Atom.keepAlive(Atom.subscriptionRef(status));
  const view: RunsView = {
    watch: Atom.make(
      runFeed(ports, runsSpec(parts)).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting ends the feed; the status then says nothing is being followed.
        Effect.ensuring(SubscriptionRef.set(status, {_tag: 'Opening'})),
      ),
    ),
    status: statusAtom,
    freshness: Atom.make(get =>
      freshnessOf(get(connection.state), get(statusAtom), hasRuns(get(store.atom))),
    ),
    retry: gate.open,
  };
  return view;
});
