/**
 * The host's cost stream: the whole cost view of every run the host holds, which the host works out,
 * then a whole view again each time any run's cost changes, while a screen watches it. The client sums
 * nothing: each message is read into a view (`readHostCost`), which is stored as it arrives
 * (`applyHostCost`) and read from `HostModel.data.hostCosts` under its window. A message that lacks a
 * field the contract requires fails the stream with the field named, and the screen shows that beside
 * its status. A stream always starts with a view, so a reopen has no cursor to resume from, and each
 * window is a stream of its own, so watching two windows never mixes their numbers.
 */
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {HostCostSchema, type WatchHostCostResponse} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  applyHostFrame,
  type FeedStatus,
  type Freshness,
  freshnessOf,
  type HostCostWindow,
  type HostFrame,
  readHostCost,
  type WholeViewPhase,
  windowSinceMs,
} from '../state/index.js';
import {InvalidMessage} from './errors.js';
import {type FeedSpec, runFeed} from './feeds.js';
import type {HostParts} from './host-store.js';
import {makeRetryGate, type RetryGate} from './retry-gate.js';

export interface HostCostFeed {
  /** Mount this to follow the host's cost in one window; unmounting closes its stream. Mounting twice shares one. */
  readonly watch: (window: HostCostWindow) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  readonly status: (window: HostCostWindow) => Atom.Atom<FeedStatus>;
  readonly freshness: (window: HostCostWindow) => Atom.Atom<Freshness>;
  /** The user asked to retry this window's failed cost stream. */
  readonly retry: (window: HostCostWindow) => Effect.Effect<void>;
}

type Statuses = Readonly<Partial<Record<HostCostWindow, FeedStatus>>>;

const OPENING: FeedStatus = {_tag: 'Opening'};

/** A frame of the view the message carries, or the failure that names what it lacks. */
function hostCostFrame(
  window: HostCostWindow,
  response: WatchHostCostResponse,
  phase: WholeViewPhase,
): Effect.Effect<HostFrame, InvalidMessage> {
  const invalid = (detail: string) =>
    Effect.fail(new InvalidMessage({direction: 'response', type: HostCostSchema.typeName, detail}));
  if (response.cost === undefined) return invalid('cost is absent');
  const read = readHostCost(response.cost);
  return read._tag === 'Missing'
    ? invalid(`${read.field} is absent`)
    : Effect.succeed({from: 'host-cost', phase, window, view: read.view});
}

const hostCostSpec = ({client}: HostParts, window: HostCostWindow): FeedSpec<HostFrame> => {
  const sinceMs = windowSinceMs(window);
  return {
    open: () =>
      client.watchHostCost(sinceMs === null ? {} : {since: timestampFromMs(sinceMs)}).pipe(
        Stream.zipWithIndex,
        Stream.mapEffect(([response, index]) =>
          hostCostFrame(window, response, index === 0 ? 'initial' : 'update'),
        ),
      ),
    apply: applyHostFrame,
    gapped: () => false,
  };
};

export const makeHostCostFeed = Effect.fn('HostCostFeed.make')(function* (parts: HostParts) {
  const {connection, store} = parts;
  const statuses = yield* SubscriptionRef.make<Statuses>({});
  const statusesAtom = Atom.keepAlive(Atom.subscriptionRef(statuses));
  /** The gate of each mounted window's feed: there while it is mounted, gone when it unmounts. */
  const gates = new Map<HostCostWindow, RetryGate>();
  const forgetWindow = (window: HostCostWindow) =>
    Effect.suspend(() => {
      gates.delete(window);
      return SubscriptionRef.update(statuses, ({[window]: _gone, ...rest}) => rest);
    });

  const status = Atom.family((window: HostCostWindow) =>
    Atom.make(get => get(statusesAtom)[window] ?? OPENING),
  );
  const watch = Atom.family((window: HostCostWindow) =>
    Atom.make(
      Effect.gen(function* () {
        const gate = yield* makeRetryGate;
        gates.set(window, gate);
        return yield* runFeed(
          {
            connection,
            model: store.ref,
            setStatus: next => SubscriptionRef.update(statuses, all => ({...all, [window]: next})),
            awaitRetry: gate.wait,
          },
          hostCostSpec(parts, window),
        );
      }).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting ends the feed: its status and gate go with it.
        Effect.ensuring(forgetWindow(window)),
      ),
    ),
  );
  const view: HostCostFeed = {
    watch,
    status,
    freshness: Atom.family((window: HostCostWindow) =>
      Atom.make(get =>
        freshnessOf(
          get(connection.state),
          get(status(window)),
          get(store.atom).data.hostCosts[window] !== undefined,
        ),
      ),
    ),
    // Only a mounted feed has a gate; a retry for any other window has nothing to release.
    retry: window => gates.get(window)?.open ?? Effect.void,
  };
  return view;
});
