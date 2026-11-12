/**
 * One run's cost stream: the whole cost view the host works out, then a whole view again each time it
 * changes, while a screen watches it. The client sums nothing: each message is read into a view
 * (`readCost`), which is stored as it arrives (`applyCost`) and read from `HostModel.data.costs`. A
 * message that lacks a field the contract requires fails the stream with the field named, and the
 * screen shows that beside its status. A stream always starts with a view, so a reopen has no cursor
 * to resume from.
 */
import {RunCostSchema, type WatchRunCostResponse} from '@vibesys/orchestrator-proto';
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
  type HostFrame,
  readCost,
  type WholeViewPhase,
} from '../state/index.js';
import {InvalidMessage} from './errors.js';
import {type FeedSpec, runFeed} from './feeds.js';
import type {HostParts} from './host-store.js';
import {makeRetryGate, type RetryGate} from './retry-gate.js';

export interface CostFeed {
  /** Mount this to follow one run's cost; unmounting closes its stream. Mounting twice shares one. */
  readonly watch: (runId: string) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  readonly status: (runId: string) => Atom.Atom<FeedStatus>;
  readonly freshness: (runId: string) => Atom.Atom<Freshness>;
  /** The user asked to retry this run's failed cost stream. */
  readonly retry: (runId: string) => Effect.Effect<void>;
}

type Statuses = Readonly<Record<string, FeedStatus>>;

const OPENING: FeedStatus = {_tag: 'Opening'};

/** A frame of the view the message carries, or the failure that names what it lacks. */
function costFrame(
  response: WatchRunCostResponse,
  phase: WholeViewPhase,
): Effect.Effect<HostFrame, InvalidMessage> {
  const invalid = (detail: string) =>
    Effect.fail(new InvalidMessage({direction: 'response', type: RunCostSchema.typeName, detail}));
  if (response.cost === undefined) return invalid('cost is absent');
  const read = readCost(response.cost);
  return read._tag === 'Missing'
    ? invalid(`${read.field} is absent`)
    : Effect.succeed({from: 'cost', phase, view: read.view});
}

const costSpec = ({client}: HostParts, runId: string): FeedSpec<HostFrame> => ({
  open: () =>
    client.watchRunCost({runId}).pipe(
      Stream.zipWithIndex,
      Stream.mapEffect(([response, index]) =>
        costFrame(response, index === 0 ? 'initial' : 'update'),
      ),
    ),
  apply: applyHostFrame,
  gapped: () => false,
});

export const makeCostFeed = Effect.fn('CostFeed.make')(function* (parts: HostParts) {
  const {connection, store} = parts;
  const statuses = yield* SubscriptionRef.make<Statuses>({});
  const statusesAtom = Atom.keepAlive(Atom.subscriptionRef(statuses));
  /** The gate of each mounted run's feed: there while it is mounted, gone when it unmounts. */
  const gates = new Map<string, RetryGate>();
  const forgetRun = (runId: string) =>
    Effect.suspend(() => {
      gates.delete(runId);
      return SubscriptionRef.update(statuses, ({[runId]: _gone, ...rest}) => rest);
    });

  const status = Atom.family((runId: string) =>
    Atom.make(get => get(statusesAtom)[runId] ?? OPENING),
  );
  const watch = Atom.family((runId: string) =>
    Atom.make(
      Effect.gen(function* () {
        const gate = yield* makeRetryGate;
        gates.set(runId, gate);
        return yield* runFeed(
          {
            connection,
            model: store.ref,
            setStatus: next => SubscriptionRef.update(statuses, all => ({...all, [runId]: next})),
            awaitRetry: gate.wait,
          },
          costSpec(parts, runId),
        );
      }).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting ends the feed: its status and gate go with it.
        Effect.ensuring(forgetRun(runId)),
      ),
    ),
  );
  const view: CostFeed = {
    watch,
    status,
    freshness: Atom.family((runId: string) =>
      Atom.make(get =>
        freshnessOf(
          get(connection.state),
          get(status(runId)),
          get(store.atom).data.costs[runId] !== undefined,
        ),
      ),
    ),
    // Only a mounted feed has a gate; a retry for any other run has nothing to release.
    retry: runId => gates.get(runId)?.open ?? Effect.void,
  };
  return view;
});
