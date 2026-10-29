import {describe, expect, test} from 'bun:test';
import type {UsageWindowEntry} from '@vibesys/hosts';
import {FakeClock} from '@vibesys/transport/testing';
import {FakeUsageBridge, ZERO_USAGE} from './fake-bridge.js';
import {createUsageStore} from './store.js';

const DAY = 24 * 3_600_000;
const NOW = 1_000 * DAY + 15 * 3_600_000;

const spent = (usd: number): UsageWindowEntry => ({...ZERO_USAGE, spentUsd: usd, turns: 1});

function world(usd: number) {
  return new FakeUsageBridge({
    projects: [
      {
        host: 'local',
        project: '/p',
        runs: [{runId: 'run-1', task: 'spsc', createdAt: 1}],
        answer: {kind: 'usage', usage: spent(usd)},
      },
    ],
  });
}

function usage(bridge: FakeUsageBridge) {
  const time = new FakeClock();
  const made = createUsageStore(bridge, {
    schedule: time.schedule,
    clock: () => ({now: NOW, midnight: 1_000 * DAY}),
  });
  return {...made, time};
}

/** Wait for the machine to leave `loading`, as the store's own subscribers would see it. */
async function settle(store: ReturnType<typeof usage>['store']): Promise<void> {
  for (let turn = 0; turn < 50 && store.getState().phase === 'loading'; turn += 1) {
    await Promise.resolve();
  }
}

describe('createUsageStore', () => {
  test('nothing is asked until the page is shown', () => {
    const bridge = world(1);
    const {store, stop} = usage(bridge);
    expect(bridge.overviews).toEqual([]);
    expect(store.getState()).toMatchObject({phase: 'loading', window: 'week', view: null});
    stop();
  });

  test('showing the page reads the week, and the figures arrive', async () => {
    const bridge = world(4);
    const {store, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    expect(store.getState().phase).toBe('ready');
    expect(store.getState().view?.tiles[0]?.figure).toBe('$4.00');
    expect(bridge.asked[0]).toMatchObject({since: NOW - 7 * DAY, until: NOW});
    unsubscribe();
    stop();
  });

  test('choosing Today asks from the local midnight and forgets the figures of the week', async () => {
    const bridge = world(4);
    const {store, actions, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    actions.choose('today');
    expect(store.getState()).toMatchObject({window: 'today', phase: 'loading', view: null});
    await settle(store);
    expect(bridge.asked.at(-1)).toMatchObject({since: 1_000 * DAY, until: NOW});
    expect(store.getState().phase).toBe('ready');
    unsubscribe();
    stop();
  });

  test('a window chosen while another is being read is the one shown, whatever answers first', async () => {
    const bridge = world(4);
    const {store, actions, stop} = usage(bridge);
    const release = bridge.holdAnswers();
    const unsubscribe = store.subscribe(() => undefined);
    await Promise.resolve();
    actions.choose('fortnight');
    release();
    await settle(store);
    expect(store.getState()).toMatchObject({window: 'fortnight', phase: 'ready'});
    unsubscribe();
    stop();
  });

  test('showing the page again asks again; the old figures stay until the new ones come', async () => {
    const bridge = world(4);
    const {store, stop} = usage(bridge);
    const first = store.subscribe(() => undefined);
    await settle(store);
    const before = store.getState().view;
    first();
    const release = bridge.holdAnswers();
    const second = store.subscribe(() => undefined);
    expect(store.getState()).toMatchObject({phase: 'loading', view: before});
    release();
    await settle(store);
    expect(bridge.overviews).toHaveLength(2);
    expect(store.getState().phase).toBe('ready');
    second();
    stop();
  });

  test('a helper that predates the question reads unsupported; a failure reads failed', async () => {
    const old = new FakeUsageBridge({
      projects: [
        {
          host: 'local',
          project: '/p',
          runs: [{runId: 'a', task: null, createdAt: 1}],
          answer: {kind: 'unsupported'},
        },
      ],
    });
    const first = usage(old);
    const unsubscribeOld = first.store.subscribe(() => undefined);
    await settle(first.store);
    expect(first.store.getState()).toMatchObject({phase: 'unsupported', view: null});
    unsubscribeOld();
    first.stop();
    const broken = new FakeUsageBridge({projects: [], overviewFails: true});
    const second = usage(broken);
    const unsubscribeBroken = second.store.subscribe(() => undefined);
    await settle(second.store);
    expect(second.store.getState().phase).toBe('failed');
    expect(second.store.getState().problem).toContain('could not be listed');
    unsubscribeBroken();
    second.stop();
  });

  test('a question nobody answers fails at its deadline', async () => {
    const bridge = world(1);
    const {store, time, stop} = usage(bridge);
    bridge.holdAnswers();
    const unsubscribe = store.subscribe(() => undefined);
    await Promise.resolve();
    expect(time.runNext()).toBe(true);
    expect(store.getState()).toMatchObject({phase: 'failed', problem: 'timed out'});
    unsubscribe();
    stop();
  });
});
