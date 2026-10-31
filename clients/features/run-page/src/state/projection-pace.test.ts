/**
 * When the page may project: a catch-up read is shown at most once per tick, anything else at
 * once. Input space: 0 to 40 steps, each an offer (catching up or not) or a wait of 0 to 1200 ms
 * on a virtual clock.
 */
import {expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {createActor} from 'xstate';
import {PROJECTION_PACE_MS, projectionPace} from './index.js';

type Cause = 'catching-up' | 'settled' | 'tick';

function pacer() {
  const clock = new FakeClock();
  const actor = createActor(projectionPace<number>(), {clock: clockFrom(clock.schedule)});
  const shown: {value: number; at: number; cause: Cause}[] = [];
  let cause: Cause = 'tick';
  actor.subscribe(({context}) => {
    if (context.shown !== undefined && context.shown !== shown.at(-1)?.value)
      shown.push({value: context.shown, at: clock.now(), cause});
  });
  actor.start();
  let next = 0;
  return {
    shown,
    /** Offer a new value, as the next publication of the loader. */
    offer(catchingUp: boolean): number {
      next += 1;
      cause = catchingUp ? 'catching-up' : 'settled';
      actor.send({type: 'offer', value: next, catchingUp});
      cause = 'tick';
      return next;
    },
    wait(ms: number): void {
      cause = 'tick';
      clock.advanceBy(ms);
    },
    stop: () => actor.stop(),
  };
}

type Step = {offer: boolean} | {wait: number};
const steps: fc.Arbitrary<Step[]> = fc.array(
  fc.oneof(
    fc.record({offer: fc.constantFrom(true, true, true, false)}),
    fc.record({wait: fc.integer({min: 0, max: 1200})}),
  ),
  {maxLength: 40},
);

test('property: the page projects newer values in order, at once when settled, and never later than a tick', () => {
  fc.assert(
    fc.property(steps, plan => {
      const run = pacer();
      let last = 0;
      for (const step of plan) {
        if ('wait' in step) run.wait(step.wait);
        else {
          last = run.offer(step.offer);
          // Anything but a catch-up is shown by the offer itself.
          if (!step.offer) expect(run.shown.at(-1)?.value).toBe(last);
        }
      }
      run.wait(PROJECTION_PACE_MS);
      // Nothing is lost: once a tick passes, the newest offer is what the page shows.
      expect(run.shown.at(-1)?.value ?? 0).toBe(last);
      // Values are only ever newer than the one before.
      run.shown.forEach((item, index) => {
        expect(item.value).toBeGreaterThan(run.shown[index - 1]?.value ?? 0);
      });
      // A show that is part of a catch-up is followed by another no sooner than a tick, unless
      // the read settles in between, which is shown at once.
      run.shown.slice(0, -1).forEach((item, index) => {
        const following = run.shown[index + 1];
        if (item.cause === 'settled' || following === undefined) return;
        expect(following.cause === 'settled' || following.at - item.at >= PROJECTION_PACE_MS).toBe(
          true,
        );
      });
    }),
    {numRuns: 200, seed: 20261030},
  );
});

test('updates inside one tick of a catch-up give one more show, with the newest', () => {
  const run = pacer();
  run.offer(true);
  expect(run.shown.map(item => item.value)).toEqual([1]);
  let newest = 0;
  for (let count = 0; count < 20; count += 1) {
    run.wait(20);
    newest = run.offer(true);
  }
  expect(run.shown.map(item => item.value)).toEqual([1]);
  run.wait(PROJECTION_PACE_MS);
  expect(run.shown.map(item => item.value)).toEqual([1, newest]);
});

test('the read settling is shown at once and leaves nothing to show at the tick', () => {
  const run = pacer();
  run.offer(true);
  run.wait(100);
  run.offer(true);
  const settled = run.offer(false);
  expect(run.shown.map(item => item.value)).toEqual([1, settled]);
  run.wait(10 * PROJECTION_PACE_MS);
  expect(run.shown.map(item => item.value)).toEqual([1, settled]);
});

test('after a quiet tick the next catch-up value is shown at once', () => {
  const run = pacer();
  run.offer(true);
  run.wait(PROJECTION_PACE_MS);
  const next = run.offer(true);
  expect(run.shown.at(-1)?.value).toBe(next);
});

test('stopping drops a value still waiting for its tick', () => {
  const run = pacer();
  run.offer(true);
  run.offer(true);
  run.stop();
  run.wait(10 * PROJECTION_PACE_MS);
  expect(run.shown.map(item => item.value)).toEqual([1]);
});
