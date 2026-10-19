import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type DeliveryEvent,
  type DeliveryState,
  deliveryEnded,
  foldDelivery,
  SENT,
} from './delivery.js';

function randomEvent(random: Seeded): DeliveryEvent {
  return random.pick<DeliveryEvent>([
    {kind: 'queued'},
    {kind: 'delivered', idea: random.int(9)},
    {kind: 'delivered', idea: null},
    {kind: 'dropped'},
    {kind: 'ended'},
    {kind: 'withdrawn'},
    {kind: 'refused', reason: 'no such role'},
  ]);
}

function sequence(seed: number): DeliveryEvent[] {
  const random = new Seeded(seed);
  return Array.from({length: 1 + random.int(8)}, () => randomEvent(random));
}

function run(events: readonly DeliveryEvent[]): DeliveryState[] {
  const states: DeliveryState[] = [SENT];
  for (const event of events) states.push(foldDelivery(states.at(-1) as DeliveryState, event));
  return states;
}

describe('foldDelivery', () => {
  test('any order of events leaves a valid state, and an ending is final', () => {
    for (const seed of SEEDS) {
      const states = run(sequence(seed));
      for (let index = 1; index < states.length; index += 1) {
        const before = states[index - 1] as DeliveryState;
        const after = states[index] as DeliveryState;
        if (deliveryEnded(before)) expect(after).toBe(before);
        // A steer that was delivered is never reported as dropped or refused afterwards.
        if (before.kind === 'delivered') {
          expect(['delivered', 'ended', 'withdrawn']).toContain(after.kind);
        }
      }
    }
  });

  test('an event twice is an event once', () => {
    for (const seed of SEEDS) {
      const events = sequence(seed);
      const once = run(events).at(-1);
      const twice = run(events.flatMap(event => [event, event])).at(-1);
      expect(twice).toEqual(once);
    }
  });

  test('without an ending event the delivery is not over, and the first delivery idea stays', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const events = Array.from(
        {length: 1 + random.int(6)},
        (): DeliveryEvent =>
          random.pick<DeliveryEvent>([{kind: 'queued'}, {kind: 'delivered', idea: random.int(9)}]),
      );
      const last = run(events).at(-1) as DeliveryState;
      expect(deliveryEnded(last)).toBe(false);
      const first = events.find(event => event.kind === 'delivered');
      if (first?.kind === 'delivered') expect(last).toEqual({kind: 'delivered', idea: first.idea});
    }
  });

  test('queued then delivered then withdrawn walks forward and stops', () => {
    const states = run([
      {kind: 'queued'},
      {kind: 'delivered', idea: 4},
      {kind: 'queued'},
      {kind: 'withdrawn'},
      {kind: 'delivered', idea: 6},
    ]).map(state => state.kind);
    expect(states).toEqual(['sent', 'waiting', 'delivered', 'delivered', 'withdrawn', 'withdrawn']);
  });

  test('a refusal before delivery keeps its reason', () => {
    expect(foldDelivery(SENT, {kind: 'refused', reason: 'no such role'})).toEqual({
      kind: 'refused',
      reason: 'no such role',
    });
  });
});
