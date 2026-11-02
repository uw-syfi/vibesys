import {describe, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {DeltaSchema, ItemStatus} from '@vibesys/orchestrator-proto';
import {
  applyAgentFeed,
  applyEvent,
  applySnapshot,
  emptyState,
  type OrchestratorState,
} from '@vibesys/orchestrator-state';
import fc from 'fast-check';
import {FakeDaemon, generateScenario} from './index.js';

const scenarios = fc.record({
  seed: fc.integer({min: 1, max: 3000}),
  subagents: fc.integer({min: 0, max: 9}),
  live: fc.boolean(),
});

/** What a snapshot carries; items and receipts reach a client by other streams. */
const known = (state: OrchestratorState) => ({
  sequence: state.sequence,
  runs: state.runs,
  agents: state.agents,
  turns: state.turns,
  messages: state.messages,
});

const fold = (events: readonly import('@vibesys/orchestrator-proto').Event[], from = emptyState) =>
  events.reduce(applyEvent, from);

describe('the event fold', () => {
  test('folding any split of the stream equals folding it whole', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, cut) => {
        const {events} = generateScenario(options);
        const at = cut % (events.length + 1);
        expect(fold(events.slice(at), fold(events.slice(0, at)))).toEqual(fold(events));
      }),
      {numRuns: 80},
    );
  });

  test('reconnecting from any earlier sequence gives the same state', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), fc.nat(), (options, reached, back) => {
        const {events} = generateScenario(options);
        const upTo = reached % (events.length + 1);
        const from = back % (upTo + 1);
        const held = fold(events.slice(0, upTo));
        // The client asks for events after `from`, which repeats what it already holds.
        const resumed = fold(events.slice(from), held);
        expect(resumed).toEqual(fold(events.slice(0, Math.max(upTo, events.length))));
      }),
      {numRuns: 80},
    );
  });

  test('a snapshot at any sequence plus the events after it knows what the whole stream knows', () => {
    fc.assert(
      fc.property(scenarios, fc.nat(), (options, cut) => {
        const scenario = generateScenario(options);
        const at = cut % (scenario.events.length + 1);
        const daemon = new FakeDaemon(scenario, at);
        const joined = applySnapshot(emptyState, daemon.snapshot());
        daemon.commit(scenario.events.length);
        const caughtUp = fold(daemon.eventsAfter(joined.sequence), joined);
        expect(known(caughtUp)).toEqual(known(fold(scenario.events)));
      }),
      {numRuns: 60},
    );
  });

  test('a skipped event records a gap and changes nothing else', () => {
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

describe('progress on running items', () => {
  test('is laid over the stored item, and dropped when the final item arrives', () => {
    const scenario = generateScenario({seed: 3, subagents: 2, live: true});
    const state = fold(scenario.events);
    const running = Object.values(state.items).find(item => item.status === ItemStatus.RUNNING);
    if (running === undefined) throw new Error('the live fixture has a running item');
    const withDelta = applyAgentFeed(state, {
      case: 'delta',
      value: create(DeltaSchema, {
        agentId: running.agentId,
        itemId: running.id,
        body: {case: 'text', value: 'abc'},
      }),
    });
    expect(withDelta.overlays[running.id]?.text).toBe('abc');
    const final = applyAgentFeed(withDelta, {
      case: 'item',
      value: {...running, status: ItemStatus.FINISHED},
    });
    expect(final.overlays[running.id]).toBeUndefined();
    // Progress that arrives after the final item is ignored, and a running copy cannot undo it.
    const late = applyAgentFeed(
      applyAgentFeed(final, {
        case: 'delta',
        value: create(DeltaSchema, {
          agentId: running.agentId,
          itemId: running.id,
          body: {case: 'text', value: 'zzz'},
        }),
      }),
      {case: 'item', value: running},
    );
    expect(late).toEqual(final);
  });

  test('older pages and the live stream give the same items in any order', () => {
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
          state = applyAgentFeed(state, {case: 'older', value: page});
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
