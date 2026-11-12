import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Scope from 'effect/Scope';
import * as TestClock from 'effect/testing/TestClock';
import {joinDriving, untilTrue} from '../testing/main/drive.js';
import {fakeSpawner, type Started} from '../testing/main/fake-process.js';
import {makeLinkPool} from './link-pool.js';

const isExit = (started: Started): boolean =>
  started.args.includes('-O') && started.args.includes('exit');

/**
 * Drive the virtual clock a second at a time, like `joinDriving`, until `done` holds instead of
 * until a given fiber ends: the bound here only guards against `done` never holding.
 */
const driveUntil = (done: () => boolean): Effect.Effect<void> =>
  Effect.gen(function* () {
    for (let turn = 0; turn < 20_000; turn += 1) {
      if (done()) return;
      yield* Effect.promise(() => new Promise<void>(resolve => setImmediate(resolve)));
      yield* TestClock.adjust('1 second');
    }
    return yield* Effect.die(new Error('the driven condition never held'));
  });

/** A pool over a spawner that ends `-O exit` at once (`hold` false) or leaves it open. */
function poolOver(options: {hold?: boolean; kill?: 'dies' | 'ignores'} = {}) {
  const mode = {hold: options.hold === true};
  const spawner = fakeSpawner(started => {
    if (isExit(started) && !mode.hold) started.process.end(0);
  }, options.kill ?? 'dies');
  const lines: string[] = [];
  return {
    spawner,
    lines,
    mode,
    make: makeLinkPool('/tmp/vsd-1/%C', line => lines.push(line)).pipe(
      Effect.provide(spawner.layer),
    ),
  };
}

describe('link pool', () => {
  it.effect('shares one link between leases of a host and releases it with the last', () =>
    Effect.gen(function* () {
      const {spawner, make} = poolOver();
      const poolScope = yield* Scope.make();
      const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
      const first = yield* Scope.make();
      const second = yield* Scope.make();
      const a = yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, first));
      const b = yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, second));
      const other = yield* pool.lease('host-b').pipe(Effect.provideService(Scope.Scope, second));
      expect(b).toBe(a);
      expect(other).not.toBe(a);
      yield* Scope.close(first, Exit.void);
      expect(spawner.started.filter(isExit)).toHaveLength(0);
      yield* Scope.close(second, Exit.void);
      yield* untilTrue(() => spawner.started.filter(isExit).length === 2);
      yield* Scope.close(poolScope, Exit.void);
    }),
  );

  it.effect('makes a new lease of a host wait for its previous link to finish releasing', () =>
    Effect.gen(function* () {
      const {spawner, mode, make} = poolOver({hold: true});
      const poolScope = yield* Scope.make();
      const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
      const first = yield* Scope.make();
      const old = yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, first));
      const releasing = yield* Effect.forkChild(Scope.close(first, Exit.void));
      yield* untilTrue(() => spawner.started.some(isExit));
      const second = yield* Scope.make();
      const waiting = yield* Effect.forkChild(
        pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, second)),
      );
      yield* Effect.promise(() => new Promise(resolve => setImmediate(resolve)));
      expect(waiting.pollUnsafe()).toBeUndefined();
      spawner.started.find(isExit)?.process.end(0);
      yield* Fiber.join(releasing);

      const fresh = yield* Fiber.join(waiting);
      expect(fresh).not.toBe(old);
      mode.hold = false;
      yield* Scope.close(second, Exit.void);
    }),
  );

  it.effect(
    'refuses a lease while its host cannot be confirmed released, and serves it once it can',
    () =>
      Effect.gen(function* () {
        const {lines, mode, make} = poolOver({hold: true, kill: 'ignores'});
        const poolScope = yield* Scope.make();
        const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
        const first = yield* Scope.make();
        yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, first));
        yield* joinDriving(yield* Effect.forkChild(Scope.close(first, Exit.void)), '1 second');
        const second = yield* Scope.make();
        const lease = pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, second));
        // The next lease tries the release again, and while it still cannot be confirmed it is refused
        // with the reason, so that nothing runs two masters on one control path.
        const refused = yield* joinDriving(yield* Effect.forkChild(Effect.flip(lease)), '1 second');
        expect(refused.message).toContain('could not confirm teardown');
        expect(lines).toEqual(['could not retire host-a']);
        // Another host is unaffected.
        yield* pool.lease('host-b').pipe(Effect.provideService(Scope.Scope, second));
        // Not for good: once the master answers, the host is served again.
        mode.hold = false;
        const served = yield* joinDriving(yield* Effect.forkChild(lease), '1 second');
        expect(served.alias).toBe('host-a');
      }),
  );

  it.effect('releases the links still held when the pool closes, and refuses new leases', () =>
    Effect.gen(function* () {
      const {spawner, make} = poolOver();
      const poolScope = yield* Scope.make();
      const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
      const held = yield* Scope.make();
      yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, held));
      yield* Scope.close(poolScope, Exit.void);
      expect(spawner.started.filter(isExit)).toHaveLength(1);
      const refused = yield* Effect.flip(
        pool.lease('host-b').pipe(Effect.provideService(Scope.Scope, held)),
      );
      expect(refused.message).toContain('released');
    }),
  );

  it.effect(
    'gives up on a frozen host within its own bound when the pool closes, naming it, without delaying a healthy host',
    () =>
      Effect.gen(function* () {
        const lines: string[] = [];
        // host-a's '-O exit' is left open and ignores every signal, as if its master were frozen
        // deep enough that nothing ssh-link.ts owns can confirm it gone. host-b answers at once.
        const spawner = fakeSpawner(started => {
          if (isExit(started) && started.args.includes('host-b')) started.process.end(0);
        }, 'ignores');
        const make = makeLinkPool('/tmp/vsd-1/%C', line => lines.push(line)).pipe(
          Effect.provide(spawner.layer),
        );
        const poolScope = yield* Scope.make();
        const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
        const held = yield* Scope.make();
        yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, held));
        yield* pool.lease('host-b').pipe(Effect.provideService(Scope.Scope, held));

        const closing = yield* Effect.forkChild(Scope.close(poolScope, Exit.void));

        // host-b's exit is answered without the clock moving at all, so its teardown cannot be
        // waiting behind host-a's, which needs the clock to advance before it can time out once.
        yield* untilTrue(
          () => spawner.started.filter(s => isExit(s) && s.args.includes('host-b')).length === 1,
        );
        expect(closing.pollUnsafe()).toBeUndefined();

        // The pool's own close gives up on host-a within RETIRE_BOUND_MS, well short of
        // ssh-link.ts's own full chain for an unanswered '-O exit' whose own kill is also ignored
        // (a 1 s control-command timeout, then two more 1 s termination graces: 3 s in all).
        yield* driveUntil(() => closing.pollUnsafe() !== undefined);
        expect(lines).toEqual([
          'gave up waiting for host-a to release (still retiring in the background)',
        ]);

        // "Still retiring in the background" is not just wording: host-a's own retirement keeps
        // running after the pool gave up waiting on it, and in time reports its own outcome.
        yield* driveUntil(() => lines.length > 1);
        expect(lines).toEqual([
          'gave up waiting for host-a to release (still retiring in the background)',
          'could not retire host-a',
        ]);
      }),
  );
});
