/** The one store the streams fold into, and what every view needs to build on it. */
import type * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {emptyHostModel, type HostModel} from '../state/index.js';
import type {HostConnectionApi} from './connection.js';
import type {OrchestratorClient} from './orchestrator-client.js';

export interface HostStore {
  readonly ref: SubscriptionRef.SubscriptionRef<HostModel>;
  /** What screens read: the model as it stands. */
  readonly atom: Atom.Atom<HostModel>;
}

export const makeHostStore: Effect.Effect<HostStore> = Effect.gen(function* () {
  const ref = yield* SubscriptionRef.make(emptyHostModel);
  return {ref, atom: Atom.keepAlive(Atom.subscriptionRef(ref))};
});

/** What a view of the host is built from. */
export interface HostParts {
  readonly client: OrchestratorClient;
  readonly connection: HostConnectionApi;
  readonly store: HostStore;
  /**
   * The ambient services (clock, randomness) the session was built under. Atoms run their effects
   * on the registry's own runtime, so a feed provides these to keep time and randomness the
   * session's, not the registry's.
   */
  readonly ambient: Context.Context<never>;
}
