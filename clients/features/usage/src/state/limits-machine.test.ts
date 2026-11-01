import {describe, expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {checkMachine} from '@vibesys/machine/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {createActor, fromPromise} from 'xstate';
import {
  LIMITS_CONNECT_POLL_MS,
  LIMITS_CONNECT_POLLS_MAX,
  LIMITS_DEADLINE_MS,
  LIMITS_FLOOR_MS,
  LIMITS_STORED_AGE_S,
  type LimitsAnswer,
  limitsMachine,
} from './limits-machine.js';

const NOW = Date.UTC(2026, 9, 31, 12, 0, 0);
const NOTHING: LimitsAnswer = {replies: [], unreachable: [], unsupported: [], connecting: []};
const WAITING: LimitsAnswer = {...NOTHING, connecting: ['local']};

interface Pending {
  readonly maxAgeS: number;
  readonly resolve: (answer: LimitsAnswer) => void;
  readonly reject: (error: Error) => void;
}

/** A machine whose questions are the test's to answer, one at a time. */
function harness() {
  const asked: Pending[] = [];
  const time = new FakeClock();
  const actor = createActor(
    limitsMachine.provide({
      actors: {
        check: fromPromise(
          ({input}) =>
            new Promise<LimitsAnswer>((resolve, reject) => {
              asked.push({maxAgeS: input.maxAgeS, resolve, reject});
            }),
        ),
      },
    }),
    {clock: clockFrom(time.schedule)},
  );
  actor.start();
  const phase = (): string => String(actor.getSnapshot().value);
  const flush = async (): Promise<void> => {
    for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
  };
  return {actor, asked, time, phase, flush};
}

describe('limitsMachine', () => {
  test('opening asks once and lets the host answer a reading under five minutes old as it is', async () => {
    const {actor, asked, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    expect(phase()).toBe('checking');
    expect(asked.map(each => each.maxAgeS)).toEqual([LIMITS_STORED_AGE_S]);
    asked[0]?.resolve(NOTHING);
    await flush();
    expect(phase()).toBe('idle');
    expect(actor.getSnapshot().context.answer).toBe(NOTHING);
  });

  test('opening again inside the floor asks nothing, and again after it asks again', async () => {
    const {actor, asked, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    asked[0]?.resolve(NOTHING);
    await flush();
    actor.send({type: 'open', now: NOW + LIMITS_FLOOR_MS - 1});
    expect(phase()).toBe('idle');
    expect(asked).toHaveLength(1);
    actor.send({type: 'open', now: NOW + LIMITS_FLOOR_MS});
    expect(phase()).toBe('checking');
    expect(asked).toHaveLength(2);
  });

  test('the floor holds after a failed question, which keeps the figures shown and says so', async () => {
    const {actor, asked, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    asked[0]?.resolve(NOTHING);
    await flush();
    actor.send({type: 'refresh', now: NOW + 10});
    asked[1]?.reject(new Error('down'));
    await flush();
    expect(phase()).toBe('idle');
    expect(actor.getSnapshot().context).toMatchObject({failed: true, answer: NOTHING});
    actor.send({type: 'open', now: NOW + 20});
    expect(asked).toHaveLength(2);
  });

  test('refresh asks the CLIs whatever the floor, and a refresh while asking replaces the question', async () => {
    const {actor, asked, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    asked[0]?.resolve(NOTHING);
    await flush();
    actor.send({type: 'refresh', now: NOW + 1});
    expect(asked[1]?.maxAgeS).toBe(0);
    actor.send({type: 'refresh', now: NOW + 2});
    expect(asked).toHaveLength(3);
    // The answer of the question that was replaced arrives late and is ignored.
    asked[1]?.resolve({...NOTHING, unreachable: ['local']});
    await flush();
    expect(phase()).toBe('checking');
    expect(actor.getSnapshot().context.answer).toBe(NOTHING);
    asked[2]?.resolve(NOTHING);
    await flush();
    expect(phase()).toBe('idle');
  });

  test('while every machine is connecting the tab waits and asks again, and shows nothing as limits', async () => {
    const {actor, asked, time, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    asked[0]?.resolve(WAITING);
    await flush();
    expect(phase()).toBe('connecting');
    expect(actor.getSnapshot().context.answer).toBeNull();
    time.advanceBy(LIMITS_CONNECT_POLL_MS);
    expect(phase()).toBe('checking');
    expect(asked).toHaveLength(2);
    asked[1]?.resolve(NOTHING);
    await flush();
    expect(phase()).toBe('idle');
    expect(actor.getSnapshot().context.answer).toBe(NOTHING);
  });

  test('the wait for a connection is bounded: the last answer is shown as it is', async () => {
    const {actor, asked, time, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    for (let poll = 0; poll <= LIMITS_CONNECT_POLLS_MAX; poll += 1) {
      asked[poll]?.resolve(WAITING);
      await flush();
      if (phase() === 'connecting') time.advanceBy(LIMITS_CONNECT_POLL_MS);
    }
    expect(phase()).toBe('idle');
    expect(asked).toHaveLength(LIMITS_CONNECT_POLLS_MAX + 1);
    expect(actor.getSnapshot().context.answer).toBe(WAITING);
  });

  test('Refresh while waiting asks the CLIs at once', async () => {
    const {actor, asked, phase, flush} = harness();
    actor.send({type: 'open', now: NOW});
    asked[0]?.resolve(WAITING);
    await flush();
    actor.send({type: 'refresh', now: NOW + 5});
    expect(phase()).toBe('checking');
    expect(asked.map(each => each.maxAgeS)).toEqual([LIMITS_STORED_AGE_S, 0]);
  });

  test('opening while a question runs asks nothing more', () => {
    const {actor, asked} = harness();
    actor.send({type: 'open', now: NOW});
    actor.send({type: 'open', now: NOW + LIMITS_FLOOR_MS * 2});
    expect(asked).toHaveLength(1);
  });

  test('a host that never answers ends at the deadline, failed', async () => {
    const {actor, time, phase} = harness();
    actor.send({type: 'open', now: NOW});
    time.advanceBy(LIMITS_DEADLINE_MS - 1);
    expect(phase()).toBe('checking');
    time.advanceBy(1);
    expect(phase()).toBe('idle');
    expect(actor.getSnapshot().context.failed).toBe(true);
  });

  test('model: the question and its deadline always belong to the checking state', () => {
    checkMachine({
      machine: limitsMachine,
      input: undefined,
      event: (random, live) => {
        const operation = live.operations[0];
        const delay = live.delays[0];
        const now = NOW + random.int(3 * LIMITS_FLOOR_MS);
        const events = [
          {type: 'open', now},
          {type: 'refresh', now},
          ...(operation === undefined
            ? []
            : [live.done(operation, NOTHING), live.error(operation, new Error('down'))]),
          ...(delay === undefined ? [] : [live.fire(delay)]),
        ];
        return random.pick(events);
      },
    });
  });
});
