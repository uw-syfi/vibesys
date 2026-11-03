import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  DeltaSchema,
  type Event,
  EventSchema,
  type Item,
  ItemSchema,
  ItemStatus,
  ItemsPageSchema,
  ReceiptSchema,
  type WatchAgentResponse,
  WatchAgentResponseSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {FakeDaemon, generateScenario} from '../../testing/orchestrator/index.js';
import {applyAgentFeed, applyEvent, applySnapshot} from './fold.js';
import {emptyState, type OrchestratorState} from './state.js';

const scenarios = fc.record({
  seed: fc.integer({min: 1, max: 3000}),
  subagents: fc.integer({min: 0, max: 9}),
  live: fc.boolean(),
});

const fold = (events: readonly Event[], from: OrchestratorState = emptyState) =>
  events.reduce(applyEvent, from);

const NO_GAP = (state: OrchestratorState) => ({...state, gap: null});

/** What a snapshot carries; items and receipts reach a client by other streams. */
const known = (state: OrchestratorState) => ({
  sequence: state.sequence,
  runs: state.runs,
  agents: state.agents,
  turns: state.turns,
  messages: state.messages,
});

/** Delivery orders: the stream cut at an arbitrary place, with arbitrary repeats and reorderings. */
const delivery = (events: readonly Event[]) =>
  fc.array(fc.nat({max: Math.max(0, events.length - 1)}), {maxLength: events.length * 3});

/** Two copies of one item chosen by `a` and `b`, the one committed earlier first. */
function pairOf<T>(
  copies: readonly T[],
  a: number,
  b: number,
): readonly [T | undefined, T | undefined] {
  const i = a % copies.length;
  const j = b % copies.length;
  return i <= j ? [copies[i], copies[j]] : [copies[j], copies[i]];
}

/** What a client holds after the agent stream delivers these copies, in this order. */
function deliver(copies: readonly {item: Item; sequence: bigint}[]): OrchestratorState {
  return copies.reduce(
    (held, copy) => applyAgentFeed(held, frame({case: 'item', value: copy.item}, copy.sequence)),
    emptyState,
  );
}

describe('the event fold', () => {
  it('folding any split of the stream equals folding it whole', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, cut) => {
        const {events} = generateScenario(options);
        const at = cut % (events.length + 1);
        expect(fold(events.slice(at), fold(events.slice(0, at)))).toEqual(fold(events));
      }),
      {numRuns: 80},
    );
  });

  it('reconnecting from any earlier sequence, and stopping anywhere, gives the state of the furthest event', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), fc.nat(), fc.nat(), (options, reached, back, stop) => {
        const {events} = generateScenario(options);
        const upTo = reached % (events.length + 1);
        const from = back % (upTo + 1);
        const end = from + (stop % (events.length - from + 1));
        const held = fold(events.slice(0, upTo));
        // The client asks for events after `from`, which repeats what it already holds, and the
        // connection ends after `end`.
        const resumed = fold(events.slice(from, end), held);
        expect(resumed).toEqual(fold(events.slice(0, Math.max(upTo, end))));
      }),
      {numRuns: 80},
    );
  });

  it('events shuffled and repeated give the state of the longest in-order prefix, whole', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, salt) => {
        const {events} = generateScenario(options);
        fc.assert(
          fc.property(delivery(events), picks => {
            const received = picks.map(pick => events[(pick + salt) % events.length] as Event);
            const state = fold(received);
            // Whatever arrived, the state is the fold of exactly the events up to its sequence.
            expect(NO_GAP(state)).toEqual(NO_GAP(fold(events.slice(0, Number(state.sequence)))));
            if (state.gap !== null) expect(state.gap.expected).toBe(state.sequence + 1n);
          }),
          {numRuns: 15},
        );
      }),
      {numRuns: 12},
    );
  });

  it('a skipped event records a gap and changes nothing else', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, cut) => {
        const {events} = generateScenario(options);
        if (events.length < 3) return;
        const at = 1 + (cut % (events.length - 2));
        const held = fold(events.slice(0, at));
        const skipped = applyEvent(held, events[at + 1] as never);
        expect({...skipped, gap: null}).toEqual({...held, gap: null});
        expect(skipped.gap).toEqual({expected: BigInt(at + 1), got: BigInt(at + 2)});
        // The skipped event arriving later closes the gap.
        expect(applyEvent(skipped, events[at] as never).gap).toBeNull();
      }),
      {numRuns: 60},
    );
  });
});

describe('a snapshot', () => {
  it('over a state the client already holds keeps what it does not carry and ends with the whole run', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), fc.nat(), (options, held, joined) => {
        const scenario = generateScenario(options);
        const total = scenario.events.length;
        const a = held % (total + 1);
        const b = a + (joined % (total - a + 1));
        // The client holds events up to `a`, and the answer to a command no snapshot carries.
        const receipt = create(ReceiptSchema, {commandId: 'c-held', commandDigest: 'a'.repeat(64)});
        const before = {...fold(scenario.events.slice(0, a)), receipts: {'c-held': receipt}};
        // A resync reads a snapshot current to `b`, then the events after it.
        const daemon = new FakeDaemon(scenario, b);
        const snapshot = daemon.snapshot();
        const resynced = applySnapshot(before, snapshot);
        expect(resynced.receipts).toEqual(before.receipts);
        expect(resynced.items).toEqual(before.items);
        expect(resynced.overlays).toEqual(before.overlays);
        // The snapshot is a read, so reading it twice changes nothing.
        expect(applySnapshot(resynced, snapshot)).toEqual(resynced);
        daemon.commit(total);
        const caughtUp = fold(daemon.eventsAfter(resynced.sequence), resynced);
        expect(known(caughtUp)).toEqual(known(fold(scenario.events)));
      }),
      {numRuns: 60},
    );
  });

  it('older than what is applied changes nothing', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, cut) => {
        const scenario = generateScenario(options);
        const total = scenario.events.length;
        const at = 1 + (cut % total);
        const stale = new FakeDaemon(scenario, at - 1).snapshot();
        const held = fold(scenario.events.slice(0, at));
        expect(applySnapshot(held, stale)).toEqual({...held, searches: {}});
      }),
      {numRuns: 40},
    );
  });

  it('older than what is applied closes a gap, so a daemon whose sequence restarted is followed', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), fc.nat(), (options, cut, back) => {
        const scenario = generateScenario(options);
        const total = scenario.events.length;
        fc.pre(total >= 3);
        // The client holds events up to `at` and then sees one that skips the next.
        const at = 1 + (cut % (total - 2));
        const gapped = applyEvent(
          fold(scenario.events.slice(0, at)),
          scenario.events[at + 1] as Event,
        );
        expect(gapped.gap).not.toBeNull();
        // The daemon answers the resync from a sequence below the one held.
        const restarted = new FakeDaemon(scenario, back % at).snapshot();
        const resynced = applySnapshot(gapped, restarted);
        expect(resynced.gap).toBeNull();
        expect(resynced.sequence).toBe(restarted.sequence);
        const fresh = applySnapshot(emptyState, restarted);
        expect(resynced.runs).toEqual(fresh.runs);
        expect(resynced.agents).toEqual(fresh.agents);
        expect(resynced.messages).toEqual(fresh.messages);
      }),
      {numRuns: 60},
    );
  });
});

/** A hand-made history of items: each has running versions, and maybe a final one, committed in any order. */
interface Versioned {
  readonly events: readonly Event[];
  /** For each item id, its versions in commit order. */
  readonly versions: ReadonlyMap<
    string,
    readonly {readonly item: Item; readonly sequence: bigint}[]
  >;
}

function itemVersion(id: string, ordinal: number, version: number, final: boolean): Item {
  return create(ItemSchema, {
    id,
    turnId: 'turn-1',
    agentId: 'agent-1',
    ordinal,
    status: final ? ItemStatus.FINISHED : ItemStatus.RUNNING,
    at: timestampFromMs(1000 * ordinal),
    body: {case: 'assistant', value: {text: `${id} v${version} ${'x'.repeat(version)}`}},
  });
}

const histories: fc.Arbitrary<Versioned> = fc
  .array(fc.record({running: fc.integer({min: 1, max: 3}), final: fc.boolean()}), {
    minLength: 1,
    maxLength: 4,
  })
  .chain(shapes => {
    const tokens = shapes.flatMap((shape, index) =>
      Array.from({length: shape.running + (shape.final ? 1 : 0)}, () => index),
    );
    return fc
      .shuffledSubarray(tokens, {minLength: tokens.length, maxLength: tokens.length})
      .map((order): Versioned => {
        const next = new Map<number, number>();
        const versions = new Map<string, {item: Item; sequence: bigint}[]>();
        const events = order.map((index, position) => {
          const shape = shapes[index] as (typeof shapes)[number];
          const version = next.get(index) ?? 0;
          next.set(index, version + 1);
          const id = `item-${index}`;
          const item = itemVersion(
            id,
            index + 1,
            version,
            shape.final && version === shape.running,
          );
          const sequence = BigInt(position + 1);
          versions.set(id, [...(versions.get(id) ?? []), {item, sequence}]);
          return create(EventSchema, {
            sequence,
            at: timestampFromMs(1000 * (position + 1)),
            body: {case: 'itemUpdated', value: item},
          });
        });
        return {events, versions};
      });
  });

const frame = (body: WatchAgentResponse['body'], sequence: bigint): WatchAgentResponse =>
  create(WatchAgentResponseSchema, {body, sequence});

/** What the agent stream sends after opening at `cut`: a page of the newest versions, then each later one with its progress. */
function agentSession(history: Versioned, cut: number): WatchAgentResponse[] {
  const newest = new Map<string, Item>();
  for (const event of history.events.slice(0, cut)) {
    if (event.body.case === 'itemUpdated') newest.set(event.body.value.id, event.body.value);
  }
  const page = create(ItemsPageSchema, {items: [...newest.values()], sequence: BigInt(cut)});
  const later = history.events.slice(cut).flatMap(event => {
    if (event.body.case !== 'itemUpdated') return [];
    const progress =
      event.body.value.status === ItemStatus.RUNNING
        ? [
            frame(
              {
                case: 'delta',
                value: create(DeltaSchema, {
                  agentId: 'agent-1',
                  itemId: event.body.value.id,
                  body: {case: 'text', value: `+${event.sequence}`},
                }),
              },
              event.sequence,
            ),
          ]
        : [];
    return [frame({case: 'item', value: event.body.value}, event.sequence), ...progress];
  });
  return [frame({case: 'page', value: page}, BigInt(cut)), ...later];
}

type Step =
  | {readonly from: 'run'; readonly event: Event}
  | {readonly from: 'agent'; readonly frame: WatchAgentResponse};

/** Both streams, each in its own order and with repeats, interleaved by `turns`. */
function interleave(
  run: readonly Event[],
  agent: readonly WatchAgentResponse[],
  turns: readonly boolean[],
): Step[] {
  const steps: Step[] = [];
  let r = 0;
  let a = 0;
  for (const takeRun of turns) {
    if ((takeRun && r < run.length) || a >= agent.length) {
      if (r < run.length) steps.push({from: 'run', event: run[r++] as Event});
    } else steps.push({from: 'agent', frame: agent[a++] as WatchAgentResponse});
  }
  while (r < run.length) steps.push({from: 'run', event: run[r++] as Event});
  while (a < agent.length) steps.push({from: 'agent', frame: agent[a++] as WatchAgentResponse});
  return steps;
}

const play = (steps: readonly Step[]): OrchestratorState =>
  steps.reduce(
    (state, step) =>
      step.from === 'run' ? applyEvent(state, step.event) : applyAgentFeed(state, step.frame),
    emptyState,
  );

/** Repeat the entries `dups` marks. */
const repeated = <T>(items: readonly T[], dups: readonly boolean[]): T[] =>
  items.flatMap((item, index) => (dups[index % Math.max(1, dups.length)] ? [item, item] : [item]));

describe('whole items from the run stream and the agent stream', () => {
  const plans = fc.record({
    history: histories,
    firstCut: fc.nat(),
    secondCut: fc.nat(),
    runDups: fc.array(fc.boolean(), {maxLength: 12}),
    agentDups: fc.array(fc.boolean(), {maxLength: 12}),
    turns: fc.array(fc.boolean(), {maxLength: 40}),
    redial: fc.boolean(),
  });

  it('every item ends as its newest version, however the streams interleave, repeat or restart', () => {
    fc.assert(
      fc.property(plans, ({history, firstCut, secondCut, runDups, agentDups, turns, redial}) => {
        const total = history.events.length;
        const first = firstCut % (total + 1);
        const second = first + (secondCut % (total - first + 1));
        // A redial opens a second session at a later cut, and the first one's frames are not repeated.
        const agent = [
          ...agentSession(history, first).slice(0, redial ? 1 + (secondCut % total) : undefined),
          ...(redial ? agentSession(history, second) : []),
        ];
        const state = play(
          interleave(repeated(history.events, runDups), repeated(agent, agentDups), turns),
        );
        for (const [id, versions] of history.versions) {
          const newest = versions[versions.length - 1];
          expect(state.items[id]).toEqual(newest?.item);
        }
      }),
      {numRuns: 300},
    );
  });

  it('a final item is never replaced by a running one, and a running one by an older running one', () => {
    fc.assert(
      fc.property(histories, fc.nat(), fc.nat(), ({events, versions}, a, b) => {
        // Every pair of copies of one item, delivered in either order.
        for (const [id, copies] of versions) {
          const [older, newer] = pairOf(copies, a, b);
          if (older === undefined || newer === undefined) continue;
          for (const order of [
            [older, newer],
            [newer, older],
          ] as const) {
            expect(deliver(order).items[id]).toEqual(newer.item);
          }
        }
        expect(events.length).toBeGreaterThan(0);
      }),
      {numRuns: 100},
    );
  });

  it('progress is only what the stored item does not hold, and never survives a final item', () => {
    fc.assert(
      fc.property(plans, ({history, firstCut, runDups, agentDups, turns}) => {
        const first = firstCut % (history.events.length + 1);
        const agent = agentSession(history, first);
        const steps = interleave(
          repeated(history.events, runDups),
          repeated(agent, agentDups),
          turns,
        );
        // Check after every step: progress never repeats text the stored item already holds.
        let state = emptyState;
        for (const step of steps) {
          state =
            step.from === 'run' ? applyEvent(state, step.event) : applyAgentFeed(state, step.frame);
          for (const [id, overlay] of Object.entries(state.overlays)) {
            const stored = state.items[id];
            const stamp = state.itemSequences[id] ?? 0n;
            expect(stored?.status).not.toBe(ItemStatus.FINISHED);
            expect(overlay.pieces.every(piece => piece.sequence >= stamp)).toBe(true);
            expect(overlay.text).toBe(overlay.pieces.map(piece => piece.text).join(''));
          }
        }
      }),
      {numRuns: 300},
    );
  });
});

describe('progress on running items', () => {
  it('is laid over the stored item, and dropped when the final item arrives', () => {
    const scenario = generateScenario({seed: 3, subagents: 2, live: true});
    const state = fold(scenario.events);
    const running = Object.values(state.items).find(item => item.status === ItemStatus.RUNNING);
    if (running === undefined) throw new Error('the live fixture has a running item');
    const at = state.sequence;
    const progress = (text: string, sequence: bigint) =>
      frame(
        {
          case: 'delta',
          value: create(DeltaSchema, {
            agentId: running.agentId,
            itemId: running.id,
            body: {case: 'text', value: text},
          }),
        },
        sequence,
      );
    const withDelta = applyAgentFeed(state, progress('abc', at));
    expect(withDelta.overlays[running.id]?.text).toBe('abc');
    const final = applyAgentFeed(
      withDelta,
      frame({case: 'item', value: {...running, status: ItemStatus.FINISHED}}, at + 1n),
    );
    expect(final.overlays[running.id]).toBeUndefined();
    // Progress that arrives after the final item is ignored, and a running copy cannot undo it.
    const late = applyAgentFeed(
      applyAgentFeed(final, progress('zzz', at + 1n)),
      frame({case: 'item', value: running}, at + 2n),
    );
    expect(late).toEqual(final);
  });

  it('made before the stored item was committed is already in it and is not added again', () => {
    const scenario = generateScenario({seed: 3, subagents: 2, live: true});
    const state = fold(scenario.events);
    const running = Object.values(state.items).find(item => item.status === ItemStatus.RUNNING);
    if (running === undefined) throw new Error('the live fixture has a running item');
    const stamp = state.itemSequences[running.id] ?? 0n;
    const stale = applyAgentFeed(
      state,
      frame(
        {
          case: 'delta',
          value: create(DeltaSchema, {
            agentId: running.agentId,
            itemId: running.id,
            body: {case: 'text', value: 'already in the item'},
          }),
        },
        stamp - 1n,
      ),
    );
    expect(stale).toBe(state);
  });

  it('older pages and the live stream give the same items in any order', () => {
    fc.assert(
      fc.property(scenarios, fc.integer({min: 1, max: 6}), (options, pageSize) => {
        const scenario = generateScenario(options);
        const daemon = new FakeDaemon(scenario);
        const agent = Object.keys(fold(scenario.events).agents)[0];
        if (agent === undefined) return;
        let state = fold(scenario.events.filter(event => event.body.case !== 'itemUpdated'));
        let before = 0;
        for (;;) {
          const page = daemon.page(agent, pageSize, before);
          state = applyAgentFeed(state, {
            body: {case: 'older', value: page},
            sequence: page.sequence,
          });
          if (page.nextBeforeOrdinal === 0) break;
          before = page.nextBeforeOrdinal;
        }
        const mine = (items: OrchestratorState['items']) =>
          Object.values(items)
            .filter(item => item.agentId === agent)
            .map(item => item.id)
            .sort();
        expect(mine(state.items)).toEqual(mine(fold(scenario.events).items));
      }),
      {numRuns: 40},
    );
  });
});
