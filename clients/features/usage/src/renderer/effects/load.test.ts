import {describe, expect, test} from 'bun:test';
import type {UsageSummaryEntry} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {periodEdges} from '../../state/index.js';
import {
  EMPTY_BOOK,
  emptySummary,
  type FakeAnswer,
  FakeUsageBridge,
  seriesOf,
} from './fake-bridge.js';
import {readUsage, refreshRates, savePrices} from './load.js';

const NOW = Date.UTC(2026, 9, 30, 15, 0, 0);
const EDGES = periodEdges('7d', NOW, 'UTC');
const LIVE = new AbortController().signal;

/** A host that spent `usd` in the first bucket. */
function spent(usd: number): FakeAnswer {
  return (_, book): UsageSummaryEntry => ({
    ...emptySummary(book),
    series: [seriesOf({}, [0], {estimateUsd: [usd], turns: [1]})],
  });
}

const read = (bridge: FakeUsageBridge, hostsOff: readonly string[] = []) =>
  readUsage(bridge, EDGES, NOW, hostsOff, LIVE);

describe('readUsage', () => {
  test('asks each reachable machine for the period in the buckets between the edges', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', projects: ['/work/a', '/work/b'], answer: spent(2)},
        {host: 'ssh:host-a', projects: ['/srv/b'], answer: spent(3)},
      ],
    });
    const answer = await read(bridge);
    expect(answer.replies.map(each => each.host)).toEqual(['local', 'ssh:host-a']);
    expect(bridge.asked.map(each => [each.host, each.projects])).toEqual([
      ['local', ['/work/a', '/work/b']],
      ['ssh:host-a', ['/srv/b']],
    ]);
    for (const each of bridge.asked) {
      expect(each.edges).toEqual(EDGES.edges);
      expect(each.now).toBe(NOW);
    }
    expect(answer).toMatchObject({unreachable: [], unsupported: []});
  });

  test('property: up to 64 projects are all named; with more, none is, and the page says older runs are unread', async () => {
    for (const seed of SEEDS.slice(0, 30)) {
      const count = new Seeded(seed).int(150);
      const projects = Array.from({length: count}, (_, at) => `/p/${at}`);
      const bridge = new FakeUsageBridge({hosts: [{host: 'local', projects, answer: spent(1)}]});
      const answer = await read(bridge);
      const named = bridge.asked[0]?.projects ?? [];
      // The host reads every run it indexed whatever it is told, so the figure is whole for runs it
      // holds an index for; only a project that cannot be named leaves the older ones unread.
      expect(named.length, `seed ${seed}`).toBe(count > 64 ? 0 : count);
      expect([...named].sort(), `seed ${seed}`).toEqual(count > 64 ? [] : [...projects].sort());
      expect(answer.olderRunsUnread, `seed ${seed}`).toBe(count > 64);
    }
  });

  test('a machine that is down, old or failing is counted and the others are read', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(5)},
        {host: 'ssh:host-a', answer: 'unsupported'},
        {host: 'ssh:host-b', answer: 'fails'},
        {host: 'ssh:host-c', answer: spent(1)},
      ],
      unavailable: ['ssh:host-c'],
    });
    const answer = await read(bridge);
    expect(answer.replies.map(each => each.host)).toEqual(['local']);
    expect(answer.unsupported).toEqual(['ssh:host-a']);
    expect([...answer.unreachable].sort()).toEqual(['ssh:host-b', 'ssh:host-c']);
  });

  test('a machine switched off is not asked', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(1)},
        {host: 'ssh:host-a', answer: spent(1)},
      ],
    });
    const answer = await read(bridge, ['ssh:host-a']);
    expect(bridge.asked.map(each => each.host)).toEqual(['local']);
    expect(answer.unreachable).toEqual([]);
  });

  test('a machine still connecting is waited for, not counted as down', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(1)},
        {host: 'ssh:host-a', answer: spent(2)},
      ],
      connecting: ['ssh:host-a'],
    });
    const answer = await read(bridge);
    expect(answer.replies.map(each => each.host)).toEqual(['local']);
    expect(answer).toMatchObject({connecting: ['ssh:host-a'], unreachable: []});
    const only = new FakeUsageBridge({
      hosts: [{host: 'local', answer: spent(1)}],
      connecting: ['local'],
    });
    expect(await read(only)).toMatchObject({replies: [], connecting: ['local']});
  });

  test('a catalogue that is not ready is asked for once more, and a missing one is left to the view', async () => {
    const listed = {
      kind: 'listed' as const,
      harnesses: [
        {
          provider: 'claude',
          model: 'claude-opus-5-5',
          default: true,
          display: {providerName: 'Claude Code', modelName: 'Opus 5.5'},
        },
      ],
    };
    const late = new FakeUsageBridge({
      hosts: [{host: 'local', answer: spent(1)}],
      harnesses: {local: listed},
      harnessesFailing: 1,
    });
    expect((await read(late)).names.map(each => each.modelName)).toEqual(['Opus 5.5']);
    expect(late.catalogues).toHaveLength(2);
    const never = new FakeUsageBridge({
      hosts: [{host: 'local', answer: spent(1)}],
      harnesses: {local: listed},
      harnessesFailing: 5,
    });
    expect((await read(never)).names).toEqual([]);
  });

  test('when nothing could be read the question fails, naming why', async () => {
    const down = new FakeUsageBridge({
      hosts: [{host: 'local', answer: 'fails'}],
      unavailable: ['ssh:host-a'],
    });
    await expect(read(down)).rejects.toThrow('nothing could be read');
    const none = new FakeUsageBridge({hosts: [], unavailable: ['ssh:host-a']});
    await expect(read(none)).rejects.toThrow('nothing could be read');
    const listing = new FakeUsageBridge({hosts: [], overviewFails: true});
    await expect(read(listing)).rejects.toThrow('could not be listed');
  });

  test('every machine being old is an answer, not a failure', async () => {
    const bridge = new FakeUsageBridge({hosts: [{host: 'local', answer: 'unsupported'}]});
    expect(await read(bridge)).toMatchObject({replies: [], unsupported: ['local']});
  });

  test('no machine at all is an answer of nothing', async () => {
    expect(await read(new FakeUsageBridge({hosts: []}))).toMatchObject({replies: []});
  });

  test('a model is named by the catalogue of the machine that lists it', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [{host: 'local', answer: spent(1)}],
      harnesses: {
        local: {
          kind: 'listed',
          harnesses: [
            {
              provider: 'claude',
              model: 'claude-opus-5-5',
              default: true,
              display: {providerName: 'Claude Code', modelName: 'Opus 5.5'},
            },
            {provider: 'codex', model: 'gpt-6.1-luna', default: false},
          ],
        },
      },
    });
    expect((await read(bridge)).names).toEqual([
      {
        harness: 'claude',
        model: 'claude-opus-5-5',
        harnessName: 'Claude Code',
        modelName: 'Opus 5.5',
      },
      {harness: 'codex', model: 'gpt-6.1-luna', harnessName: null, modelName: null},
    ]);
  });

  test('a question nobody wants any more stops before its answers are used', async () => {
    const bridge = new FakeUsageBridge({hosts: [{host: 'local', answer: spent(1)}]});
    const stop = new AbortController();
    const release = bridge.holdAnswers();
    const pending = readUsage(bridge, EDGES, NOW, [], stop.signal);
    stop.abort();
    release();
    await expect(pending).rejects.toThrow();
  });
});

describe('refreshRates', () => {
  test('asks each named machine, and a failure on one is not a failure of the page', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(1)},
        {host: 'ssh:host-a', answer: spent(1)},
      ],
      unavailable: ['ssh:host-a'],
    });
    await refreshRates(bridge, ['local', 'ssh:host-a'], NOW, LIVE);
    expect(bridge.refreshed.map(each => each.host)).toEqual(['local', 'ssh:host-a']);
  });
});

describe('savePrices', () => {
  const rate = {input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75, cacheWrite1h: null};
  const draft = {
    model: 'claude-opus-5-5',
    rate: {input: '3', output: '15', cacheRead: '0.3', cacheWrite: '3.75'},
    automatic: false,
    mapTo: '',
  };

  test('saves the change to each destination against that destination’s own book', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(1), book: {...EMPTY_BOOK, revision: 4}},
        {host: 'ssh:host-a', answer: spent(1), book: EMPTY_BOOK},
      ],
    });
    const results = await savePrices(
      bridge,
      {
        draft,
        destinations: [
          {host: 'local', book: bridge.bookOf('local')},
          {host: 'ssh:host-a', book: bridge.bookOf('ssh:host-a')},
        ],
      },
      LIVE,
    );
    expect(results.map(each => [each.host, each.outcome])).toEqual([
      ['local', 'saved'],
      ['ssh:host-a', 'saved'],
    ]);
    expect(bridge.saves.map(each => each.baseRevision)).toEqual([4, 0]);
    expect(bridge.bookOf('local').overrides).toEqual([{model: 'claude-opus-5-5', rate}]);
  });

  test('a destination whose book moved is reported, and the others are still saved', async () => {
    const bridge = new FakeUsageBridge({
      hosts: [
        {host: 'local', answer: spent(1)},
        {host: 'ssh:host-a', answer: spent(1)},
        {host: 'ssh:host-b', answer: spent(1)},
      ],
    });
    const books = ['local', 'ssh:host-a', 'ssh:host-b'].map(host => ({
      host,
      book: bridge.bookOf(host),
    }));
    bridge.edit('ssh:host-a', {overrides: [], aliases: []});
    bridge.failSaves('ssh:host-b');
    const results = await savePrices(bridge, {draft, destinations: books}, LIVE);
    expect(results.map(each => each.outcome)).toEqual(['saved', 'moved', 'failed']);
  });

  test('a change that makes a book no host would accept is not sent', async () => {
    const bridge = new FakeUsageBridge({hosts: [{host: 'local', answer: spent(1)}]});
    const results = await savePrices(
      bridge,
      {
        draft: {...draft, mapTo: 'claude-opus-5-5'},
        destinations: [{host: 'local', book: EMPTY_BOOK}],
      },
      LIVE,
    );
    expect(results[0]).toMatchObject({outcome: 'failed'});
    expect(bridge.saves).toEqual([]);
  });

  test('saving the same change twice leaves the book as it was after the first', async () => {
    const bridge = new FakeUsageBridge({hosts: [{host: 'local', answer: spent(1)}]});
    const once = await savePrices(
      bridge,
      {draft, destinations: [{host: 'local', book: bridge.bookOf('local')}]},
      LIVE,
    );
    const first = bridge.bookOf('local');
    await savePrices(bridge, {draft, destinations: [{host: 'local', book: first}]}, LIVE);
    expect(once[0]).toMatchObject({outcome: 'saved'});
    expect(bridge.bookOf('local')).toEqual(first);
  });
});
