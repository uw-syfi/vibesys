import {describe, expect, test} from 'bun:test';
import type {CoreState, SteerDeliveryFact} from '@vibesys/run-state';
import {initialCoreState} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type DeliveryEvent,
  type DeliveryState,
  deliveriesOf,
  deliveryEnded,
  foldDelivery,
  SENT,
} from './delivery.js';

function randomEvent(random: Seeded): DeliveryEvent {
  return random.pick<DeliveryEvent>([
    {kind: 'queued'},
    {kind: 'delivered', idea: random.int(9), where: null},
    {kind: 'delivered', idea: null, where: null},
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
          random.pick<DeliveryEvent>([
            {kind: 'queued'},
            {kind: 'delivered', idea: random.int(9), where: null},
          ]),
      );
      const last = run(events).at(-1) as DeliveryState;
      expect(deliveryEnded(last)).toBe(false);
      const first = events.find(event => event.kind === 'delivered');
      if (first?.kind === 'delivered')
        expect(last).toEqual({kind: 'delivered', idea: first.idea, where: null});
    }
  });

  test('queued then delivered then withdrawn walks forward and stops', () => {
    const states = run([
      {kind: 'queued'},
      {kind: 'delivered', idea: 4, where: null},
      {kind: 'queued'},
      {kind: 'withdrawn'},
      {kind: 'delivered', idea: 6, where: null},
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

describe('deliveriesOf', () => {
  const fact = (patch: Partial<SteerDeliveryFact>): SteerDeliveryFact => ({
    sequence: 1,
    step: 'queued',
    controlId: 'c-1',
    action: 'steer',
    withdraws: null,
    role: null,
    attemptId: null,
    reason: null,
    ...patch,
  });
  const core = (...facts: SteerDeliveryFact[]): CoreState => ({
    ...initialCoreState(),
    steerDeliveries: facts,
  });

  test('each server step becomes the delivery event the card folds', () => {
    const steps = core(
      fact({step: 'queued'}),
      fact({step: 'reached_mid_turn', role: 'implementer', attemptId: 'a-3'}),
      fact({step: 'in_force', controlId: 'c-2', role: 'judge'}),
      fact({step: 'dropped', controlId: 'c-3'}),
      fact({step: 'refused', controlId: 'c-4', reason: 'no such role'}),
      fact({step: 'ended', controlId: 'w-1', withdraws: 'c-2', reason: 'withdrawn'}),
      fact({step: 'ended', controlId: 'c-5'}),
    );
    expect(deliveriesOf(steps, id => (id === 'a-3' ? 4 : null))).toEqual([
      {controlId: 'c-1', event: {kind: 'queued'}},
      {controlId: 'c-1', event: {kind: 'delivered', idea: 4, where: 'Builder'}},
      {controlId: 'c-2', event: {kind: 'delivered', idea: null, where: 'Reviewer'}},
      {controlId: 'c-3', event: {kind: 'dropped'}},
      {controlId: 'c-4', event: {kind: 'refused', reason: 'no such role'}},
      {controlId: 'c-2', event: {kind: 'withdrawn'}},
      {controlId: 'c-5', event: {kind: 'ended'}},
    ]);
  });

  test('a step with no control id, or a queued withdrawal, is no card event', () => {
    expect(
      deliveriesOf(
        core(
          fact({controlId: null}),
          fact({step: 'withdraw_queued', controlId: 'w-1', action: 'withdraw', withdraws: 'c-1'}),
        ),
      ),
    ).toEqual([]);
  });
});
