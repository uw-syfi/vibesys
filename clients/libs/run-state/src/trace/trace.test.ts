import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, type RunTrace, traceFromSnapshot, traceSpans} from './index.js';

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

const RUN_ID = 'run-trace';

function span(id: string, parent: string | null, at: number, ended: boolean, kind?: string): Span {
  return {
    id,
    parent,
    type: id.startsWith('op:') ? 'operation' : 'flow',
    name: id,
    status: ended ? 'done' : 'running',
    started_at: 0,
    ended_at: ended ? at : null,
    label: kind === undefined ? null : {kind, title: `a ${kind}`},
  };
}

/**
 * A run's journal as the server writes it: span events for spans that start under an earlier
 * span, change and end, mixed with other events; some spans carry labels of kinds no client knows.
 */
function journal(random: Seeded): RunEvent[] {
  const events: RunEvent[] = [];
  const open: string[] = [];
  const known: string[] = [];
  let sequence = 0;
  const push = (type: string, data: RunEvent['data']) => {
    sequence += 1;
    events.push({
      protocol_version: 1,
      run_id: RUN_ID,
      sequence,
      timestamp: '2026-01-01T00:00:00Z',
      type,
      data,
    } as RunEvent);
  };
  for (let step = 0; step < 4 + random.int(30); step += 1) {
    const choice = random.int(4);
    if (choice === 0 || known.length === 0) {
      const id = `${random.int(2) === 0 ? 'flow' : 'op'}:s${known.length}`;
      const parent = known.length === 0 ? null : (known[random.int(known.length)] ?? null);
      const kind = random.int(3) === 0 ? `kind-${random.int(5)}` : undefined;
      push('span_started', {kind: 'span_started', span: span(id, parent, step, false, kind)});
      known.push(id);
      open.push(id);
    } else if (choice === 1 && open.length > 0) {
      const id = open.splice(random.int(open.length), 1)[0] as string;
      const parent = id === known[0] ? null : (known[0] ?? null);
      push('span_ended', {kind: 'span_ended', span: span(id, parent, step, true)});
    } else if (choice === 2) {
      const id = known[random.int(known.length)] as string;
      const parent = id === known[0] ? null : (known[0] ?? null);
      push('span_updated', {kind: 'span_updated', span: span(id, parent, step, false, 'later')});
    } else {
      push('round_finished', null);
    }
  }
  return events;
}

/** The snapshot the server builds at `sequence`: the latest of each span, in start order. */
function snapshotAt(events: readonly RunEvent[], sequence: number): RunSnapshot {
  const spans = new Map<string, Span>();
  for (const event of events) {
    if ((event.sequence ?? 0) > sequence) break;
    const data = event.data;
    if (data && 'span' in data) spans.set(data.span.id, data.span);
  }
  return {
    protocol_version: 1,
    run_id: RUN_ID,
    sequence,
    status: 'running',
    spans: [...spans.values()],
  } as RunSnapshot;
}

function same(trace: RunTrace, snapshot: RunSnapshot): void {
  expect(traceSpans(trace)).toEqual(snapshot.spans ?? []);
  expect(trace.sequence).toBe(snapshot.sequence);
}

describe('run trace fold', () => {
  it('folding all span events gives the snapshot, and a prefix gives the trace at its sequence', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const events = journal(new Seeded(seed));
      const last = events.at(-1)?.sequence ?? 0;
      same(foldTrace(EMPTY_TRACE, events), snapshotAt(events, last));
      const cut = new Seeded(seed * 7).int(events.length + 1);
      const prefix = events.slice(0, cut);
      const at = prefix.at(-1)?.sequence ?? 0;
      same(foldTrace(EMPTY_TRACE, prefix), snapshotAt(events, at));
      // A client that starts from the snapshot at the cut and folds every event (old ones too)
      // ends where folding everything from the start does.
      same(foldTrace(traceFromSnapshot(snapshotAt(events, at)), events), snapshotAt(events, last));
    }
  });

  it('keeps a span where it started, after its parent, whatever replaces it', () => {
    for (let seed = 1; seed <= 100; seed += 1) {
      const spans = traceSpans(foldTrace(EMPTY_TRACE, journal(new Seeded(seed))));
      const seen = new Set<string>();
      for (const each of spans) {
        if (each.parent !== null) expect(seen.has(each.parent)).toBe(true);
        seen.add(each.id);
      }
    }
  });

  it('leaves the trace unchanged by events that carry no span', () => {
    const other = {
      protocol_version: 1,
      run_id: RUN_ID,
      sequence: 1,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'round_finished',
    } as RunEvent;
    const folded = foldTrace(EMPTY_TRACE, [other]);
    expect(folded.spans.size).toBe(0);
    expect(folded.sequence).toBe(1);
  });
});
