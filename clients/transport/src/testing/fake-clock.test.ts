import {describe, it} from 'node:test';
import {expect} from '../test-support/expect.js';
import {FakeClock} from './fake-clock.js';

/** A small deterministic generator so each seed replays exactly. */
function random(seed: number): (bound: number) => number {
  let state = seed >>> 0 || 1;
  return bound => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return state % bound;
  };
}

interface Fired {
  readonly id: number;
  readonly dueAt: number;
  readonly at: number;
}

/** Drive one seeded interleaving of schedule, cancel, runNext and advanceBy. */
function drive(seed: number) {
  const next = random(seed);
  const clock = new FakeClock();
  const dueAt = new Map<number, number>();
  const cancels = new Map<number, () => void>();
  const cancelled = new Set<number>();
  const fired: Fired[] = [];
  let ids = 0;
  const schedule = (): void => {
    const id = ids;
    ids += 1;
    const delay = next(40);
    dueAt.set(id, clock.now() + delay);
    cancels.set(
      id,
      clock.schedule(() => {
        fired.push({id, dueAt: dueAt.get(id) ?? Number.NaN, at: clock.now()});
        // A timer may arm another, as reconnect backoff does.
        if (next(3) === 0) schedule();
      }, delay),
    );
  };
  for (let step = 0; step < 60; step += 1) {
    const choice = next(5);
    if (choice <= 1) schedule();
    else if (choice === 2 && ids > 0) {
      const id = next(ids);
      cancels.get(id)?.();
      // Cancelling a timer that already fired is a no-op, as with clearTimeout.
      if (!fired.some(entry => entry.id === id)) cancelled.add(id);
    } else if (choice === 3) {
      const before = clock.pending;
      expect(clock.runNext()).toBe(before > 0);
    } else {
      clock.advanceBy(next(30));
      expect(clock.pendingDelays().every(delay => delay > 0)).toBe(true);
    }
  }
  return {clock, fired, cancelled};
}

describe('FakeClock', () => {
  it('fires like setTimeout: at its due time, earliest due first, ties in scheduling order', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const {fired, cancelled} = drive(seed);
      for (const [index, entry] of fired.entries()) {
        expect(entry.at).toBe(entry.dueAt);
        expect(cancelled.has(entry.id)).toBe(false);
        const previous = fired[index - 1];
        if (previous === undefined) continue;
        expect(previous.dueAt <= entry.dueAt).toBe(true);
        if (previous.dueAt === entry.dueAt) expect(previous.id < entry.id).toBe(true);
      }
    }
  });

  it('runNext moves time to the earliest-due timer, not the first scheduled', () => {
    const clock = new FakeClock();
    const order: string[] = [];
    clock.schedule(() => order.push('late'), 30);
    clock.schedule(() => order.push('early'), 10);
    clock.schedule(() => order.push('tie'), 10);

    expect(clock.runNext()).toBe(true);
    expect(order).toEqual(['early']);
    expect(clock.now()).toBe(10);
    expect(clock.pendingDelays()).toEqual([0, 20]);
  });

  it('runPending advances time until every timer live now has fired', () => {
    const clock = new FakeClock();
    const order: string[] = [];
    clock.schedule(() => order.push('b'), 20);
    clock.schedule(() => order.push('a'), 5);

    clock.runPending();

    expect(order).toEqual(['a', 'b']);
    expect(clock.now()).toBe(20);
    expect(clock.pending).toBe(0);
  });

  it('waitUntilScheduled resolves once production arms a timer of that delay', async () => {
    const clock = new FakeClock();
    let calls = 0;
    const waiting = clock.waitUntilScheduled(5);
    const cancel = clock.schedule(() => {
      calls += 1;
    }, 5);
    await waiting;
    cancel();

    expect(clock.runNext()).toBe(false);
    expect(calls).toBe(0);
    expect(clock.pendingDelays()).toEqual([]);
  });
});
