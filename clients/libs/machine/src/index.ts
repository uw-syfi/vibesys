/**
 * The shell glue every control-state machine shares (docs/contributing/clients-architecture.md,
 * "State"): a machine's actor seen as the `Store` a view subscribes to, a clock adapter so a
 * machine's `after` delays run on an injected scheduler, and the placeholder for an operation the
 * shell provides. Generic: it names no concept of the product.
 */
import {createListenerDelivery, type Store} from '@vibesys/store';
import {
  type Actor,
  type ActorOptions,
  type AnyActorLogic,
  type EventFromLogic,
  fromPromise,
  type PromiseActorLogic,
  type SnapshotFrom,
} from 'xstate';

/** What `createActor`'s `clock` option takes: XState does not export the type by name. */
export type MachineClock = NonNullable<ActorOptions<AnyActorLogic>['clock']>;

/** Schedule `callback` after `delayMs`; the returned function cancels it if it has not run. */
export type ScheduleTimeout = (callback: () => void, delayMs: number) => () => void;

/**
 * A machine clock over `schedule`, so `after` delays run on the scheduler the composition injects
 * (the browser's timers in production, a fake clock in tests).
 */
export function clockFrom(schedule: ScheduleTimeout): MachineClock {
  return {
    setTimeout: (callback: () => void, delayMs: number) => schedule(callback, delayMs),
    clearTimeout: (cancel: unknown) => {
      if (typeof cancel === 'function') cancel();
    },
  };
}

/**
 * The placeholder for an operation the shell provides with `machine.provide({actors})`: typed with
 * the operation's input and output so a real implementation provides cleanly, and failing loudly
 * when a machine runs without it.
 */
export function unprovided<Output, Input = void>(name: string): PromiseActorLogic<Output, Input> {
  return fromPromise<Output, Input>(() => Promise.reject(new Error(`${name} was not provided`)));
}

/**
 * `actor` as a `Store`: `getState` is `view` of its snapshot, `dispatch` sends an event, and a
 * subscriber hears only when the view changed (by identity), so React re-renders only on a change
 * the view can see. The view must return the same value for a snapshot whose relevant parts did
 * not change (read it from context, which `assign` replaces only when it changes).
 */
export function actorStore<L extends AnyActorLogic, V>(
  actor: Actor<L>,
  view: (snapshot: SnapshotFrom<L>) => V,
): Store<V, EventFromLogic<L>> {
  let current = view(actor.getSnapshot());
  const delivery = createListenerDelivery();
  actor.subscribe(snapshot => {
    const next = view(snapshot);
    if (Object.is(next, current)) return;
    current = next;
    delivery.notify();
  });
  return {
    getState: () => current,
    subscribe: delivery.subscribe,
    dispatch: event => actor.send(event),
  };
}
