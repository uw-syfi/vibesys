import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/backend-client';
import {event} from '@vibesys/backend-client/testing';
import {
  type CoreState,
  hasRunEnded,
  initialCoreState,
  reduceEventBatch,
  reduceResponseEvents,
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

const RUN_ID = 'run-20261010-0001';

/**
 * One run's journal: events before the run is ready carry the empty run id (no stand-in), then
 * the real id; the run may end with a terminal event.
 */
function journal(random: Seeded): RunEvent[] {
  const length = 4 + random.int(20);
  const ready = random.int(length);
  const ends = random.int(3) === 0;
  const events: RunEvent[] = [];
  for (let sequence = 1; sequence <= length; sequence += 1) {
    const runId = sequence <= ready ? '' : RUN_ID;
    const last = ends && sequence === length;
    events.push(
      last
        ? event(sequence, random.int(2) === 0 ? 'run_failed' : 'run_interrupted', {run_id: runId})
        : event(sequence, 'agent_output_chunk', {
            run_id: runId,
            invocation_id: `turn-${sequence % 3}`,
            data: {kind: 'agent_output_chunk', channel: 'assistant', content: `chunk ${sequence}`},
          }),
    );
  }
  return events;
}

/**
 * Deliver `events` the ways a reconnecting client sees them: subscription batches that resume
 * from a cursor at or before what was folded (duplicates on reconnect), and RPC responses that
 * carry later events out of band.
 */
function deliveries(
  random: Seeded,
  events: readonly RunEvent[],
): {route: 'stream' | 'response'; events: RunEvent[]}[] {
  const out: {route: 'stream' | 'response'; events: RunEvent[]}[] = [];
  let cursor = 0;
  while (cursor < events.length) {
    const resumeFrom = Math.max(0, cursor - random.int(3));
    const end = Math.min(events.length, cursor + 1 + random.int(5));
    if (random.int(4) === 0 && end < events.length) {
      const ahead = end + random.int(events.length - end);
      out.push({route: 'response', events: events.slice(end, ahead + 1)});
    }
    out.push({route: 'stream', events: events.slice(resumeFrom, end)});
    cursor = end;
  }
  return out;
}

function check(before: CoreState, after: CoreState): void {
  expect(after.sequence).toBeGreaterThanOrEqual(before.sequence);
  expect([null, RUN_ID]).toContain(after.runId);
  if (before.runId !== null) expect(after.runId).toBe(before.runId);
  if (hasRunEnded(before)) expect(hasRunEnded(after)).toBe(true);
}

describe('the run view over reconnects and replays', () => {
  it('model: identity never latches a stand-in, the cursor never goes back, ended stays ended', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const random = new Seeded(seed);
      const events = journal(random);
      let state = initialCoreState();
      for (const delivery of deliveries(random, events)) {
        const next =
          delivery.route === 'stream'
            ? reduceEventBatch(state, delivery.events)
            : reduceResponseEvents(state, delivery.events);
        check(state, next);
        state = next;
      }
      expect(state.sequence).toBe(events.length);
      expect(state.runId).toBe(events.some(item => item.run_id === RUN_ID) ? RUN_ID : null);
      const last = events.at(-1);
      expect(hasRunEnded(state)).toBe(
        last?.type === 'run_failed' || last?.type === 'run_interrupted',
      );
      expect(state.diagnostics.filter(item => item.code === 'run_identity_mismatch')).toEqual([]);
    }
  });
});
