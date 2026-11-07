/**
 * The hosts the window has a session with, which the person can add to and remove from while the
 * window is open. Each host's session lives in a scope of its own inside the window's, so removing a
 * host ends its streams and its connection and nothing else.
 */
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Atom from 'effect/reactivity/Atom';
import * as Scope from 'effect/Scope';
import * as Semaphore from 'effect/Semaphore';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {type HostKey, hostFromKey, hostLabel} from '../../state/index.js';
import {type HostSession, makeHostSession} from '../host-session.js';
import type {Ids} from '../ids.js';
import type {GatewayTransport} from '../transport/gateway-transport.js';

/** One host the window reads, by the `HostKey` its routes name. */
export interface RosterEntry {
  readonly key: HostKey;
  readonly label: string;
  readonly session: HostSession;
}

export interface Roster {
  /** The hosts as they stand, in the order they were added. */
  readonly entries: Atom.Atom<readonly RosterEntry[]>;
  /** Opens a session with the host; nothing happens when it already has one. */
  readonly add: (key: HostKey) => Effect.Effect<void>;
  /** Ends the host's session and drops it; nothing happens when it has none. */
  readonly remove: (key: HostKey) => Effect.Effect<void>;
}

export const makeRoster = Effect.fn('Roster.make')(function* (
  initial: readonly HostKey[],
): Effect.fn.Return<Roster, never, GatewayTransport | Ids | Scope.Scope> {
  const parent = yield* Scope.Scope;
  const context = yield* Effect.context<GatewayTransport | Ids>();
  const ref = yield* SubscriptionRef.make<readonly RosterEntry[]>([]);
  const scopes = new Map<HostKey, Scope.Closeable>();
  /** One change at a time, so two quick presses cannot open a host twice. */
  const changing = Semaphore.makeUnsafe(1);
  const add = (key: HostKey): Effect.Effect<void> =>
    changing.withPermits(1)(
      Effect.gen(function* () {
        if (scopes.has(key)) return;
        const own = yield* Scope.fork(parent, 'sequential');
        const session = yield* makeHostSession(key).pipe(
          Scope.provide(own),
          Effect.provide(context),
        );
        scopes.set(key, own);
        const entry = {key, label: hostLabel(hostFromKey(key)), session};
        yield* SubscriptionRef.update(ref, entries => [...entries, entry]);
      }),
    );
  const remove = (key: HostKey): Effect.Effect<void> =>
    changing.withPermits(1)(
      Effect.gen(function* () {
        const own = scopes.get(key);
        if (own === undefined) return;
        scopes.delete(key);
        yield* SubscriptionRef.update(ref, entries => entries.filter(entry => entry.key !== key));
        yield* Scope.close(own, Exit.void);
      }),
    );
  for (const key of initial) yield* add(key);
  return {entries: Atom.keepAlive(Atom.subscriptionRef(ref)), add, remove};
});
