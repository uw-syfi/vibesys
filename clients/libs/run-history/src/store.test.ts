import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/store/testing';
import {type HistoryRunKey, RunHistoryStore} from './index.js';
import {type FakeEvent, FakeHistorySource} from './testing/index.js';

const RUN: HistoryRunKey = {host: 'ssh:remote-host', project: '/work/queue-rs', runId: 'run-1'};

/** `count` events numbered from 1, each about `size` bytes of JSON. */
function journal(count: number, size = 40): FakeEvent[] {
  return Array.from({length: count}, (_, index) => ({
    sequence: index + 1,
    text: 'x'.repeat(size),
  }));
}

function storeOver(source: FakeHistorySource, maxBytes?: number): RunHistoryStore {
  return new RunHistoryStore({source, ...(maxBytes === undefined ? {} : {maxBytes})});
}

describe('a page of only some kinds of events', () => {
  test('is held apart from the page of every event that starts at the same place', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(10));
    source.setKind('trace', event => event.sequence % 5 === 0);
    const store = storeOver(source);
    const whole = await store.read(RUN, 1, 10);
    const trace = await store.read(RUN, 1, 10, 'trace');
    expect(whole.kind === 'page' && whole.events).toHaveLength(10);
    expect(
      trace.kind === 'page' && trace.events.map(event => (event as FakeEvent).sequence),
    ).toEqual([5, 10]);
    // Each is held: reading either again asks the host nothing.
    expect(await store.read(RUN, 1, 10)).toEqual(whole);
    expect(await store.read(RUN, 1, 10, 'trace')).toEqual(trace);
    expect(source.requests.map(request => request.kinds)).toEqual([undefined, 'trace']);
  });
});

describe('a page read twice', () => {
  test('is asked of the host once, and both readers get the same page', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(30));
    const store = storeOver(source);
    const first = await store.read(RUN, 1, 10);
    const second = await store.read(RUN, 1, 10);
    expect(second).toEqual(first);
    expect(source.requests).toHaveLength(1);
    expect(store.stats()).toMatchObject({hits: 1, misses: 1, pages: 1, runs: 1});
  });

  test('another window reading the whole run issues no host request after the first did', async () => {
    const source = new FakeHistorySource({pageBytes: 400, withTotals: true});
    source.setJournal(RUN, journal(100));
    const store = storeOver(source);
    const readAll = async (): Promise<unknown[]> => {
      const events: unknown[] = [];
      for (let from = 1; ; ) {
        const page = await store.read(RUN, from, 25);
        if (page.kind !== 'page') throw new Error('unsupported');
        events.push(...page.events);
        if (page.complete || page.next <= from) return events;
        from = page.next;
      }
    };
    const first = await readAll();
    const asked = source.requests.length;
    expect(asked).toBeGreaterThan(1);
    const second = await readAll();
    expect(second).toEqual(first);
    expect(source.requests).toHaveLength(asked);
  });

  test('is read from the host once when several windows ask while it is in flight', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(10));
    source.hold();
    const store = storeOver(source);
    const reads = [store.read(RUN, 1, 5), store.read(RUN, 1, 5), store.read(RUN, 1, 5)];
    source.release();
    const pages = await Promise.all(reads);
    expect(source.requests).toHaveLength(1);
    expect(pages[1]).toEqual(pages[0]);
    expect(pages[2]).toEqual(pages[0]);
  });
});

describe('what is not held', () => {
  test('a failed read is not held: the next read asks the host again', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(5));
    const store = storeOver(source);
    source.failNext(new Error('channel closed'));
    await expect(store.read(RUN, 1, 5)).rejects.toThrow('channel closed');
    const page = await store.read(RUN, 1, 5);
    expect(page.kind).toBe('page');
    expect(source.requests).toHaveLength(2);
  });

  test('a host without history is asked again, since it may be updated', async () => {
    const source = new FakeHistorySource();
    source.setUnsupported(true);
    const store = storeOver(source);
    expect(await store.read(RUN, 1, 5)).toEqual({kind: 'unsupported'});
    source.setUnsupported(false);
    source.setJournal(RUN, journal(3));
    expect((await store.read(RUN, 1, 5)).kind).toBe('page');
    expect(source.requests).toHaveLength(2);
  });
});

describe('invalidation', () => {
  test('a run that is live again drops its held pages', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(10));
    const store = storeOver(source);
    await store.read(RUN, 1, 5);
    store.runsLive(RUN.host, [RUN.runId, 'another-run']);
    expect(store.stats()).toMatchObject({pages: 0, bytes: 0, runs: 0});
    source.setJournal(RUN, journal(20));
    const page = await store.read(RUN, 1, 50);
    expect(page.kind === 'page' && page.events.length).toBe(20);
  });

  test('a reply that was in flight when its run was invalidated is not kept', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(10));
    source.hold();
    const store = storeOver(source);
    const reading = store.read(RUN, 1, 5);
    store.invalidateRun(RUN.host, RUN.runId);
    source.release();
    expect((await reading).kind).toBe('page');
    expect(store.stats().pages).toBe(0);
  });

  test('a host that goes away drops every one of its runs and no other host’s', async () => {
    const source = new FakeHistorySource();
    const other: HistoryRunKey = {...RUN, host: 'local'};
    const second: HistoryRunKey = {...RUN, runId: 'run-2'};
    for (const run of [RUN, other, second]) source.setJournal(run, journal(4));
    const store = storeOver(source);
    for (const run of [RUN, other, second]) await store.read(run, 1, 4);
    store.invalidateHost(RUN.host);
    expect(store.stats().runs).toBe(1);
    await store.read(other, 1, 4);
    expect(source.requests).toHaveLength(3);
  });
});

describe('the memory bound', () => {
  test('the least recently read run is dropped first', async () => {
    const source = new FakeHistorySource();
    const runs = ['a', 'b', 'c'].map(runId => ({...RUN, runId}));
    for (const run of runs) source.setJournal(run, journal(10, 100));
    // Each run's one page is about 1.2 KB: two fit, three do not.
    const store = storeOver(source, 3000);
    const [a, b, c] = runs as [HistoryRunKey, HistoryRunKey, HistoryRunKey];
    await store.read(a, 1, 10);
    await store.read(b, 1, 10);
    await store.read(a, 1, 10);
    await store.read(c, 1, 10);
    const asked = source.requests.length;
    await store.read(a, 1, 10);
    await store.read(c, 1, 10);
    expect(source.requests).toHaveLength(asked);
    await store.read(b, 1, 10);
    expect(source.requests).toHaveLength(asked + 1);
    expect(store.stats().evictions).toBeGreaterThan(0);
  });

  test('a run larger than the bound is read through and never kept', async () => {
    const source = new FakeHistorySource();
    source.setJournal(RUN, journal(10, 500));
    const store = storeOver(source, 1000);
    await store.read(RUN, 1, 10);
    await store.read(RUN, 1, 10);
    expect(source.requests).toHaveLength(2);
    expect(store.stats()).toMatchObject({bytes: 0, runs: 0});
  });
});

describe('over generated reads, the store is transparent and bounded', () => {
  test('every answer equals the host’s, and held bytes stay within the bound', async () => {
    for (let seed = 1; seed <= 60; seed += 1) {
      const random = new Seeded(seed);
      const source = new FakeHistorySource({pageBytes: 200 + random.int(800)});
      const runs = Array.from({length: 4}, (_, index) => ({...RUN, runId: `run-${index}`}));
      for (const run of runs) {
        source.setJournal(run, journal(1 + random.int(60), 20 + random.int(120)));
      }
      const maxBytes = 1500 + random.int(6000);
      const store = storeOver(source, maxBytes);
      let reads = 0;
      for (let step = 0; step < 80; step += 1) {
        const run = random.pick(runs);
        if (random.int(10) === 0) {
          store.invalidateRun(run.host, run.runId);
          continue;
        }
        const from = 1 + random.int(40);
        const max = 1 + random.int(30);
        const answered = await store.read(run, from, max);
        reads += 1;
        // The journal is immutable, so the page the host serves now is the page the store served,
        // whether it was held or read.
        expect(answered, `seed ${seed}, step ${step}`).toEqual(await source.read(run, from, max));
        expect(store.stats().bytes).toBeLessThanOrEqual(maxBytes);
      }
      const {hits, misses} = store.stats();
      expect(hits + misses).toBe(reads);
    }
  });
});
