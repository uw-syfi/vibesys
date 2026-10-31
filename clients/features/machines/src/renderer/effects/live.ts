/**
 * The Machines page's live stats: one subscription per host card, read through
 * `useSyncExternalStore`, and the clock a stale value's age is counted on. The window holds no
 * timer for the subscription (main owns the retries); the clock is the only thing here that
 * schedules, and only while a stale card watches it.
 */
import type {HostKey} from '@vibesys/hosts';
import {actorStore, clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {MachineStats} from '@vibesys/proto';
import {
  CONNECTING,
  type Instant,
  machineStats,
  type SubscriptionPort,
  type SubscriptionSource,
  subscriptionSource,
} from '@vibesys/subscription';
import {useMemo, useSyncExternalStore} from 'react';
import {assign, createActor} from 'xstate';
import {clockMachine, type StatsView, statsView} from '../../state/index.js';

/** The live stats the page reads: a source for each host, and the clock ages are counted on. */
export interface LiveStats {
  source(host: HostKey): SubscriptionSource<MachineStats>;
  /** The viewer's time; it ticks only while someone watches it. */
  readonly clock: {getState(): Instant; subscribe(listener: () => void): () => void};
}

/** The live stats over `port`; `now` reads the viewer's clock and `schedule` is its timer. */
export function createLiveStats(
  port: SubscriptionPort,
  schedule: ScheduleTimeout,
  now: () => Instant,
): LiveStats {
  const actor = createActor(clockMachine.provide({actions: {read: assign({now: () => now()})}}), {
    input: {now: now()},
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

const UNWATCHED_STATE = CONNECTING;
const NOT_WATCHED: SubscriptionSource<MachineStats> = {
  getSnapshot: () => UNWATCHED_STATE,
  subscribe: () => () => {},
  retry: () => {},
};

/** A host's live stats as its card shows them, and the press that asks it to try again. */
export function useHostStats(
  live: LiveStats | undefined,
  host: HostKey,
): {readonly view: StatsView | null; readonly retry: () => void} {
  const source = useMemo(() => live?.source(host) ?? NOT_WATCHED, [live, host]);
  const state = useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot);
  // A page with no subscriptions shows no stats, not a status it never asked for.
  return {view: live === undefined ? null : statsView(state), retry: source.retry};
}

/** The viewer's time now, ticking while the caller is mounted. */
export function useNow(live: LiveStats): Instant {
  return useSyncExternalStore(live.clock.subscribe, live.clock.getState, live.clock.getState);
}
