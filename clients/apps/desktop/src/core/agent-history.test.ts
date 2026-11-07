import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import {transcript, unbrokenFrom} from '../state/index.js';
import {
  ELIDE_OVER,
  FakeDaemon,
  faultsOn,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import {AGENT_PAGE_SIZE} from './policy.js';

const scenario = generateScenario({seed: 3, subagents: 9, live: false, tree: true, depth: 40});

/** The agent with the longest history, and how many items it has. */
const busiest = (daemon: FakeDaemon): {id: string; items: number} =>
  daemon
    .snapshot()
    .agents.map(agent => ({id: agent.id, items: daemon.page(agent.id, 1000, 0).items.length}))
    .reduce((most, one) => (one.items > most.items ? one : most));

const callsTo = (h: HostHarness, method: string) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf(method));

const itemsOf = (h: HostHarness, id: string) =>
  h.model.pipe(
    Effect.map(model => Object.values(model.data.items).filter(item => item.agentId === id)),
  );

/** An agent open on the first page, with the run stream read so the agent is known. */
const opened = (daemon: FakeDaemon, wrap?: Parameters<typeof hostHarness>[1]) =>
  Effect.gen(function* () {
    const h = yield* hostHarness(daemon, wrap);
    const {id, items} = busiest(daemon);
    h.mount(h.session.runs.watch);
    h.mount(h.session.agents.watch(id));
    yield* h.elapse(5);
    return {h, id, items};
  });

describe('reading earlier items', () => {
  it.effect('pages back from the first page to the very first item, each item once', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const {h, id, items} = yield* opened(daemon);
      expect(items).toBeGreaterThan(AGENT_PAGE_SIZE * 2);
      expect(yield* itemsOf(h, id)).toHaveLength(AGENT_PAGE_SIZE);
      for (let pages = 0; pages < 20; pages++) {
        const from = unbrokenFrom((yield* h.model).data, id);
        if (from === null || from <= 1) break;
        yield* h.session.history.loadEarlier(id, from);
      }
      const loaded = yield* itemsOf(h, id);
      expect(loaded.map(item => item.ordinal).sort((a, b) => a - b)).toEqual(
        Array.from({length: items}, (_, at) => at + 1),
      );
      expect(transcript((yield* h.model).data, id, 0)?.earlier).toBeNull();
    }),
  );

  it.effect('reads once when asked twice at the same time, and moves no stream cursor', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const {h, id} = yield* opened(daemon);
      const cursor = (yield* h.model).agentCursors[id];
      const from = unbrokenFrom((yield* h.model).data, id) ?? 1;
      yield* Effect.all(
        [h.session.history.loadEarlier(id, from), h.session.history.loadEarlier(id, from)],
        {concurrency: 'unbounded'},
      );
      expect(callsTo(h, 'ListItems')).toHaveLength(1);
      expect((yield* h.model).agentCursors[id]).toBe(cursor);
      expect(yield* h.read(h.session.history.earlier(id))).toEqual({_tag: 'Idle'});
    }),
  );

  it.effect('says a read failed, and reads again when asked again', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const {h, id} = yield* opened(daemon, {
        wrap: faultsOn('ListItems', {dropAfter: [0], duplicate: []}),
      });
      const from = unbrokenFrom((yield* h.model).data, id) ?? 1;
      yield* h.session.history.loadEarlier(id, from);
      expect((yield* h.read(h.session.history.earlier(id)))._tag).toBe('Failed');
      expect(yield* itemsOf(h, id)).toHaveLength(AGENT_PAGE_SIZE);
      yield* h.session.history.loadEarlier(id, from);
      expect(yield* h.read(h.session.history.earlier(id))).toEqual({_tag: 'Idle'});
      expect((yield* itemsOf(h, id)).length).toBeGreaterThan(AGENT_PAGE_SIZE);
    }),
  );
});

describe('reading an item whole', () => {
  it.effect('fills in the text a page left out, only for the item asked for', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const {h, id} = yield* opened(daemon);
      const elided = Object.keys((yield* h.model).data.elided);
      expect(elided.length).toBeGreaterThan(0);
      const first = (yield* h.model).data.items[elided[0] ?? ''];
      if (first === undefined) throw new Error('the item is loaded');
      yield* h.session.history.expand(id, first.ordinal);
      const data = (yield* h.model).data;
      expect(data.elided[first.id]).toBeUndefined();
      expect(Object.keys(data.elided)).toHaveLength(elided.length - 1);
      const body = data.items[first.id]?.body;
      const whole =
        body?.case === 'tool'
          ? body.value.output
          : body?.case === 'reasoning'
            ? body.value.text
            : '';
      expect(new TextEncoder().encode(whole).length).toBeGreaterThan(ELIDE_OVER);
    }),
  );

  it.effect('says so when the item is not there', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const {h, id} = yield* opened(daemon);
      yield* h.session.history.expand(id, 99_999);
      const status = yield* h.read(h.session.history.whole(id, 99_999));
      expect(status._tag).toBe('Failed');
    }),
  );
});
