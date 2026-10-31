import {describe, expect, test} from 'bun:test';
import type {UsageSummaryEntry} from '@vibesys/hosts';
import {FakeClock} from '@vibesys/transport/testing';
import {draftOf} from '../../state/index.js';
import {emptySummary, type FakeAnswer, FakeUsageBridge, seriesOf} from './fake-bridge.js';
import {createUsageStore} from './store.js';

const NOW = Date.UTC(2026, 9, 30, 15, 0, 0);

/** A host that spent `usd` of Claude in the first bucket, in `tokens` uncached tokens. */
function spent(usd: number, extra: Partial<UsageSummaryEntry> = {}): FakeAnswer {
  return (_, book): UsageSummaryEntry => ({
    ...emptySummary(book),
    series: [
      seriesOf({}, [0], {estimateUsd: [usd], inputUsd: [usd], uncached: [1_000_000], turns: [2]}),
    ],
    rates: [
      {
        provider: 'claude',
        model: 'model-a',
        speed: 'standard',
        rate: {input: 5, output: 25, cacheRead: 0.5, cacheWrite: 6.25, cacheWrite1h: null},
        source: 'model',
      },
    ],
    ...extra,
  });
}

function world(usd: number, extra: Partial<UsageSummaryEntry> = {}) {
  return new FakeUsageBridge({hosts: [{host: 'local', answer: spent(usd, extra)}]});
}

function usage(bridge: FakeUsageBridge) {
  const time = new FakeClock();
  const made = createUsageStore(bridge, {
    schedule: time.schedule,
    clock: () => ({now: NOW, zone: 'UTC'}),
  });
  return {...made, time};
}

/** Let the machine leave the states that wait on the bridge, as its subscribers would see it. */
async function settle(store: ReturnType<typeof usage>['store']): Promise<void> {
  for (let turn = 0; turn < 80; turn += 1) {
    await Promise.resolve();
    const phase = store.getState().phase;
    if (phase !== 'loading' && phase !== 'refreshing' && phase !== 'rating') return;
  }
}

describe('createUsageStore', () => {
  test('nothing is asked until the page is shown', () => {
    const bridge = world(1);
    const {store, stop} = usage(bridge);
    expect(bridge.overviews).toEqual([]);
    expect(store.getState()).toMatchObject({
      phase: 'loading',
      status: 'Connecting',
      period: '7d',
      tab: 'cost',
      view: null,
    });
    stop();
  });

  test('showing the page reads the week, and the figures arrive', async () => {
    const bridge = world(4);
    const {store, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    const state = store.getState();
    expect(state).toMatchObject({phase: 'ready', status: 'Ready'});
    expect(state.view?.hero.figure).toBe('$4.00');
    expect(bridge.asked[0]?.edges).toHaveLength(8);
    unsubscribe();
    stop();
  });

  test('choosing another period forgets the figures and asks in the new buckets', async () => {
    const bridge = world(4);
    const {store, actions, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    actions.period('24h');
    expect(store.getState()).toMatchObject({period: '24h', phase: 'loading', view: null});
    await settle(store);
    expect(bridge.asked.at(-1)?.edges).toHaveLength(25);
    expect(store.getState().phase).toBe('ready');
    unsubscribe();
    stop();
  });

  test('a period chosen while another is being read is the one shown, whatever answers first', async () => {
    const bridge = world(4);
    const {store, actions, stop} = usage(bridge);
    const release = bridge.holdAnswers();
    const unsubscribe = store.subscribe(() => undefined);
    await Promise.resolve();
    actions.period('30d');
    release();
    await settle(store);
    expect(store.getState()).toMatchObject({period: '30d', phase: 'ready'});
    expect(store.getState().view?.chart.labels).toHaveLength(30);
    unsubscribe();
    stop();
  });

  test('the tab, the source and a hidden provider filter what is held and ask nothing', async () => {
    const bridge = world(4);
    const {store, actions, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    const asked = bridge.asked.length;
    actions.tab('tokens');
    expect(store.getState().view?.hero.figure).toBe('1M');
    actions.provider('claude');
    expect(store.getState().view?.hero.figure).toBe('0');
    actions.provider('claude');
    actions.source('outside');
    expect(store.getState().view?.empty).toBe(true);
    expect(bridge.asked).toHaveLength(asked);
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
    expect(store.getState()).toMatchObject({phase: 'refreshing', stale: true, view: before});
    release();
    await settle(store);
    expect(bridge.overviews).toHaveLength(2);
    expect(store.getState().phase).toBe('ready');
    second();
    stop();
  });

  test('a host with work left is asked again, and the status says Scanning meanwhile', async () => {
    let answers = 0;
    const bridge = new FakeUsageBridge({
      hosts: [
        {
          host: 'local',
          answer: (buckets, book) => {
            answers += 1;
            return {
              ...spent(answers)(buckets, book),
              pendingRuns: answers < 3 ? 5 : 0,
            };
          },
        },
      ],
    });
    const {store, stop} = usage(bridge);
    const seen: string[] = [];
    const unsubscribe = store.subscribe(() => seen.push(store.getState().status));
    for (let turn = 0; turn < 200 && store.getState().phase !== 'ready'; turn += 1) {
      await Promise.resolve();
    }
    expect(seen).toContain('Scanning');
    expect(store.getState()).toMatchObject({status: 'Ready'});
    expect(answers).toBe(3);
    unsubscribe();
    stop();
  });

  test('a machine too old for the question reads unsupported; a failure reads failed', async () => {
    const old = new FakeUsageBridge({hosts: [{host: 'local', answer: 'unsupported'}]});
    const first = usage(old);
    const unsubscribeOld = first.store.subscribe(() => undefined);
    await settle(first.store);
    expect(first.store.getState()).toMatchObject({phase: 'unsupported', view: null});
    unsubscribeOld();
    first.stop();
    const broken = new FakeUsageBridge({hosts: [], overviewFails: true});
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

  test('a host whose price list is due is asked to fetch it once, then the figures are read again', async () => {
    const stale = {
      ...emptySummary().pricing,
      status: 'cached' as const,
      refreshDue: true,
    };
    const bridge = world(1, {pricing: stale});
    const {store, stop} = usage(bridge);
    const unsubscribe = store.subscribe(() => undefined);
    await settle(store);
    for (let turn = 0; turn < 40; turn += 1) await Promise.resolve();
    expect(bridge.refreshed).toEqual([{host: 'local', force: false}]);
    expect(store.getState().phase).toBe('ready');
    unsubscribe();
    stop();
  });
});

describe('the prices dialog', () => {
  async function opened() {
    const bridge = world(4);
    const made = usage(bridge);
    const unsubscribe = made.store.subscribe(() => undefined);
    await settle(made.store);
    made.actions.setPrice('claude\u0000model-a');
    return {...made, bridge, unsubscribe};
  }

  test('opens with the price the host resolved, for every host that answered', async () => {
    const {store, unsubscribe, stop} = await opened();
    const dialog = store.getState().dialog;
    expect(dialog?.kind).toBe('prices');
    if (dialog?.kind !== 'prices') throw new Error('expected the prices dialog');
    expect(dialog.prices?.draft).toEqual(
      draftOf(
        'model-a',
        {input: 5, output: 25, cacheRead: 0.5, cacheWrite: 6.25, cacheWrite1h: null},
        '',
      ),
    );
    expect(dialog.prices?.destinations.map(each => each.host)).toEqual(['local']);
    unsubscribe();
    stop();
  });

  test('saving writes the book, shows Saved, and reads the figures again at the new revision', async () => {
    const {store, actions, bridge, unsubscribe, stop} = await opened();
    const dialog = store.getState().dialog;
    if (dialog?.kind !== 'prices' || dialog.prices === null) throw new Error('expected a dialog');
    actions.edit({...dialog.prices.draft, rate: {...dialog.prices.draft.rate, input: '7'}});
    actions.save();
    await settle(store);
    for (let turn = 0; turn < 40; turn += 1) await Promise.resolve();
    expect(bridge.bookOf('local').overrides[0]?.rate.input).toBe(7);
    const after = store.getState().dialog;
    expect(after?.kind === 'prices' ? after.prices?.destinations[0]?.result : null).toBe('Saved');
    unsubscribe();
    stop();
  });

  test('a book that moved under the page is reported and nothing is written over it', async () => {
    const {store, actions, bridge, unsubscribe, stop} = await opened();
    bridge.edit('local', {overrides: [], aliases: [{from: 'a', to: 'b'}]});
    actions.save();
    await settle(store);
    for (let turn = 0; turn < 40; turn += 1) await Promise.resolve();
    expect(bridge.bookOf('local').aliases).toEqual([{from: 'a', to: 'b'}]);
    expect(bridge.bookOf('local').overrides).toEqual([]);
    const after = store.getState().dialog;
    expect(after?.kind === 'prices' ? after.prices?.destinations[0]?.result : null).toBe(
      'Updated elsewhere',
    );
    unsubscribe();
    stop();
  });

  test('a draft that cannot be saved asks nothing', async () => {
    const {store, actions, bridge, unsubscribe, stop} = await opened();
    const dialog = store.getState().dialog;
    if (dialog?.kind !== 'prices' || dialog.prices === null) throw new Error('expected a dialog');
    actions.edit({...dialog.prices.draft, rate: {...dialog.prices.draft.rate, input: '-1'}});
    expect(store.getState().dialog).toMatchObject({prices: {canSave: false}});
    actions.save();
    expect(bridge.saves).toEqual([]);
    unsubscribe();
    stop();
  });
});
