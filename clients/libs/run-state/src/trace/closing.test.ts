import {describe, expect, it} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {closeSpans, isOpen, type RunEnding, type RunTrace} from './index.js';

/** mulberry32: a seeded generator, so a failing seed replays exactly (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0 || 1;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }
}

const STATUSES: readonly Span['status'][] = [
  'pending',
  'running',
  'waiting',
  'done',
  'failed',
  'stopped',
];
const ENDS: readonly RunEnding['status'][] = ['done', 'failed', 'stopped'];
const SEEDS = Array.from({length: 300}, (_, index) => index + 1);

function traceOf(spans: readonly Span[]): RunTrace {
  return {spans: new Map(spans.map(span => [span.id, span])), sequence: 7};
}

/** A trace of spans in every status, started at various times, some with attempts and a label. */
function generated(random: Seeded): Span[] {
  const spans: Span[] = [];
  for (let at = 0; at < 1 + random.int(14); at += 1) {
    const status = STATUSES[random.int(STATUSES.length)] as Span['status'];
    const started = 1000 + random.int(5000);
    const ended = status === 'done' || status === 'failed' || status === 'stopped';
    spans.push({
      id: `op:${at}`,
      parent: at === 0 ? null : `op:${random.int(at)}`,
      type: 'operation',
      name: `step ${at}`,
      status,
      started_at: started,
      ended_at: ended ? started + random.int(300) : null,
      waiting_on: status === 'waiting' ? [{kind: 'slot', ref: 'group', since: started + 5}] : [],
      attempts:
        random.int(3) === 0
          ? [{n: 1, of: 3, started_at: started + 1, ended_at: null, outcome: null}]
          : [],
      label: random.int(2) === 0 ? {kind: 'workstream', title: `idea ${at}`} : null,
    });
  }
  return spans;
}

function endingOf(random: Seeded, spans: readonly Span[]): RunEnding {
  const turns = spans
    .filter(() => random.int(3) === 0)
    .map(span => ({span_id: span.id, duration_ms: random.int(900_000)}));
  return {
    status: ENDS[random.int(ENDS.length)] as RunEnding['status'],
    reason: random.int(2) === 0 ? null : 'it broke',
    heardAt: random.int(3) === 0 ? null : 1000 + random.int(9000),
    turns: [...turns, {span_id: null, duration_ms: 5}, {span_id: 'op:0', duration_ms: null}],
  };
}

function expectEndedWith(after: Span, before: Span, ending: RunEnding, seed: number): void {
  expect(after.status, `seed ${seed}`).toBe(ending.status);
  expect(after.waiting_on).toEqual([]);
  expect(after.ended_at ?? Number.NaN).toBeGreaterThanOrEqual(ending.heardAt ?? 0);
  for (const attempt of after.attempts ?? []) expect(attempt.ended_at).not.toBeNull();
  if (ending.status === 'done') return;
  expect(after.failure).toEqual({kind: 'interruption', message: ending.reason ?? ''});
  if (before.label !== null && before.label !== undefined) {
    expect(after.label?.reason).toBe(`run_${ending.status}`);
  }
}

describe('closeSpans', () => {
  it('leaves no span open, whatever the spans and the ending', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const spans = generated(random);
      const closed = closeSpans(traceOf(spans), endingOf(random, spans));
      for (const span of closed.spans.values()) expect(isOpen(span), `seed ${seed}`).toBe(false);
    }
  });

  it('keeps every span in place, as it started, and every ended span as it was', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const spans = generated(random);
      const before = traceOf(spans);
      const closed = closeSpans(before, endingOf(random, spans));
      expect([...closed.spans.keys()], `seed ${seed}`).toEqual(spans.map(span => span.id));
      expect(closed.sequence).toBe(before.sequence);
      for (const span of spans) {
        const after = closed.spans.get(span.id) as Span;
        expect(after.started_at).toBe(span.started_at);
        expect(after.parent).toBe(span.parent);
        if (!isOpen(span)) expect(after).toBe(span);
        expect(after.ended_at ?? Number.NaN).toBeGreaterThanOrEqual(span.started_at);
      }
    }
  });

  it('ends a span that recorded its turn done, as long after it started as the turn took', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const spans = generated(random);
      const ending = endingOf(random, spans);
      const closed = closeSpans(traceOf(spans), ending);
      for (const turn of ending.turns) {
        const span = spans.find(each => each.id === turn.span_id);
        if (span === undefined || !isOpen(span) || turn.duration_ms === null) continue;
        const after = closed.spans.get(span.id) as Span;
        const took = ending.turns
          .filter(each => each.span_id === span.id && each.duration_ms !== null)
          .reduce((total, each) => total + (each.duration_ms ?? 0), 0);
        expect(after.status, `seed ${seed}`).toBe('done');
        expect(after.ended_at).toBeCloseTo(span.started_at + took / 1000, 6);
      }
    }
  });

  it('ends any other open span with the status the run ended with, no earlier than the run was heard', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const spans = generated(random);
      const ending = endingOf(random, spans);
      const turned = new Set(ending.turns.map(turn => turn.span_id));
      const closed = closeSpans(traceOf(spans), ending);
      for (const span of spans) {
        if (!isOpen(span) || turned.has(span.id)) continue;
        expectEndedWith(closed.spans.get(span.id) as Span, span, ending, seed);
      }
    }
  });

  it('is done after one pass: closing a closed trace gives it back', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const spans = generated(random);
      const ending = endingOf(random, spans);
      const closed = closeSpans(traceOf(spans), ending);
      expect(closeSpans(closed, ending)).toBe(closed);
    }
  });

  it('returns a trace with no open span as it is', () => {
    const done: Span = {
      id: 'op:1',
      parent: null,
      type: 'operation',
      name: 'x',
      status: 'done',
      started_at: 1,
      ended_at: 2,
    };
    const trace = traceOf([done]);
    expect(closeSpans(trace, {status: 'failed', reason: null, heardAt: 9, turns: []})).toBe(trace);
  });
});
