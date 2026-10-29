import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {
  EMPTY_LEDGER,
  initialCoreState,
  NO_TOKENS,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  type TokenTally,
  totalTokens,
} from './index.js';

/** mulberry32: a seeded generator whose low bits vary (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }
}

const FIELDS = [
  ['input_tokens', 'input'],
  ['cache_read_input_tokens', 'cacheRead'],
  ['cache_creation_input_tokens', 'cacheWrite'],
  ['output_tokens', 'output'],
  ['reasoning_output_tokens', 'reasoning'],
] as const;

type Reading = Partial<Record<(typeof FIELDS)[number][0], number>>;

interface Turn {
  readonly span: string | null;
  readonly reading: Reading;
}

/** A turn's reading: each count reported or not, as a provider that omits some would. */
function reading(random: Seeded): Reading {
  const out: Reading = {};
  for (const [field] of FIELDS) {
    if (random.int(3) !== 0) out[field] = random.int(100_000);
  }
  return out;
}

function turns(random: Seeded): Turn[] {
  const count = 1 + random.int(14);
  return Array.from({length: count}, () => ({
    span: random.int(5) === 0 ? null : `span-${random.int(4)}`,
    reading: reading(random),
  }));
}

function eventsOf(list: readonly Turn[]): RunEvent[] {
  return list.map((turn, index) =>
    event(index + 1, 'turn_usage_recorded', {
      data: {
        kind: 'turn_usage_recorded',
        ...turn.reading,
        ...(turn.span === null ? {} : {span_id: turn.span}),
      },
    }),
  );
}

/** The sum of one count over `list`, or null when no turn reports it. */
function summed(list: readonly Turn[], field: (typeof FIELDS)[number][0]): number | null {
  const values = list.flatMap(turn =>
    turn.reading[field] === undefined ? [] : [turn.reading[field]],
  );
  return values.length === 0 ? null : values.reduce((a, b) => a + b, 0);
}

function tallyOf(list: readonly Turn[]): TokenTally {
  const [input, cacheRead, cacheWrite, output, reasoning] = FIELDS.map(([field]) =>
    summed(list, field),
  );
  return {
    input: input ?? null,
    cacheRead: cacheRead ?? null,
    cacheWrite: cacheWrite ?? null,
    output: output ?? null,
    reasoning: reasoning ?? null,
  };
}

const SEEDS = Array.from({length: 60}, (_, index) => index + 1);

describe('token ledger', () => {
  it('starts with nothing reported, which is not zero', () => {
    const state = initialCoreState();
    expect(state.tokens).toEqual(EMPTY_LEDGER);
    expect(state.tokens.total).toEqual(NO_TOKENS);
    expect(totalTokens(state.tokens.total)).toBeNull();
  });

  it('sums the turn events into the run total and each turn span, keeping missing counts missing', () => {
    for (const seed of SEEDS) {
      const list = turns(new Seeded(seed));
      const state = eventsOf(list).reduce(reduceEvent, initialCoreState());
      expect(state.tokens.total).toEqual(tallyOf(list));
      const spans = new Set(list.flatMap(turn => (turn.span === null ? [] : [turn.span])));
      expect(new Set(Object.keys(state.tokens.bySpan))).toEqual(spans);
      for (const span of spans) {
        expect(state.tokens.bySpan[span]).toEqual(tallyOf(list.filter(turn => turn.span === span)));
      }
    }
  });

  it('counts a turn the trace does not name in the run total only', () => {
    const state = reduceEvent(
      initialCoreState(),
      event(1, 'turn_usage_recorded', {
        data: {kind: 'turn_usage_recorded', input_tokens: 7, output_tokens: 3},
      }),
    );
    expect(totalTokens(state.tokens.total)).toBe(10);
    expect(state.tokens.bySpan).toEqual({});
  });

  it('reads a replayed history as it reads the live stream, batch and tail alike', () => {
    for (const seed of SEEDS) {
      const events = eventsOf(turns(new Seeded(seed)));
      const live = events.reduce(reduceEvent, initialCoreState());
      const batch = reduceEventBatch(initialCoreState(), events);
      expect(batch.tokens).toEqual(live.tokens);
      // A tail bootstrap: the newest events first, the older ones folded in as a prefix.
      const cut = Math.floor(events.length / 2);
      const tail = reduceEventBatch(
        initialCoreState(),
        events.slice(cut),
        undefined,
        undefined,
        cut,
      );
      const merged = reduceEventPrefix(tail, events.slice(0, cut), 0);
      expect(merged.tokens).toEqual(live.tokens);
    }
  });

  it('adds a turn that reports nothing as nothing, not as zero', () => {
    const state = reduceEvent(
      initialCoreState(),
      event(1, 'turn_usage_recorded', {
        data: {kind: 'turn_usage_recorded', span_id: 'span-1'},
      }),
    );
    expect(state.tokens.total).toEqual(NO_TOKENS);
    expect(state.tokens.bySpan['span-1']).toEqual(NO_TOKENS);
  });
});
