import {describe, expect, test} from 'bun:test';
import type {HostKey, UsageSummaryEntry} from '@vibesys/hosts';
import {FakeClock} from '@vibesys/transport/testing';
import {emptySummary, FakeUsageBridge, seriesOf} from './fake-bridge.js';
import {createUsageStore} from './store.js';

const NOW = Date.UTC(2026, 9, 30, 15, 0, 0);
const HOSTS: readonly HostKey[] = ['local', 'ssh:host-a', 'ssh:host-b'];

/** A small seeded generator so each seed replays exactly. */
function generator(seed: number): (bound: number) => number {
  let state = seed >>> 0 || 1;
  return bound => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return state % bound;
  };
}

type Step =
  | {readonly kind: 'up' | 'down'; readonly host: HostKey}
  | {readonly kind: 'show' | 'leave' | 'deadline' | 'settle'};

function steps(next: (bound: number) => number): readonly Step[] {
  return Array.from({length: next(12)}, (): Step => {
    const pick = next(8);
    if (pick < 2) return {kind: 'up', host: HOSTS[next(HOSTS.length)] ?? 'local'};
    if (pick < 4) return {kind: 'down', host: HOSTS[next(HOSTS.length)] ?? 'local'};
    return {kind: (['show', 'leave', 'deadline', 'settle'] as const)[pick - 4] ?? 'settle'};
  });
}

const settled = (phase: string): boolean => phase !== 'loading' && phase !== 'refreshing';

async function turns(store: {getState(): {phase: string}}): Promise<void> {
  for (let turn = 0; turn < 80 && !settled(store.getState().phase); turn += 1) {
    await Promise.resolve();
  }
}

/** Host number `index` spent `index + 1` dollars. */
function aBridge(next: (bound: number) => number): FakeUsageBridge {
  return new FakeUsageBridge({
    hosts: HOSTS.map((host, index) => ({
      host,
      answer: (_, book): UsageSummaryEntry => ({
        ...emptySummary(book),
        // One source per host, as three machines with three state directories.
        sources: emptySummary(book).sources.map(source => ({...source, sourceId: host})),
        series: [seriesOf({}, [0], {estimateUsd: [index + 1], turns: [1]})],
      }),
    })),
    unavailable: HOSTS.filter(() => next(2) === 0),
  });
}

/** The page of a run of the property: its store, how it is shown, and what happens to it. */
function session(bridge: FakeUsageBridge) {
  const time = new FakeClock();
  const made = createUsageStore(bridge, {
    schedule: time.schedule,
    clock: () => ({now: NOW, zone: 'UTC'}),
  });
  let unsubscribe: (() => void) | null = null;
  const show = (): void => {
    unsubscribe ??= made.store.subscribe(() => undefined);
  };
  const leave = (): void => {
    unsubscribe?.();
    unsubscribe = null;
  };
  const apply = (step: Step): void => {
    if (step.kind === 'up') bridge.connect(step.host);
    else if (step.kind === 'down') bridge.disconnect(step.host);
    else if (step.kind === 'deadline') time.runNext();
    else if (step.kind === 'show') show();
    else if (step.kind === 'leave') leave();
  };
  return {store: made.store, stop: made.stop, show, leave, apply};
}

/**
 * Over generated sequences of connection phases (machines going down and up, the page shown and
 * left, a question timing out) and reads that fail while their machine is down: once every
 * machine is up and the page is shown, it shows the total of all of them, never "Unavailable".
 */
describe('Usage after a connection failure', () => {
  test('property: the figures arrive once the machines are reachable', async () => {
    for (let seed = 1; seed <= 150; seed += 1) {
      const next = generator(seed);
      const bridge = aBridge(next);
      const page = session(bridge);
      page.show();
      for (const step of steps(next)) {
        page.apply(step);
        await turns(page.store);
      }
      for (const host of HOSTS) bridge.connect(host);
      page.show();
      await turns(page.store);
      for (let turn = 0; turn < 3000 && page.store.getState().phase !== 'ready'; turn += 1) {
        await Promise.resolve();
      }
      expect(page.store.getState().phase, `seed ${seed}`).toBe('ready');
      expect(page.store.getState().view?.hero.figure, `seed ${seed}`).toBe('$6.00');
      page.leave();
      page.stop();
    }
  });

  test('a question nobody answers is still bounded for a machine that is up', async () => {
    const bridge = new FakeUsageBridge({hosts: [{host: 'local', answer: () => emptySummary()}]});
    const time = new FakeClock();
    const {store, stop} = createUsageStore(bridge, {
      schedule: time.schedule,
      clock: () => ({now: NOW, zone: 'UTC'}),
    });
    bridge.holdAnswers();
    const unsubscribe = store.subscribe(() => undefined);
    await Promise.resolve();
    expect(time.runNext()).toBe(true);
    expect(store.getState()).toMatchObject({phase: 'failed', problem: 'timed out'});
    unsubscribe();
    stop();
  });
});
