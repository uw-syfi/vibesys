import {expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {checkMachine} from '@vibesys/machine/testing';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {createActor, fromPromise} from 'xstate';
// test-isolation: exercise the gateway's pure operation/deadline ownership across every control mode.
import {GATEWAY_FIRST_CONNECT_BUDGET_MS, gatewayMachine} from './gateway.js';

test('gateway model bounds retries and owns all dials and backoff delays', () => {
  checkMachine({
    machine: gatewayMachine,
    input: undefined,
    event: (random, live) =>
      random.pick([
        {type: 'lost'},
        {type: 'close'},
        ...live.operations.map(operation =>
          live.done(
            operation,
            random.pick([
              {ok: true},
              {ok: false, fatal: false, error: 'link lost'},
              {ok: false, fatal: true, error: 'hash mismatch'},
            ]),
          ),
        ),
        ...live.operations.map(operation => live.error(operation, new Error('dial failed'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot => (snapshot.context.attempt <= 4 ? [] : ['retry bound exceeded']),
  });
});

type Dial = 'ok' | 'fail' | 'hang';

/** Run the gateway machine against a script of dial outcomes, as the shell provides them. */
async function recoveryTimes(random: Seeded): Promise<{longest: number; settled: boolean}> {
  const clock = new FakeClock();
  const script = Array.from({length: 40}, () => random.pick(['ok', 'fail', 'hang'] as Dial[]));
  let next = 0;
  const actor = createActor(
    gatewayMachine.provide({
      actors: {
        dial: fromPromise<{ok: true} | {ok: false; fatal: boolean; error: string}, void>(
          async () => {
            const outcome = script[next++ % script.length];
            if (outcome === 'hang') return new Promise<never>(() => {});
            return outcome === 'ok' ? {ok: true} : {ok: false, fatal: false, error: 'link lost'};
          },
        ),
      },
    }),
    {clock: clockFrom(clock.scheduleTimeout)},
  );
  actor.start();
  let enteredAt = clock.now();
  let longest = 0;
  let wasConnecting = true;
  for (let step = 0; step < 400 && actor.getSnapshot().status !== 'done'; step++) {
    for (let drain = 0; drain < 5; drain++) await Promise.resolve();
    const snapshot = actor.getSnapshot();
    const connecting = snapshot.status !== 'done' && !snapshot.matches('ready');
    if (connecting && !wasConnecting) enteredAt = clock.now();
    if (connecting) longest = Math.max(longest, clock.now() - enteredAt);
    wasConnecting = connecting;
    if (actor.getSnapshot().matches('ready') && random.bool()) actor.send({type: 'lost'});
    clock.advanceBy(random.pick([1, 500, 4_000, 30_000]));
  }
  const settled = actor.getSnapshot().status === 'done' || actor.getSnapshot().matches('ready');
  actor.stop();
  return {longest, settled};
}

test('property: a lost or hung dial reaches ready or offline within the recovery budget', async () => {
  for (const seed of SEEDS) {
    const {longest} = await recoveryTimes(new Seeded(seed));
    // One step of the driver's clock past the budget is the most a check can lag behind it.
    expect(longest).toBeLessThanOrEqual(GATEWAY_FIRST_CONNECT_BUDGET_MS + 30_000);
  }
});
