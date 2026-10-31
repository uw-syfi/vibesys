import {describe, expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {createActor, fromPromise} from 'xstate';
import type {periodEdges} from './edges.js';
import {
  hostsDue,
  pendingWork,
  type ReadAnswer,
  type SaveRequest,
  type SaveResult,
  USAGE_REFRESH_MS,
  usageMachine,
} from './machine.js';
import {repliesFor} from './testing/generate.js';

const NOW = Date.UTC(2026, 9, 30, 15, 0, 0);
const CLOCK = {now: NOW, zone: 'UTC'};

interface Pending<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (error: Error) => void;
}

function deferred<T>(): Pending<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: Error) => void = () => undefined;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {promise, resolve, reject};
}

const answer = (pendingRuns = 0, refreshDue = false): ReadAnswer => {
  const replies = repliesFor(new Seeded(11), 7, 1).map(reply => ({
    ...reply,
    summary: {
      ...reply.summary,
      pendingRuns,
      pricing: {...reply.summary.pricing, refreshDue},
    },
  }));
  return {
    replies,
    unreachable: [],
    unsupported: [],
    connecting: [],
    names: [],
    olderRunsUnread: false,
  };
};

/** A machine whose questions are the test's to answer, one at a time. */
function harness() {
  const reads: {edges: ReturnType<typeof periodEdges>; reply: Pending<ReadAnswer>}[] = [];
  const saves: Pending<readonly SaveResult[]>[] = [];
  const refreshes: Pending<null>[] = [];
  const time = new FakeClock();
  const actor = createActor(
    usageMachine.provide({
      actors: {
        read: fromPromise(({input}) => {
          const reply = deferred<ReadAnswer>();
          reads.push({edges: input.edges, reply});
          return reply.promise;
        }),
        save: fromPromise(({input}: {input: SaveRequest}) => {
          void input;
          const reply = deferred<readonly SaveResult[]>();
          saves.push(reply);
          return reply.promise;
        }),
        refreshRates: fromPromise(() => {
          const reply = deferred<null>();
          refreshes.push(reply);
          return reply.promise;
        }),
      },
    }),
    {input: {period: '7d', clock: CLOCK}, clock: clockFrom(time.schedule)},
  );
  actor.start();
  const phase = (): string => String(actor.getSnapshot().value);
  const flush = async (): Promise<void> => {
    for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
  };
  return {actor, reads, saves, refreshes, time, phase, flush};
}

describe('usageMachine', () => {
  test('asks once on start, and shows what came back', async () => {
    const {actor, reads, phase, flush} = harness();
    expect(phase()).toBe('loading');
    expect(reads).toHaveLength(1);
    reads[0]?.reply.resolve(answer());
    await flush();
    expect(phase()).toBe('ready');
    expect(actor.getSnapshot().context.answer?.replies).toHaveLength(1);
  });

  test('a host with work left keeps the page scanning until it has none', async () => {
    const {reads, phase, flush} = harness();
    reads[0]?.reply.resolve(answer(5));
    await flush();
    expect(phase()).toBe('scanning');
    expect(reads).toHaveLength(2);
    reads[1]?.reply.resolve(answer(2));
    await flush();
    expect(phase()).toBe('scanning');
    reads[2]?.reply.resolve(answer(0));
    await flush();
    expect(phase()).toBe('ready');
    expect(pendingWork(answer(0))).toBe(false);
  });

  test('another period forgets the figures and ignores a late answer for the one left', async () => {
    const {actor, reads, phase, flush} = harness();
    actor.send({type: 'period', period: '24h', clock: CLOCK});
    expect(phase()).toBe('loading');
    expect(actor.getSnapshot().context.answer).toBeNull();
    expect(reads).toHaveLength(2);
    expect(reads[1]?.edges.edges).toHaveLength(25);
    // The week's answer arrives after the day was chosen.
    reads[0]?.reply.resolve(answer());
    await flush();
    expect(phase()).toBe('loading');
    expect(actor.getSnapshot().context.answer).toBeNull();
    reads[1]?.reply.resolve(answer());
    await flush();
    expect(phase()).toBe('ready');
    expect(actor.getSnapshot().context.period).toBe('24h');
  });

  test('a failed first question fails the page; asking again with nothing shown fails it again, not Ready', async () => {
    const {actor, reads, phase, flush} = harness();
    reads[0]?.reply.reject(new Error('nothing could be read'));
    await flush();
    expect(phase()).toBe('failed');
    expect(actor.getSnapshot().context.problem).toBe('nothing could be read');
    actor.send({type: 'refresh', clock: CLOCK});
    expect(phase()).toBe('refreshing');
    reads[1]?.reply.reject(new Error('still down'));
    await flush();
    expect(phase()).toBe('failed');
    expect(actor.getSnapshot().context.problem).toBe('still down');
  });

  test('a failed question with figures shown keeps them and says why', async () => {
    const {actor, reads, phase, flush} = harness();
    reads[0]?.reply.resolve(answer());
    await flush();
    const shown = actor.getSnapshot().context.answer;
    actor.send({type: 'refresh', clock: CLOCK});
    reads[1]?.reply.reject(new Error('timed out'));
    await flush();
    expect(phase()).toBe('ready');
    expect(actor.getSnapshot().context.answer).toBe(shown);
    expect(actor.getSnapshot().context.problem).toBe('timed out');
  });

  test('a shown page asks again after a minute, and a question nobody answers fails at its deadline', async () => {
    const {reads, phase, flush, time} = harness();
    reads[0]?.reply.resolve(answer());
    await flush();
    expect(phase()).toBe('ready');
    time.advanceBy(USAGE_REFRESH_MS);
    await flush();
    expect(phase()).toBe('refreshing');
    expect(reads).toHaveLength(2);
    time.advanceBy(60_000);
    await flush();
    // Figures were held, so the page stays readable.
    expect(phase()).toBe('ready');
  });

  test('a host whose prices are due is asked to fetch them once a visit, then the figures are read again', async () => {
    const {actor, reads, refreshes, phase, flush} = harness();
    reads[0]?.reply.resolve(answer(0, true));
    await flush();
    expect(phase()).toBe('rating');
    expect(refreshes).toHaveLength(1);
    refreshes[0]?.resolve(null);
    await flush();
    expect(phase()).toBe('refreshing');
    // The host still says a fetch is due (it could not fetch): it is not asked again this visit.
    reads[1]?.reply.resolve(answer(0, true));
    await flush();
    expect(phase()).toBe('ready');
    expect(refreshes).toHaveLength(1);
    expect(hostsDue(actor.getSnapshot().context)).toEqual([]);
  });

  test('the tab, source and hidden providers filter without asking, and a machine switched off asks again', async () => {
    const {actor, reads, flush} = harness();
    reads[0]?.reply.resolve(answer());
    await flush();
    actor.send({type: 'tab', tab: 'tokens'});
    actor.send({type: 'source', source: 'outside'});
    actor.send({type: 'provider', provider: 'claude'});
    actor.send({type: 'provider', provider: 'claude'});
    expect(actor.getSnapshot().context).toMatchObject({
      tab: 'tokens',
      source: 'outside',
      hidden: [],
    });
    expect(reads).toHaveLength(1);
    actor.send({type: 'host', host: 'local'});
    expect(reads).toHaveLength(2);
    expect(actor.getSnapshot().context.hostsOff).toEqual(['local']);
  });

  test('saving records each destination’s result, then reads the figures again', async () => {
    const {actor, reads, saves, phase, flush} = harness();
    reads[0]?.reply.resolve(answer());
    await flush();
    actor.send({
      type: 'prices',
      key: 'claude\u0000model-a',
      draft: {
        model: 'model-a',
        rate: {input: '1', output: '2', cacheRead: '', cacheWrite: ''},
        automatic: false,
        mapTo: '',
      },
      apply: ['local'],
    });
    actor.send({
      type: 'save',
      request: {
        draft: {
          model: 'model-a',
          rate: {input: '1', output: '2', cacheRead: '', cacheWrite: ''},
          automatic: false,
          mapTo: '',
        },
        destinations: [{host: 'local', book: {revision: 0, overrides: [], aliases: []}}],
      },
    });
    expect(phase()).toBe('saving');
    saves[0]?.resolve([{host: 'local', outcome: 'failed', reason: 'down'}]);
    await flush();
    expect(phase()).toBe('refreshing');
    const dialog = actor.getSnapshot().context.dialog;
    expect(dialog?.kind === 'prices' ? dialog.results : null).toEqual([
      {host: 'local', outcome: 'failed', reason: 'down'},
    ]);
    // Editing the draft clears the results, which belonged to the one before.
    if (dialog?.kind === 'prices') actor.send({type: 'edit', draft: {...dialog.draft, mapTo: 'x'}});
    const edited = actor.getSnapshot().context.dialog;
    expect(edited?.kind === 'prices' ? edited.results : null).toEqual([]);
  });
});
