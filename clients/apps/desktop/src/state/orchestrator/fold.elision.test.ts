import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ElisionSchema, type Item, ItemStatus, ItemsPageSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {itemOf} from '../../testing/orchestrator/builders.js';
import {type AgentFrame, applyAgentFeed} from './fold.js';
import {itemIs} from './items.js';
import {emptyState, type OrchestratorState} from './state.js';

const ID = 'item';

/** A tool item whose output is `output`; the elided copy of it has none. */
const copy = (output: string, status = ItemStatus.FINISHED): Item =>
  itemOf({
    id: ID,
    turnId: 't',
    agentId: 'a',
    ordinal: 1,
    atMs: 1_000,
    kind: 'tool',
    text: 'cmd',
    output,
    status,
  });

type Arrival =
  | {readonly how: 'page'; readonly sequence: number}
  | {readonly how: 'older'; readonly sequence: number}
  | {readonly how: 'whole'; readonly sequence: number}
  | {readonly how: 'live'; readonly sequence: number};

const WHOLE = 'the whole output';

function frameOf(arrival: Arrival): AgentFrame {
  const sequence = BigInt(arrival.sequence);
  switch (arrival.how) {
    case 'page':
      return {
        sequence,
        body: {
          case: 'page',
          value: create(ItemsPageSchema, {
            sequence,
            items: [copy('')],
            elisions: [create(ElisionSchema, {ordinal: 1, bodyBytes: 5_000n})],
          }),
        },
      };
    case 'older':
      return {
        sequence,
        body: {
          case: 'older',
          value: {
            items: [copy('')],
            elisions: [create(ElisionSchema, {ordinal: 1, bodyBytes: 5_000n})],
          },
        },
      };
    case 'whole':
      return {sequence, body: {case: 'whole', value: {item: copy(WHOLE)}}};
    case 'live':
      return {sequence, body: {case: 'item', value: copy(WHOLE)}};
  }
}

const arrivals = fc.array(
  fc.record({
    how: fc.constantFrom('page', 'older', 'whole', 'live'),
    sequence: fc.integer({min: 1, max: 20}),
  }),
  {minLength: 1, maxLength: 12},
);

const run = (list: readonly Arrival[]): OrchestratorState =>
  list.reduce((state, arrival) => applyAgentFeed(state, frameOf(arrival)), emptyState);

const outputOf = (state: OrchestratorState): string | undefined => {
  const item = state.items[ID];
  return itemIs(item, 'tool') ? item.body.value.output : undefined;
};

describe('an item a page left out the text of', () => {
  it('is whole once any whole copy has arrived, in any order and at any sequence, and elided before', () => {
    fc.assert(
      fc.property(arrivals, list => {
        const state = run(list);
        const whole = list.some(one => one.how === 'whole' || one.how === 'live');
        expect(outputOf(state)).toBe(whole ? WHOLE : '');
        expect(state.elided[ID]).toBe(whole ? undefined : 5_000);
      }),
    );
  });

  it('is whole when the whole copy comes after the page and before it, however old', () => {
    for (const order of [
      ['page', 'whole'],
      ['whole', 'page'],
    ] as const) {
      const state = run(order.map((how, at) => ({how, sequence: how === 'whole' ? 1 : 10 + at})));
      expect(outputOf(state)).toBe(WHOLE);
      expect(state.elided[ID]).toBeUndefined();
    }
  });

  it('is replaced by a finished page copy even when that left the text out, as a running copy is not final', () => {
    const running = applyAgentFeed(emptyState, {
      sequence: 5n,
      body: {case: 'item', value: copy('so far', ItemStatus.RUNNING)},
    });
    const state = applyAgentFeed(running, frameOf({how: 'page', sequence: 5}));
    expect(state.items[ID]?.status).toBe(ItemStatus.FINISHED);
    expect(state.elided[ID]).toBe(5_000);
  });
});
