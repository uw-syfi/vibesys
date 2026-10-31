/**
 * The Machines page's live stats: one subscription per host card, read through
 * `useSyncExternalStore`, and the clock a stale value's age is counted on. The window holds no
 * timer for the subscription (main owns the retries); the clock is the only thing here that
 * schedules, and only while a stale card watches it.
 */
import type {HostKey} from '@vibesys/hosts';
import {actorStore, clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  type Instant,
  type MachineStats,
  machineStats,
  type SubscriptionPort,
  type SubscriptionSource,
  subscriptionSource,
} from '@vibesys/subscription';
import {useMemo, useSyncExternalStore} from 'react';
import {assign, createActor} from 'xstate';
import {ageWord, clockMachine, type HostLive, statsView} from '../../state/index.js';

/** The live stats the page reads: a source for each host, and the clock ages are counted on. */
export interface LiveStats {
  source(host: HostKey): SubscriptionSource<MachineStats>;
  /** The viewer's time, or null until someone watches; it ticks only while someone does. */
  readonly clock: {getState(): Instant | null; subscribe(listener: () => void): () => void};
}

/** The live stats over `port`; `now` reads the viewer's clock and `schedule` is its timer. */
export function createLiveStats(
  port: SubscriptionPort,
  schedule: ScheduleTimeout,
  now: () => Instant,
): LiveStats {
  const actor = createActor(clockMachine.provide({actions: {read: assign({now: () => now()})}}), {
    clock: clockFrom(schedule),
  });
  actor.start();
  const time = actorStore(actor, snapshot => snapshot.context.now);
  return {
    source: host => subscriptionSource(port, host, machineStats),
    clock: {
      getState: time.getState,
      subscribe: listener => {
        const stop = time.subscribe(listener);
        actor.send({type: 'watch'});
        return () => {
          stop();
          actor.send({type: 'unwatch'});
        };
      },
    },
  };
}

/**
 * A host's live stats as its card shows them: what to draw, how old a stale value is (the clock
 * is watched only while there is one), and the press that asks the host to try again.
 */
export function useHostStats(live: LiveStats, host: HostKey): HostLive {
  const source = useMemo(() => live.source(host), [live, host]);
  const state = useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot);
  const view = statsView(state);
  const stale = view.kind === 'values' ? view.stale : null;
  const now = useSyncExternalStore(
    stale === null ? noSubscription : live.clock.subscribe,
    live.clock.getState,
    live.clock.getState,
  );
  return {
    view,
    age: stale === null || now === null ? null : ageWord(now, stale.valueAt),
    retry: source.retry,
  };
}

const noSubscription = (): (() => void) => () => {};
