import {describe, expect, test} from 'bun:test';
import type {HostKey, UsageWindowEntry} from '@vibesys/hosts';
import {FakeClock} from '@vibesys/transport/testing';
import {FakeUsageBridge, ZERO_USAGE} from './fake-bridge.js';
import {createUsageStore} from './store.js';

const DAY = 24 * 3_600_000;
const NOW = 1_000 * DAY + 15 * 3_600_000;
const HOSTS: readonly HostKey[] = ['host-a', 'host-b', 'host-c'];

const spent = (usd: number): UsageWindowEntry => ({...ZERO_USAGE, spentUsd: usd, turns: 1});

/** A small seeded generator so each seed replays exactly (fast-check is not this package's dependency). */
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
    if (pick < 2) return {kind: 'up', host: HOSTS[next(HOSTS.length)] ?? 'host-a'};
    if (pick < 4) return {kind: 'down', host: HOSTS[next(HOSTS.length)] ?? 'host-a'};
    return {kind: (['show', 'leave', 'deadline', 'settle'] as const)[pick - 4] ?? 'settle'};
  });
}

async function turns(store: {getState(): {phase: string}}): Promise<void> {
  for (let turn = 0; turn < 50 && store.getState().phase === 'loading'; turn += 1) {
    await Promise.resolve();
  }
}

function aBridge(next: (bound: number) => number): FakeUsageBridge {
  return new FakeUsageBridge({
    projects: HOSTS.map((host, index) => ({
      host,
      project: '/p',
      runs: [{runId: `run-${index}`, task: null, createdAt: 1}],
      answer: {kind: 'usage', usage: spent(index + 1)},
    })),
    unavailable: HOSTS.filter(() => next(2) === 0),
  });
}

/** The page of a run of the property: its store, how it is shown, and what happens to it. */
function session(bridge: FakeUsageBridge) {
  const time = new FakeClock();
  const made = createUsageStore(bridge, {
    schedule: time.schedule,
    clock: () => ({now: NOW, midnight: 1_000 * DAY}),
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
 * machine is up and the page is shown, it shows the totals of all of them, never "Unavailable".
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
      expect(page.store.getState().phase, `seed ${seed}`).toBe('ready');
      expect(page.store.getState().view?.tiles[0]?.figure, `seed ${seed}`).toBe('$6.00');
      page.leave();
      page.stop();
    }
  });

  test('a question nobody answers is still bounded for a machine that is up', async () => {
    const bridge = new FakeUsageBridge({
      projects: [
        {
          host: 'host-a',
          project: '/p',
          runs: [{runId: 'run-1', task: null, createdAt: 1}],
          answer: {kind: 'usage', usage: spent(1)},
        },
      ],
    });
    const time = new FakeClock();
    const {store, stop} = createUsageStore(bridge, {
      schedule: time.schedule,
      clock: () => ({now: NOW, midnight: 1_000 * DAY}),
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
