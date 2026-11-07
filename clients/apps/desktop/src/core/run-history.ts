/**
 * One run's history, read on request. A snapshot of runs holds each run and its summary and nothing of
 * its history (its agents, turns, messages and check-ins), so the screens of a run need it read: the
 * host serves it a page at a time (`GetRunHistory`), and each page folds into the same model the
 * streams fold into. The run stream's cursor does not move for it, and what happens after a page
 * still arrives on the stream.
 *
 * While a screen of the run is mounted the history is kept read: it is read once when the screen
 * opens, and read again when a new snapshot forgot that it was (the model says so in `histories`),
 * never in between. A failed read stops and says so as the run's status until the user retries.
 */
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {applyHostFrame, type Fault, type HostModel} from '../state/index.js';
import type {AppError} from './errors.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';
import {makeRetryGate, type RetryGate} from './retry-gate.js';

/** Where reading a run's history stands. */
export type RunHistoryStatus =
  | {readonly _tag: 'Idle'}
  | {readonly _tag: 'Loading'}
  | {readonly _tag: 'Failed'; readonly fault: Fault};

export interface RunHistory {
  /**
   * Mount this while a screen of the run is open: it reads the run's history, and again whenever a
   * new snapshot forgot it. Mounting twice shares one.
   */
  readonly watch: (runId: string) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  /** Where reading the run's history stands. */
  readonly status: (runId: string) => Atom.Atom<RunHistoryStatus>;
  /** The user asked to retry a failed read of this run's history. */
  readonly retry: (runId: string) => Effect.Effect<void>;
}

type Statuses = Readonly<Record<string, RunHistoryStatus>>;

const IDLE: RunHistoryStatus = {_tag: 'Idle'};

/** Whether the model holds the whole history of the run as read. */
const held = (model: HostModel, runId: string): boolean =>
  model.data.histories[runId]?.complete === true;

export const makeRunHistory = Effect.fn('RunHistory.make')(function* (parts: HostParts) {
  const {client, connection, store} = parts;
  const statuses = yield* SubscriptionRef.make<Statuses>({});
  const statusesAtom = Atom.keepAlive(Atom.subscriptionRef(statuses));
  /** The gate of each mounted run's reader: there while it is mounted, gone when it unmounts. */
  const gates = new Map<string, RetryGate>();

  const setStatus = (runId: string, status: RunHistoryStatus) =>
    SubscriptionRef.update(statuses, all => ({...all, [runId]: status}));
  const forgetRun = (runId: string) =>
    Effect.suspend(() => {
      gates.delete(runId);
      return SubscriptionRef.update(statuses, ({[runId]: _gone, ...rest}) => rest);
    });

  const fold = (frame: Parameters<typeof applyHostFrame>[1]) =>
    SubscriptionRef.update(store.ref, model => applyHostFrame(model, frame));

  /** Every page of the run's history, each folded as it arrives. */
  const readPages = (runId: string): Effect.Effect<void, AppError> =>
    Effect.gen(function* () {
      let cursor = 0n;
      let first = true;
      while (true) {
        const page = yield* client.getRunHistory({runId, cursor});
        const last = page.nextCursor === 0n;
        yield* fold({from: 'run-history', runId, page, first, last});
        if (last) return;
        first = false;
        cursor = page.nextCursor;
      }
    });

  const read = (runId: string) =>
    Effect.gen(function* () {
      yield* connection.ready().pipe(Effect.result);
      yield* setStatus(runId, {_tag: 'Loading'});
      const done = yield* readPages(runId).pipe(Effect.result);
      return done._tag === 'Success' ? null : faultOf(done.failure);
    });

  const status = Atom.family((runId: string) => Atom.make(get => get(statusesAtom)[runId] ?? IDLE));
  const watch = Atom.family((runId: string) =>
    Atom.make(
      Effect.gen(function* () {
        const gate = yield* makeRetryGate;
        gates.set(runId, gate);
        while (true) {
          // Wait until the model does not hold the run's history: before the first read, and after a
          // new snapshot forgot it.
          yield* SubscriptionRef.changes(store.ref).pipe(
            Stream.filter(model => !held(model, runId)),
            Stream.take(1),
            Stream.runDrain,
          );
          const fault = yield* read(runId);
          if (fault === null) {
            yield* setStatus(runId, IDLE);
            continue;
          }
          yield* gate.wait(setStatus(runId, {_tag: 'Failed', fault}));
        }
      }).pipe(Effect.provideContext(parts.ambient), Effect.ensuring(forgetRun(runId))),
    ),
  );

  const history: RunHistory = {
    watch,
    status,
    // Only a mounted reader has a gate; a retry for any other run has nothing to release.
    retry: runId => gates.get(runId)?.open ?? Effect.void,
  };
  return history;
});
