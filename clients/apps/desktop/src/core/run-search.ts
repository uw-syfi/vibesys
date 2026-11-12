/**
 * Search of the host's whole durable run history. The runs snapshot is deliberately byte-bounded,
 * so the local Runs filter cannot see its oldest finished runs. `SearchRuns` asks the host for those
 * matches without making the snapshot or this controller another source of run state.
 *
 * Typing stays immediate in the UI: this controller only owns the authoritative, host-wide answer.
 * Calls wait briefly so a word being typed does not ask about every prefix. A monotonically
 * increasing request number makes an overtaken answer inert, including an A -> B -> A sequence.
 */
import type {SearchRunsResponse} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Ref from 'effect/Ref';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {Fault} from '../state/index.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';

/** How long the host-wide lookup waits while the operator is still typing. */
export const RUN_SEARCH_DEBOUNCE = '200 millis';

/** The answer for exactly one query, never retained under a different query. */
export type RunSearchState =
  | {readonly _tag: 'Idle'}
  | {readonly _tag: 'Searching'; readonly query: string}
  | {readonly _tag: 'Found'; readonly query: string; readonly response: SearchRunsResponse}
  | {readonly _tag: 'Invalid'; readonly query: string; readonly reason: string}
  | {readonly _tag: 'Failed'; readonly query: string; readonly fault: Fault};

export interface RunSearch {
  /** Where the latest host-wide lookup stands. */
  readonly state: Atom.Atom<RunSearchState>;
  /**
   * Make `typed` the latest query. Empty input clears the answer. The effect never fails; failure is
   * represented in `state`, and an answer overtaken by another call is discarded.
   */
  readonly search: (typed: string) => Effect.Effect<void>;
  /**
   * Keep `query` searched while the Runs screen is open. It starts when an omitted-run snapshot
   * arrives, refreshes an answer older than the feed, and retries a failed read when remounted.
   */
  readonly watch: (query: string) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
}

const IDLE: RunSearchState = {_tag: 'Idle'};

/** Whether `before` already answers the watcher at the current feed cut. */
function searchIsCurrent(before: RunSearchState, query: string, feedSequence: bigint): boolean {
  if (before._tag === 'Idle') return query === '';
  if (before.query !== query) return false;
  if (before._tag === 'Searching' || before._tag === 'Invalid') return true;
  return before._tag === 'Found' && before.response.sequence >= feedSequence;
}

export const makeRunSearch = Effect.fn('RunSearch.make')(function* (parts: HostParts) {
  const {client, store, ambient} = parts;
  const generation = yield* Ref.make(0);
  const state = yield* SubscriptionRef.make<RunSearchState>(IDLE);
  const stateAtom = Atom.keepAlive(Atom.subscriptionRef(state));

  const searchAt = Effect.fn('RunSearch.search')(function* (typed: string, feedSequence: bigint) {
    const query = typed.trim();
    const before = yield* SubscriptionRef.get(state);
    if (searchIsCurrent(before, query, feedSequence)) return;
    const request = yield* Ref.modify(generation, current => [current + 1, current + 1]);
    if (query === '') return yield* SubscriptionRef.set(state, IDLE);

    yield* SubscriptionRef.set(state, {_tag: 'Searching', query});
    yield* Effect.sleep(RUN_SEARCH_DEBOUNCE);
    if ((yield* Ref.get(generation)) !== request) return;

    yield* Effect.gen(function* () {
      const result = yield* client.searchRuns({query}).pipe(Effect.result);
      if ((yield* Ref.get(generation)) !== request) return;
      if (result._tag === 'Success') {
        yield* SubscriptionRef.set(state, {_tag: 'Found', query, response: result.success});
      } else if (result.failure._tag === 'InvalidMessage') {
        yield* SubscriptionRef.set(state, {
          _tag: 'Invalid',
          query,
          reason: 'Use a search query of at most 256 UTF-8 bytes.',
        });
      } else {
        yield* SubscriptionRef.set(state, {_tag: 'Failed', query, fault: faultOf(result.failure)});
      }
    }).pipe(
      Effect.onInterrupt(() =>
        Effect.gen(function* () {
          if ((yield* Ref.get(generation)) === request) yield* SubscriptionRef.set(state, IDLE);
        }),
      ),
    );
  });

  const search = (typed: string) => searchAt(typed, 0n).pipe(Effect.provideContext(ambient));
  const watch = Atom.family((query: string) =>
    Atom.make(
      SubscriptionRef.changes(store.ref).pipe(
        Stream.filter(model => query.trim() === '' || model.data.cut.runsLeftOut > 0),
        Stream.mapEffect(model => searchAt(query, model.data.sequence)),
        Stream.runDrain,
        // A mounted watcher is a lifetime, not a one-shot computation. The subscription normally
        // never ends; if its source is replaced during teardown, keep the atom's result uninhabited.
        Effect.forever,
        Effect.provideContext(ambient),
      ),
    ),
  );

  return {
    state: stateAtom,
    search,
    watch,
  } satisfies RunSearch;
});
