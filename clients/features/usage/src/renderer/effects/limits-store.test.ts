import {describe, expect, test} from 'bun:test';
import type {
  UsageAccountLimitsEntry,
  UsageLimitsEntry,
  UsageProviderLimitsEntry,
} from '@vibesys/hosts';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {LIMITS_FLOOR_MS, LIMITS_STORED_AGE_S} from '../../state/index.js';
import {emptySummary, FakeUsageBridge} from './fake-bridge.js';
import {createUsageStore} from './store.js';

const NOW = Date.UTC(2026, 9, 31, 12, 0, 0);
const HOUR = 3_600_000;

const provider = (
  name: string,
  used: number,
  status: UsageProviderLimitsEntry['status'] = 'live',
): UsageProviderLimitsEntry => ({
  provider: name,
  status,
  unavailable: null,
  limits: {
    provider: name,
    account: `${name}-account`,
    plan: 'pro',
    windows: [
      {
        id: `${name}:w`,
        kind: 'weekly',
        label: 'Weekly',
        usedFraction: used,
        resetsAt: NOW + 5 * HOUR,
        windowMinutes: 10_080,
      },
    ],
    resetCreditsAvailable: 1,
    resetCredits: [],
    usageUrl: null,
    readAt: NOW,
  },
});

const entry = (...providers: UsageProviderLimitsEntry[]): UsageLimitsEntry => ({
  providers,
  readAt: NOW,
});

function world(limits: UsageLimitsEntry | 'fails' | 'unsupported') {
  const bridge = new FakeUsageBridge({
    hosts: [{host: 'local', answer: (_, book) => emptySummary(book)}],
  });
  bridge.limitsOf = {local: limits};
  const time = new FakeClock();
  let now = NOW;
  const made = createUsageStore(bridge, {
    schedule: time.schedule,
    clock: () => ({now, zone: 'UTC'}),
  });
  return {
    ...made,
    bridge,
    time,
    at: (next: number) => {
      now = next;
    },
  };
}

async function settle(store: ReturnType<typeof world>['store']): Promise<void> {
  for (let turn = 0; turn < 80; turn += 1) {
    await Promise.resolve();
    if (!store.getState().limits.checking) return;
  }
}

describe('the Limits tab', () => {
  test('nothing is asked of a host until the tab is chosen, then each machine is asked once', async () => {
    const {store, actions, bridge, stop} = world(entry(provider('codex', 0.25)));
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    expect(bridge.limitsAsked).toEqual([]);
    actions.tab('limits');
    await settle(store);
    expect(bridge.limitsAsked).toEqual([{host: 'local', maxAgeS: LIMITS_STORED_AGE_S, now: NOW}]);
    const card = store.getState().limits.view?.cards[0];
    expect(card?.providerName).toBe('Codex');
    expect(card?.bars[0]?.leftText).toBe('75% left');
    expect(card?.bars[0]?.resets).toBe('5 h');
    expect(store.getState().tab).toBe('limits');
    unsubscribe();
    stop();
  });

  test('choosing the tab again inside the floor asks nothing, and Refresh asks the CLIs at once', async () => {
    const {store, actions, bridge, at, stop} = world(entry(provider('claude', 0.1)));
    const unsubscribe = store.subscribe(() => undefined);
    actions.tab('limits');
    await settle(store);
    actions.tab('cost');
    at(NOW + LIMITS_FLOOR_MS - 1);
    actions.tab('limits');
    await settle(store);
    expect(bridge.limitsAsked).toHaveLength(1);
    actions.refresh();
    await settle(store);
    expect(bridge.limitsAsked.map(each => each.maxAgeS)).toEqual([LIMITS_STORED_AGE_S, 0]);
    unsubscribe();
    stop();
  });

  test('a failed question keeps the figures shown and says so, and asks nothing on its own', async () => {
    const {store, actions, bridge, stop} = world(entry(provider('codex', 0.5)));
    const unsubscribe = store.subscribe(() => undefined);
    actions.tab('limits');
    await settle(store);
    bridge.limitsOf = {local: 'fails'};
    actions.refresh();
    await settle(store);
    const state = store.getState().limits;
    expect(state.failed).toBe(true);
    expect(state.view?.cards[0]?.bars[0]?.left).toBe(50);
    expect(bridge.limitsAsked).toHaveLength(2);
    unsubscribe();
    stop();
  });

  test('a host that cannot answer is counted, and a first failure is Unavailable, not an empty page', async () => {
    const down = world('fails');
    const unsubscribe = down.store.subscribe(() => undefined);
    down.actions.tab('limits');
    await settle(down.store);
    expect(down.store.getState().limits).toMatchObject({view: null, failed: true});
    unsubscribe();
    down.stop();
    const old = world('unsupported');
    const again = old.store.subscribe(() => undefined);
    old.actions.tab('limits');
    await settle(old.store);
    expect(old.store.getState().limits).toMatchObject({unsupported: true, failed: false});
    again();
    old.stop();
  });

  test('while the machine is connecting the tab loads and then shows its limits, never No limits', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [{host: 'local', answer: (_, book) => emptySummary(book)}],
      connecting: ['local'],
    });
    bridge.limitsOf = {local: entry(provider('codex', 0.25))};
    const made = createUsageStore(bridge, {
      schedule: new FakeClock().schedule,
      clock: () => ({now: NOW, zone: 'UTC'}),
    });
    const unsubscribe = made.store.subscribe(() => undefined);
    made.actions.tab('limits');
    await settle(made.store);
    expect(made.store.getState().limits).toMatchObject({view: null, checking: true, failed: false});
    expect(bridge.limitsAsked).toEqual([]);
    bridge.connect('local');
    // The machine coming up asks again at once, without waiting for the next poll.
    for (let turn = 0; turn < 200 && made.store.getState().limits.view === null; turn += 1) {
      await Promise.resolve();
    }
    expect(made.store.getState().limits.view?.cards.map(card => card.providerName)).toEqual([
      'Codex',
    ]);
    unsubscribe();
    made.stop();
  });

  test('property: when a window refills does not depend on how long the question took to start', async () => {
    // A real clock moves between the moment the page asks and the moment the question reaches the
    // bridge. The question has one time: the window's span is from it, whatever the clock did.
    await fc.assert(
      fc.asyncProperty(fc.integer({min: 0, max: 3_600_000}), async step => {
        const bridge = new FakeUsageBridge({
          hosts: [{host: 'local', answer: (_, book) => emptySummary(book)}],
        });
        // A provider that says when it refills relative to the moment it was asked.
        bridge.limitsOf = {
          local: now => ({
            readAt: now,
            providers: [
              {
                ...provider('codex', 0.25),
                limits: {
                  ...(provider('codex', 0.25).limits as UsageAccountLimitsEntry),
                  windows: [
                    {
                      id: 'codex:w',
                      kind: 'weekly' as const,
                      label: 'Weekly',
                      usedFraction: 0.25,
                      resetsAt: now + 5 * HOUR,
                      windowMinutes: 10_080,
                    },
                  ],
                  readAt: now,
                },
              },
            ],
          }),
        };
        let now = NOW;
        const made = createUsageStore(bridge, {
          schedule: new FakeClock().schedule,
          clock: () => {
            now += step;
            return {now, zone: 'UTC'};
          },
        });
        const unsubscribe = made.store.subscribe(() => undefined);
        made.actions.tab('limits');
        await settle(made.store);
        expect(made.store.getState().limits.view?.cards[0]?.bars[0]?.resets).toBe('5 h');
        unsubscribe();
        made.stop();
      }),
      {numRuns: 30, seed: 4417},
    );
  });

  test('the state is the same object until something changed', async () => {
    const {store, actions, stop} = world(entry(provider('codex', 0.5)));
    const unsubscribe = store.subscribe(() => undefined);
    actions.tab('limits');
    await settle(store);
    expect(store.getState()).toBe(store.getState());
    unsubscribe();
    stop();
  });
});
