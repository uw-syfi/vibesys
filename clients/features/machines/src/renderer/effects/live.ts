/**
 * The Machines page's live stats: one subscription per host card, read through
 * `useSyncExternalStore`, and the clock a stale value's age is counted on. The window holds no
 * timer for the subscription (main owns the retries); the clock is the only thing here that
 * schedules, and only while a stale card watches it.
 */
import type {HostKey} from '@vibesys/hosts';
import {actorStore, clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  type HostRunFacts,
  type Instant,
  type MachineStats,
  machineStats,
  runs,
  type SubscriptionPort,
  type SubscriptionSource,
  subscriptionSource,
} from '@vibesys/subscription';
import {useMemo, useSyncExternalStore} from 'react';
import {assign, createActor} from 'xstate';
import {ageWord, clockMachine, type HostLive, runsView, statsView} from '../../state/index.js';

/**
 * The live data the page reads: a source of each host's stats and of its runs, and the clock ages
 * are counted on. The run counts of a card all come from the one `runs` source.
 */
export interface LiveStats {
  source(host: HostKey): SubscriptionSource<MachineStats>;
  runs(host: HostKey): SubscriptionSource<HostRunFacts>;
  /** The viewer's time, or null until someone watches; it ticks only while someone does. */
  readonly clock: {getState(): Instant | null; subscribe(listener: () => void): () => void};
}

/** The source kept for `host` in `kept`, made on first use. */
function shared<S>(
  kept: Map<HostKey, SubscriptionSource<S>>,
  host: HostKey,
  make: () => SubscriptionSource<S>,
): SubscriptionSource<S> {
  const found = kept.get(host);
  if (found !== undefined) return found;
  const made = make();
  kept.set(host, made);
  return made;
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
  // One source per host and topic, so the cards and the rail that read a host share one observation.
  const stats = new Map<HostKey, SubscriptionSource<MachineStats>>();
  const held = new Map<HostKey, SubscriptionSource<HostRunFacts>>();
  const time = actorStore(actor, snapshot => snapshot.context.now);
  return {
    source: host => shared(stats, host, () => subscriptionSource(port, host, machineStats)),
    runs: host => shared(held, host, () => subscriptionSource(port, host, runs)),
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
 * A host's live data as its card shows it: what to draw, how old a stale value is (the clock is
 * watched only while there is one), the runs it holds, and the press that asks it to try again.
 */
export function useHostStats(live: LiveStats, host: HostKey): HostLive {
  const source = useMemo(() => live.source(host), [live, host]);
  const state = useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot);
  const held = useMemo(() => live.runs(host), [live, host]);
  const holding = useSyncExternalStore(held.subscribe, held.getSnapshot, held.getSnapshot);
  const view = statsView(state);
  const stale = view.kind === 'values' ? view.stale : null;
  const now = useSyncExternalStore(
    stale === null ? noSubscription : live.clock.subscribe,
    live.clock.getState,
    live.clock.getState,
  );
  return {
    view,
    runs: runsView(holding),
    age: stale === null || now === null ? null : ageWord(now, stale.valueAt),
    retry: source.retry,
  };
}

const noSubscription = (): (() => void) => () => {};
