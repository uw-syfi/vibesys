import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Scope from 'effect/Scope';
import {joinDriving, untilTrue} from '../testing/main/drive.js';
import {fakeSpawner, type Started} from '../testing/main/fake-process.js';
import {makeLinkPool} from './link-pool.js';

const isExit = (started: Started): boolean =>
  started.args.includes('-O') && started.args.includes('exit');

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

  it.effect('refuses a host whose link could not be confirmed released', () =>
    Effect.gen(function* () {
      const {lines, make} = poolOver({hold: true, kill: 'ignores'});
      const poolScope = yield* Scope.make();
      const pool = yield* make.pipe(Effect.provideService(Scope.Scope, poolScope));
      const first = yield* Scope.make();
      yield* pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, first));
      yield* joinDriving(yield* Effect.forkChild(Scope.close(first, Exit.void)), '1 second');
      const second = yield* Scope.make();
      const refused = yield* Effect.flip(
        pool.lease('host-a').pipe(Effect.provideService(Scope.Scope, second)),
      );
      expect(refused.message).toContain('could not confirm teardown');
      expect(lines).toEqual(['could not retire host-a']);
      // Another host is unaffected.
      yield* pool.lease('host-b').pipe(Effect.provideService(Scope.Scope, second));
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
});
